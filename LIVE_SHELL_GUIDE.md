# Direct Shell Console Guide

## Overview

The Direct Shell Console queues one command at a time for a remote agent. The agent sends stdout and stderr chunks while the command runs, and the browser polls those durable chunks every 500ms. It is not a persistent interactive terminal and does not accept stdin.

## Features

- **Authenticated execution**: Only an admin session can queue commands
- **Command history**: Navigate previous commands using arrow keys
- **Cross-platform support**: Works on Windows (cmd), Linux, and macOS (bash)
- **Live output chunks**: stdout and stderr appear while the command runs
- **Task status polling**: Shows queued, running, and final task states
- **Session management**: Secure authenticated access

## How to Use

### Accessing Direct Shell

1. Log in to the Control Nexus dashboard
2. Navigate to an agent's detail page
3. Click the **"Open Direct Shell"** button
4. The Direct Shell console will open in a new page

### Executing Commands

1. Type your command in the input field
2. Press **Enter** to execute, or **Shift+Enter** for a newline
3. Watch stdout and stderr appear in the execution log
4. Wait for the task status to change to a final state

### Command History

- Press **Arrow Up** to navigate to previous commands
- Press **Arrow Down** to navigate forward in history
- History is maintained for the current session

### Console Controls

- **Clear Output**: Removes all output from the console
- **Copy Output**: Copies all console text to clipboard
- **Back to Agent**: Returns to the agent detail page

## Technical Details

### Architecture

```
Browser (HTTP) <-> Flask Server <-> Agent (HTTP polling)
```

1. User enters a command in the browser
2. Browser sends the command to the server with the session and CSRF token
3. Server creates an `execute_shell` task
4. Agent polls and receives the task
5. Agent sends stdout/stderr chunks while executing
6. Agent reports the final output and status
7. Browser polls the task and displays live and final output

### Task Flow

```
POST /api/shell_execute/<agent_id>
  ↓
Task created with type="execute_shell"
  ↓
Agent polls and receives task
  ↓
Agent executes command
  ↓
POST /api/task-output/<agent_id>/<task_id> (stdout/stderr chunks)
  ↓
Browser polls GET /api/shell_task/<task_id> every 500ms
  ↓
POST /api/report/<agent_id>/<task_id>
```

### Execution Modes

**Windows**:
```
cmd /c <command>
```

**Linux/macOS**:
```
bash -lc <command>
```

## Security Considerations

- Direct shell access requires an authenticated session with `shell.execute` permission
- Commands are executed with the agent's process privileges
- No command validation or sandboxing is performed
- Use with caution on production systems
- Consider implementing command whitelisting for production use

## Limitations

- Commands requiring interactive input are not supported
- Long-running commands may timeout based on agent configuration
- Output delivery depends on the agent's network connection; final output remains available after the task completes
- No terminal emulation (colors, cursor control, etc.)

## Troubleshooting

### No Output Displayed

- Check that the agent is online
- Verify the task reached a final state
- Ensure the agent has network connectivity to the server

### Command Hangs

- The command may be waiting for input, which is not supported
- Use non-interactive flags such as `-y` or `--no-input`
- Check the agent logs for errors

## Examples

### System Information

**Windows**:
```
systeminfo
whoami
ipconfig
```

**Linux/macOS**:
```
uname -a
whoami
ifconfig
```

### File Operations

**Windows**:
```
dir C:\
type C:\file.txt
```

**Linux/macOS**:
```
ls -la /home
cat /etc/hosts
```

### Process Management

**Windows**:
```
tasklist
netstat -ano
```

**Linux/macOS**:
```
ps aux
netstat -tulpn
```

## Best Practices

1. **Test commands first**: Try commands on a test agent before production
2. **Use absolute paths**: Avoid relying on current working directory
3. **Keep commands short**: Break complex operations into multiple commands
4. **Monitor output**: Watch for errors or unexpected behavior
5. **Clear output regularly**: Keep the console readable

## Future Enhancements

Potential improvements for future versions:

- Command whitelisting/blacklisting
- Command approval workflow
- Session recording and audit logs
- Terminal emulation support
- Multi-line command support
- File upload/download integration
- Persistent shell sessions
