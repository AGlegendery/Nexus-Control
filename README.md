# Control Nexus - Python Remote Task Management System

Control Nexus is a production-oriented local remote task manager with a Flask control server and multiple Python agents using HTTP polling (default every 10 seconds).

## Stack

- Server: Python, Flask, SQLite, Jinja2, local CSS/JS
- Agent: Python, requests
- No Node.js
- No CDN CSS/JS dependencies

## Key Capabilities

- Admin-authenticated dashboard with session login
- Settings page to change dashboard password from UI
- Dedicated script editor page with per-script edit workflow
- Multi-agent registry (`agent_id`, `hostname`, `os_family`, `platform`, `last_seen`, `status`)
- Agent registration + tokenized authentication
- Task queue with statuses (`pending`, `running`, `completed`, `failed`)
- Task stop controls from UI (`Kill`) for pending/running tasks
- **Direct Shell Console**: Authenticated one-shot shell execution with live output chunk polling
- Scheduled task execution for agents or agent groups
- Agent groups, health telemetry, CPU/memory/disk alerts, and system specification panels
- Audit logs for administrative and agent activity
- Role-based access with `admin`, `operator`, and `viewer` roles
- Manual task retries with attempt lineage
- Task persistence in both:
  - SQLite (`tasks` table)
  - JSON files (`server/tasks/task_<uuid>.json`)
- Script Library supports two kinds:
  - `command`: OS-specific command mapping
  - `file`: actual script code + filename (`.bat`, `.vbs`, `.ps1`, `.sh`, etc.)
- File transfer workflow:
  - Upload local script file to Script Library
  - Queue `deploy_script` to transfer into the agent `scripts/` folder
- Agent script inventory sync:
  - Agent periodically reports available files in its `scripts/` folder
  - Shown in agent detail page
- Optional non-zero exit success mode to avoid false failures when tools return non-zero despite successful action

## Safety Constraint

Reverse-shell/backdoor task types are intentionally blocked.

## Task Types

- `run_script`
  - Runs a script from server Script Library
  - For `command` scripts, server sends OS-specific command
  - For `file` scripts, server sends content and agent writes+runs it
- `deploy_script`
  - Transfers a `file` Script Library item to agent `scripts/` folder only
- `run_agent_file`
  - Runs an existing file already present in agent `scripts/` folder
- `execute_shell`
  - Executes a one-shot command through the agent
  - Access via "Open Direct Shell" on the agent detail page
   - Shows output chunks while the command runs and the final result when complete

  - Supports command history in the current browser session
  - Works on Windows (cmd), Linux, and macOS (bash)

## Timeout and Kill Behavior

- Timeout value means maximum script runtime in seconds.
- Set timeout to `0` for unlimited runtime.
- `Kill` on a pending task marks it `cancelled`.
- `Kill` on a running task sends a stop request to the agent; the agent reports `killed` when terminated.
- You can enable “Treat non-zero exit code as success” per script or per `run_agent_file` dispatch.

## Project Layout

- `server/app.py`: routes, auth, APIs, task dispatch logic
- `server/db.py`: schema, migrations, default seeding
- `server/task_store.py`: task JSON file write/update
- `server/templates/`: dashboard/login/settings/agent UI
- `server/static/css/style.css`: local polished UI styling
- `server/static/js/app.js`: local UI behavior
- `agent/agent.py`: register, poll, inventory sync, execute/report loop

## API Endpoints

- `POST /api/register`
  - Header: `X-Register-Token`
- `POST /api/heartbeat/<agent_id>`
  - Header: `X-Agent-Token`
  - Reports system specifications, health metrics, and liveness
- `POST /api/inventory/<agent_id>`
  - Header: `X-Agent-Token`
  - Agent pushes its scripts folder inventory
- `GET /api/poll/<agent_id>`
  - Header: `X-Agent-Token`
  - Returns pending task + execution payload
- `POST /api/report/<agent_id>/<task_id>`
  - Header: `X-Agent-Token`
  - Reports completion/failure and output/error
- `POST /api/shell_execute/<agent_id>`
  - Session authentication (admin) plus CSRF token
  - Queues an `execute_shell` task
- `GET /api/shell_task/<task_id>`
  - Session authentication (admin)
  - Returns task status, final output, and live output chunks
- `POST /api/task-output/<agent_id>/<task_id>`
  - Header: `X-Agent-Token`
  - Stores stdout/stderr chunks for live shell polling
- `GET /health`
  - Displays agent health and open alerts
- `GET /schedules`
  - Creates and manages recurring agent/group tasks
- `GET /agent-groups`
  - Creates groups and manages membership
- `GET /audit`
  - Displays recent audit events
