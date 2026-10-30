import sqlite3
from pathlib import Path


BASE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    last_seen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    os_family TEXT,
    platform TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_group_members (
    group_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, agent_id),
    FOREIGN KEY (group_id) REFERENCES agent_groups(group_id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    schedule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    task_value TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    timeout_seconds INTEGER NOT NULL DEFAULT 180,
    allow_nonzero_success INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    remote_address TEXT,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS agent_system_info (
    agent_id TEXT PRIMARY KEY,
    reported_at TEXT NOT NULL,
    agent_version TEXT,
    architecture TEXT,
    cpu_model TEXT,
    logical_cpu_count INTEGER,
    memory_total_bytes INTEGER,
    disk_total_bytes INTEGER,
    boot_time TEXT,
    ip_addresses_json TEXT,
    raw_json TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_health (
    agent_id TEXT PRIMARY KEY,
    reported_at TEXT NOT NULL,
    cpu_percent REAL,
    memory_percent REAL,
    memory_used_bytes INTEGER,
    disk_percent REAL,
    load_1m REAL,
    uptime_seconds INTEGER,
    battery_percent REAL,
    raw_json TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS health_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    message TEXT NOT NULL,
    observed_value REAL,
    threshold_value REAL,
    opened_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at TEXT,
    details_json TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL,
    output TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    task_file TEXT NOT NULL,
    meta_json TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    schedule_id TEXT,
    root_task_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS task_output_chunks (
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    stream TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt_number, stream, sequence),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scripts (
    script_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    windows_command TEXT NOT NULL,
    linux_command TEXT NOT NULL,
    darwin_command TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    script_kind TEXT NOT NULL DEFAULT 'command',
    file_name TEXT,
    script_content TEXT,
    content_encoding TEXT NOT NULL DEFAULT 'utf8',
    content_size INTEGER NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    allow_nonzero_success INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    modified_at TEXT,
    synced_at TEXT NOT NULL,
    UNIQUE(agent_id, rel_path),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS task_controls (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_agent_status_created
ON tasks(agent_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created
ON tasks(status, created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_created
ON tasks(created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_agent_created
ON tasks(agent_id, created_at);

CREATE INDEX IF NOT EXISTS idx_agents_last_seen
ON agents(last_seen);

CREATE INDEX IF NOT EXISTS idx_scripts_enabled
ON scripts(enabled);

CREATE INDEX IF NOT EXISTS idx_agent_files_agent
ON agent_files(agent_id);

CREATE INDEX IF NOT EXISTS idx_task_controls_agent
ON task_controls(agent_id);

CREATE INDEX IF NOT EXISTS idx_schedules_due
ON scheduled_tasks(enabled, next_run_at);

CREATE INDEX IF NOT EXISTS idx_audit_occurred
ON audit_logs(occurred_at);

CREATE INDEX IF NOT EXISTS idx_audit_resource
ON audit_logs(resource_type, resource_id);

CREATE INDEX IF NOT EXISTS idx_health_alerts_open
ON health_alerts(agent_id, alert_type, status);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def migrate_schema(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "agents", "os_family"):
        conn.execute("ALTER TABLE agents ADD COLUMN os_family TEXT")
    if not column_exists(conn, "agents", "platform"):
        conn.execute("ALTER TABLE agents ADD COLUMN platform TEXT")
    if not column_exists(conn, "tasks", "meta_json"):
        conn.execute("ALTER TABLE tasks ADD COLUMN meta_json TEXT")
    if not column_exists(conn, "scripts", "script_kind"):
        conn.execute("ALTER TABLE scripts ADD COLUMN script_kind TEXT NOT NULL DEFAULT 'command'")
    if not column_exists(conn, "scripts", "file_name"):
        conn.execute("ALTER TABLE scripts ADD COLUMN file_name TEXT")
    if not column_exists(conn, "scripts", "script_content"):
        conn.execute("ALTER TABLE scripts ADD COLUMN script_content TEXT")
    if not column_exists(conn, "scripts", "content_encoding"):
        conn.execute("ALTER TABLE scripts ADD COLUMN content_encoding TEXT NOT NULL DEFAULT 'utf8'")
    if not column_exists(conn, "scripts", "content_size"):
        conn.execute("ALTER TABLE scripts ADD COLUMN content_size INTEGER NOT NULL DEFAULT 0")
    if not column_exists(conn, "scripts", "content_sha256"):
        conn.execute("ALTER TABLE scripts ADD COLUMN content_sha256 TEXT")
    if not column_exists(conn, "scripts", "allow_nonzero_success"):
        conn.execute("ALTER TABLE scripts ADD COLUMN allow_nonzero_success INTEGER NOT NULL DEFAULT 0")
    if not column_exists(conn, "tasks", "source_type"):
        conn.execute("ALTER TABLE tasks ADD COLUMN source_type TEXT NOT NULL DEFAULT 'manual'")
    if not column_exists(conn, "tasks", "schedule_id"):
        conn.execute("ALTER TABLE tasks ADD COLUMN schedule_id TEXT")
    if not column_exists(conn, "tasks", "root_task_id"):
        conn.execute("ALTER TABLE tasks ADD COLUMN root_task_id TEXT")
    if not column_exists(conn, "tasks", "attempt_number"):
        conn.execute("ALTER TABLE tasks ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1")
    if not column_exists(conn, "tasks", "max_attempts"):
        conn.execute("ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1")
    if not column_exists(conn, "tasks", "created_by"):
        conn.execute("ALTER TABLE tasks ADD COLUMN created_by TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_lineage ON tasks(root_task_id, attempt_number)")


def seed_default_scripts(conn: sqlite3.Connection) -> None:
    defaults = [
        {
            "script_id": "shutdown",
            "display_name": "Shutdown Machine",
            "description": "Power off the machine with a short grace period.",
            "windows_command": "shutdown /s /t 15 /f",
            "linux_command": "shutdown -h +1",
            "darwin_command": "osascript -e 'tell application \"System Events\" to shut down'",
            "timeout_seconds": 30,
        },
        {
            "script_id": "logout",
            "display_name": "Log Out Current User",
            "description": "Log out the currently active user session.",
            "windows_command": "shutdown /l",
            "linux_command": "loginctl terminate-user \"$USER\"",
            "darwin_command": "osascript -e 'tell application \"System Events\" to log out'",
            "timeout_seconds": 30,
        },
        {
            "script_id": "switcher",
            "display_name": "Switch User Session",
            "description": "Switch to login screen without shutting down.",
            "windows_command": "tsdiscon",
            "linux_command": "loginctl lock-session",
            "darwin_command": "'/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession' -suspend",
            "timeout_seconds": 30,
        },
        {
            "script_id": "system_info",
            "display_name": "System Information",
            "description": "Show operating system, kernel, uptime, and host details.",
            "windows_command": "systeminfo",
            "linux_command": "uname -a; printf '\\nUptime:\\n'; uptime",
            "darwin_command": "sw_vers; printf '\\nUptime:\\n'; uptime",
            "timeout_seconds": 45,
        },
        {
            "script_id": "current_identity",
            "display_name": "Current User Identity",
            "description": "Show the account, groups, and login identity used by the agent.",
            "windows_command": "whoami /all",
            "linux_command": "id; printf '\\nUser:\\n'; whoami; printf '\\nGroups:\\n'; groups",
            "darwin_command": "id; whoami; groups",
            "timeout_seconds": 30,
        },
        {
            "script_id": "network_info",
            "display_name": "Network Configuration",
            "description": "Show local addresses, adapters, routes, and DNS configuration.",
            "windows_command": "ipconfig /all",
            "linux_command": "ip -brief address || ifconfig -a; printf '\\nRoutes:\\n'; ip route || true",
            "darwin_command": "ifconfig; printf '\\nRoutes:\\n'; netstat -rn",
            "timeout_seconds": 45,
        },
        {
            "script_id": "disk_usage",
            "display_name": "Disk Usage",
            "description": "Show filesystem capacity and remaining space.",
            "windows_command": "powershell -NoProfile -Command \"Get-Volume | Select-Object DriveLetter,FileSystemLabel,SizeRemaining,Size | Format-Table -AutoSize\"",
            "linux_command": "df -hT",
            "darwin_command": "df -h",
            "timeout_seconds": 45,
        },
        {
            "script_id": "memory_usage",
            "display_name": "Memory Usage",
            "description": "Show physical memory totals, available memory, and swap usage.",
            "windows_command": "powershell -NoProfile -Command \"Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | Format-List\"",
            "linux_command": "free -h",
            "darwin_command": "vm_stat; sysctl vm.swapusage",
            "timeout_seconds": 45,
        },
        {
            "script_id": "running_processes",
            "display_name": "Top Running Processes",
            "description": "List the processes currently consuming the most CPU time.",
            "windows_command": "powershell -NoProfile -Command \"Get-Process | Sort-Object CPU -Descending | Select-Object -First 20 Name,Id,CPU,WorkingSet | Format-Table -AutoSize\"",
            "linux_command": "ps -eo pid,ppid,user,%cpu,%mem,comm --sort=-%cpu | head -n 21",
            "darwin_command": "ps -axo pid,ppid,user,%cpu,%mem,comm | head -n 21",
            "timeout_seconds": 45,
        },
        {
            "script_id": "listening_ports",
            "display_name": "Listening Network Ports",
            "description": "Show listening TCP and UDP endpoints and owning processes.",
            "windows_command": "powershell -NoProfile -Command \"Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize\"",
            "linux_command": "ss -tulpn || netstat -tulpn",
            "darwin_command": "lsof -nP -iTCP -sTCP:LISTEN || netstat -an",
            "timeout_seconds": 45,
        },
        {
            "script_id": "system_uptime",
            "display_name": "System Uptime",
            "description": "Show how long the machine has been running.",
            "windows_command": "powershell -NoProfile -Command \"(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime\"",
            "linux_command": "uptime -p; uptime",
            "darwin_command": "uptime",
            "timeout_seconds": 30,
        },
        {
            "script_id": "logged_in_users",
            "display_name": "Logged In Users",
            "description": "List users currently logged into the machine.",
            "windows_command": "query user",
            "linux_command": "who; printf '\\nLogged-in processes:\\n'; w",
            "darwin_command": "who; printf '\\nLogged-in processes:\\n'; w",
            "timeout_seconds": 30,
        },
        {
            "script_id": "installed_apps",
            "display_name": "Installed Applications",
            "description": "List installed applications and package versions.",
            "windows_command": "powershell -NoProfile -Command \"Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* -ErrorAction SilentlyContinue | Where-Object DisplayName | Select-Object DisplayName,DisplayVersion | Sort-Object DisplayName | Format-Table -AutoSize\"",
            "linux_command": "if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='${Package} ${Version}\\n' | sort; elif command -v rpm >/dev/null 2>&1; then rpm -qa; elif command -v pacman >/dev/null 2>&1; then pacman -Q; else printf 'No supported package manager found\\n'; fi",
            "darwin_command": "system_profiler SPApplicationsDataType",
            "timeout_seconds": 90,
        },
        {
            "script_id": "connectivity_test",
            "display_name": "Internet Connectivity Test",
            "description": "Test basic HTTPS and ICMP connectivity to a public endpoint.",
            "windows_command": "powershell -NoProfile -Command \"Test-NetConnection 1.1.1.1 -Port 443 | Select-Object ComputerName,RemotePort,TcpTestSucceeded\"",
            "linux_command": "ping -c 3 -W 2 1.1.1.1 || true",
            "darwin_command": "ping -c 3 -W 2000 1.1.1.1 || true",
            "timeout_seconds": 30,
        },
        {
            "script_id": "system_time",
            "display_name": "System Date And Time",
            "description": "Show the current system date, time, and timezone.",
            "windows_command": "powershell -NoProfile -Command \"Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'\"",
            "linux_command": "date '+%Y-%m-%d %H:%M:%S %Z'",
            "darwin_command": "date '+%Y-%m-%d %H:%M:%S %Z'",
            "timeout_seconds": 30,
        },
        {
            "script_id": "temp_usage",
            "display_name": "Temporary Storage Usage",
            "description": "Show temporary-directory size without deleting any files.",
            "windows_command": "powershell -NoProfile -Command \"Get-ChildItem $env:TEMP -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum | Select-Object Count,Sum\"",
            "linux_command": "du -sh \"${TMPDIR:-/tmp}\"",
            "darwin_command": "du -sh \"${TMPDIR:-/tmp}\"",
            "timeout_seconds": 60,
        },
        {
            "script_id": "service_overview",
            "display_name": "Running Services",
            "description": "List running services for a quick system service overview.",
            "windows_command": "powershell -NoProfile -Command \"Get-Service | Where-Object Status -eq 'Running' | Select-Object Name,DisplayName,Status | Sort-Object Name | Format-Table -AutoSize\"",
            "linux_command": "systemctl list-units --type=service --state=running --no-pager || service --status-all",
            "darwin_command": "launchctl list",
            "timeout_seconds": 60,
        },
    ]

    for script in defaults:
        exists = conn.execute(
            "SELECT script_id FROM scripts WHERE script_id = ?",
            (script["script_id"],),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE scripts SET script_kind = 'command' WHERE script_id = ?",
                (script["script_id"],),
            )
            continue

        now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AS ts").fetchone()["ts"]
        conn.execute(
            """
            INSERT INTO scripts (
                script_id, display_name, description,
                windows_command, linux_command, darwin_command,
                timeout_seconds, enabled, created_at, updated_at,
                script_kind, file_name, script_content, allow_nonzero_success
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'command', NULL, NULL, 0)
            """,
            (
                script["script_id"],
                script["display_name"],
                script["description"],
                script["windows_command"],
                script["linux_command"],
                script["darwin_command"],
                script["timeout_seconds"],
                now,
                now,
            ),
        )


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        # Journal mode is persistent per database file. Setting it on every
        # connection requires an exclusive lock and can fail while other
        # requests are active, so it belongs here, once, at startup.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(BASE_SCHEMA)
        migrate_schema(conn)
        seed_default_scripts(conn)
        conn.commit()
    finally:
        conn.close()
