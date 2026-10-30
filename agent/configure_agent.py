#!/usr/bin/env python3

import argparse
import getpass
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def read_existing_value(env_path: Path, key: str, default: str) -> str:
    if not env_path.exists():
        return default
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def ask(prompt: str, default: str, hidden: bool = False) -> str:
    label = f"{prompt} [{default}]: " if default else f"{prompt}: "
    value = getpass.getpass(label) if hidden else input(label)
    return value.strip() or default


def validate_server_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SERVER_URL must start with http:// or https:// and include a host.")
    return value.rstrip("/")


def parse_bool(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return 1
    if normalized in {"0", "false", "no", "n"}:
        return 0
    raise ValueError("VERIFY_SSL must be 0 or 1.")


def write_env(path: Path, values: dict[str, str]) -> None:
    content = """# Control Nexus Agent Configuration
SERVER_URL={server_url}
REGISTER_TOKEN={register_token}
POLL_SECONDS={poll_seconds}
REQUEST_TIMEOUT={request_timeout}
INVENTORY_SYNC_SECONDS={inventory_sync_seconds}
VERIFY_SSL={verify_ssl}
""".format(**values)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure a Control Nexus agent for a server.")
    parser.add_argument("--server-url", help="Server URL, for example https://control.example.com")
    parser.add_argument("--register-token", help="Registration token from the server setup")
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument("--request-timeout", type=int, default=None)
    parser.add_argument("--inventory-sync-seconds", type=int, default=None)
    parser.add_argument("--verify-ssl", choices=["0", "1"], help="1 verifies HTTPS certificates")
    parser.add_argument("--keep-state", action="store_true", help="Keep the current agent_state.json")
    parser.add_argument("--start", action="store_true", help="Start agent.py after saving the configuration")
    args = parser.parse_args()

    agent_dir = base_dir()
    env_path = agent_dir / ".env"
    existing_server = read_existing_value(env_path, "SERVER_URL", "http://127.0.0.1:5000")
    existing_token = read_existing_value(env_path, "REGISTER_TOKEN", "")
    existing_poll = read_existing_value(env_path, "POLL_SECONDS", "10")
    existing_timeout = read_existing_value(env_path, "REQUEST_TIMEOUT", "15")
    existing_inventory = read_existing_value(env_path, "INVENTORY_SYNC_SECONDS", "60")
    existing_verify = read_existing_value(env_path, "VERIFY_SSL", "1")

    server_url = args.server_url or ask("Server URL", existing_server)
    try:
        server_url = validate_server_url(server_url)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    register_token = args.register_token or ask("Register token", existing_token, hidden=True)
    if not register_token:
        print("Configuration error: register token is required.")
        return 2

    try:
        poll_seconds = args.poll_seconds if args.poll_seconds is not None else int(ask("Poll seconds", existing_poll))
        request_timeout = args.request_timeout if args.request_timeout is not None else int(ask("Request timeout", existing_timeout))
        inventory_sync_seconds = args.inventory_sync_seconds if args.inventory_sync_seconds is not None else int(ask("Inventory sync seconds", existing_inventory))
        verify_ssl = parse_bool(args.verify_ssl or ask("Verify SSL certificate (0/1)", existing_verify))
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    if min(poll_seconds, request_timeout, inventory_sync_seconds) < 0:
        print("Configuration error: numeric intervals cannot be negative.")
        return 2

    values = {
        "server_url": server_url,
        "register_token": register_token,
        "poll_seconds": poll_seconds,
        "request_timeout": request_timeout,
        "inventory_sync_seconds": inventory_sync_seconds,
        "verify_ssl": verify_ssl,
    }
    write_env(env_path, values)

    state_path = agent_dir / "agent_state.json"
    if not args.keep_state and state_path.exists():
        state_path.unlink()

    print(f"Agent configuration saved to {env_path}")
    print("Defender, antivirus, firewall, and other security settings were not changed.")
    print("Start the agent with: python agent.py")

    if args.start:
        agent_path = agent_dir / "agent.py"
        if not agent_path.exists():
            print(f"Cannot start: {agent_path} does not exist.")
            return 2
        return subprocess.run([sys.executable, str(agent_path)], cwd=agent_dir).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