- `GET /users-roles`
  - Creates users and assigns roles

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
```

### Configure Server

```bash
cd server
cp .env.example .env
```

Set at least:

- `REGISTER_TOKEN` (must match agent)
- `FLASK_SECRET_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` (or `ADMIN_PASSWORD_HASH`)
- `SCHEDULER_ENABLED=1` for recurring schedules
- `SCHEDULER_INTERVAL_SECONDS=15` for scheduler frequency

### Configure Agent

```bash
cd ../agent
cp .env.example .env
```

Set at least:

- `SERVER_URL`
- `REGISTER_TOKEN`
- For your VPS host, set: `SERVER_URL=https://your-server.example.com`

## Run Server

```bash
cd server
export $(grep -v '^#' .env | xargs)
python3 app.py
```

The shell UI queues a task and polls durable output chunks every 500ms. Agent telemetry and schedule processing run in the server process.

Open:

- `http://127.0.0.1:5000/login`

Windows PowerShell alternative:

```powershell
cd server
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.StartsWith('#')) {
    $kv = $_.Split('=', 2)
    [System.Environment]::SetEnvironmentVariable($kv[0], $kv[1], 'Process')
  }
}
python app.py
```

## Run Agent

```bash
cd agent
export $(grep -v '^#' .env | xargs)
python3 agent.py
```

Run one agent process per machine.

If running as a packaged `.exe`, place an `.env` file next to the executable.

Instead of hand-editing `agent/.env`, open **Deploy Agent** in the panel, set the public server URL, and download a ready-to-run `agent.py` with the URL and registration token already embedded.

## Deploy to Railway or Similar Hosts

The repository ships a `Dockerfile`, `railway.json`, and `Procfile`. Any Docker host (Railway, Render, Fly.io, Koyeb) works when it can run one long-lived container with a persistent volume.

1. Push the project to GitHub, then create a Railway project from the repo. `railway.json` uses the Dockerfile and the `/healthz` health check.
2. Add these variables in the platform dashboard:
   - `FLASK_SECRET_KEY` - random 32+ character string
   - `REGISTER_TOKEN` - strong shared token for agents
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD` (or `ADMIN_PASSWORD_HASH`)
   - `SESSION_COOKIE_SECURE=1`
   - `TRUST_PROXY_HEADERS=1` (enables `ProxyFix`; automatic when `PORT` is set)
3. Attach a volume mounted at `/data`. The image sets `DATA_DIR=/data`, so `control.db` and task files survive redeploys.
4. Keep exactly one replica. SQLite, the in-process scheduler, and the in-memory rate limiter assume a single server process.
5. Open the generated HTTPS domain, go to **Deploy Agent**, and paste that same URL. No static IP is needed because agents poll outbound; no inbound port has to be exposed.

The platform supplies `PORT`; the server binds `0.0.0.0` automatically. Set `SERVER_PORT` only to override it locally. `/healthz` returns `503` if the database cannot be opened.


## Script Workflows

1. Add command script in "Add / Update Command Script".
2. Add file script by either:
   - uploading local script file, or
   - writing code in textarea + file name.
3. Dispatch tasks:
   - `run_script` to run library script,
   - `deploy_script` to transfer script only,
   - `run_agent_file` to run a script already present on agent.
4. Use Direct Shell:
   - Click "Open Direct Shell" on the agent detail page
   - Type a command and press Enter
   - View output while the task runs and the final result when complete
   - Use arrow keys to navigate command history
- Use the pencil `Edit` button in Script Library to update any script.

## Security Notes

- Dashboard routes require an authenticated session and role permission.
- Agent APIs require registration token + per-agent token.
- File names are sanitized before save/execute.
- Audit logs record actions without passwords, tokens, shell payloads, or uploaded content.
- Reverse-shell task type is blocked.
- Health alert acknowledgements require `health.manage`; the `viewer` role stays read-only.
- If `FLASK_SECRET_KEY` is unset or still an example value, the server generates a random per-process key (sessions reset on restart). Set a real key to keep sessions across restarts.
- When bound to a network interface (`PORT` set or non-loopback `SERVER_HOST`), the server refuses to start while `REGISTER_TOKEN` or the initial `ADMIN_PASSWORD` still use default/example values.
- HTTPS responses receive HSTS, and session cookies are `Secure` when `PORT` is set.

## Optional Hardening

- Use only `ADMIN_PASSWORD_HASH` and remove plaintext admin password.
- Rotate `REGISTER_TOKEN` regularly.
- Restrict inbound IPs to known management hosts.
- Put server behind HTTPS reverse proxy.
- Generate hash locally:
  - `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourStrongPassword'))"`
