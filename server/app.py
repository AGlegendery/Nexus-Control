import json
import os
import re
import secrets
import sqlite3
import uuid
import hashlib
import base64
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    g,
    get_flashed_messages,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_connection, init_db
from task_store import repair_task_file_paths, resolve_task_file, update_task_file, write_task_file
from config_manager import ConfigManager

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR))).expanduser().resolve()
DB_PATH = DATA_DIR / "control.db"
TASKS_DIR = DATA_DIR / "tasks"
AGENT_SOURCE_PATH = Path(
    os.getenv("AGENT_SOURCE_PATH", str(BASE_DIR.parent / "agent" / "agent.py"))
).expanduser()

TASK_STATUSES = {"pending", "running", "completed", "failed", "cancelled", "killed"}
TASK_TYPES = {"run_script", "deploy_script", "run_agent_file", "execute_shell"}
FORM_TASK_TYPES = {"run_script", "deploy_script", "run_agent_file"}
BLOCKED_TASK_TYPES = {"reverse_shell"}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
ROLE_PERMISSIONS = {
    "admin": {
        "dashboard.view", "agent.view", "agent.manage", "group.manage", "schedule.manage",
        "script.manage", "task.create", "task.kill", "task.retry", "shell.execute", "audit.view",
        "health.view", "health.manage", "user.manage",
    },
    "operator": {
        "dashboard.view", "agent.view", "agent.manage", "group.manage", "schedule.manage",
        "script.manage", "task.create", "task.kill", "task.retry", "shell.execute", "health.view",
        "health.manage",
    },
    "viewer": {"dashboard.view", "agent.view", "health.view", "audit.view"},
}
HEALTH_THRESHOLDS = {"cpu_percent": 90.0, "memory_percent": 90.0, "disk_percent": 90.0}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_os_family(raw_os: str | None) -> str:
    candidate = (raw_os or "").strip().lower()
    if candidate in {"windows", "linux", "darwin"}:
        return candidate
    if "win" in candidate:
        return "windows"
    if "mac" in candidate or "darwin" in candidate:
        return "darwin"
    return "linux"


def is_safe_script_id(script_id: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", script_id))


def is_safe_file_name(file_name: str) -> bool:
    if not file_name or len(file_name) > 128:
        return False
    if "/" in file_name or "\\" in file_name:
        return False
    if file_name.endswith((".", " ")):
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", file_name):
        return False
    stem = file_name.split(".", 1)[0].upper()
    return stem not in WINDOWS_RESERVED


def is_supported_script_extension(file_name: str) -> bool:
    suffix = Path(file_name).suffix
    return bool(re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix))


SCRIPT_PLATFORM_LABELS = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}
WINDOWS_SCRIPT_SUFFIXES = {".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".exe", ".msi"}
MACOS_SCRIPT_SUFFIXES = {".command", ".app"}
LINUX_SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh"}


def script_platform_keys(row: Any) -> list[str]:
    """Infer target operating systems from configured commands or the script file type."""
    columns = row.keys() if hasattr(row, "keys") else ()
    kind = str(row["script_kind"] or "").lower() if "script_kind" in columns else ""
    file_name = str(row["file_name"] or "").lower() if "file_name" in columns else ""
    if kind == "file" or file_name:
        suffix = Path(file_name).suffix
        if suffix in WINDOWS_SCRIPT_SUFFIXES:
            return ["windows"]
        if suffix in MACOS_SCRIPT_SUFFIXES:
            return ["macos"]
        if suffix in LINUX_SCRIPT_SUFFIXES:
            return ["linux", "macos"]
        return ["windows", "linux", "macos"]
    targets = []
    for key, column in (
        ("windows", "windows_command"),
        ("linux", "linux_command"),
        ("macos", "darwin_command"),
    ):
        if column in columns and str(row[column] or "").strip():
            targets.append(key)
    return targets or ["windows", "linux", "macos"]


def decorate_scripts(rows: list[Any]) -> list[dict[str, Any]]:
    """Attach platform tags to script rows for templates and filters."""
    decorated = []
    for row in rows:
        item = dict(row)
        keys = script_platform_keys(row)
        item["platform_attr"] = " ".join(keys)
        item["platform_text"] = (
            "Any OS" if len(keys) == 3 else ", ".join(SCRIPT_PLATFORM_LABELS[key] for key in keys)
        )
        item["platform_chips"] = (
            [{"key": "any", "label": "Any OS"}]
            if len(keys) == 3
            else [{"key": key, "label": SCRIPT_PLATFORM_LABELS[key]} for key in keys]
        )
        decorated.append(item)
    return decorated


def uploaded_basename(raw_name: str | None) -> str:
    # Browsers normally send a bare name, but some clients send a full path
    # with either separator. Path().name only understands the local platform.
    return (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()


AGENT_SOURCE_MARKER = 'load_env_file(BASE_DIR / ".env")'
PLACEHOLDER_REGISTER_TOKENS = {
    "", "change-me-register-token", "change-me", "changeme",
    "replace-with-strong-registration-token",
}
DEFAULT_AGENT_POLL_SECONDS = 10


def normalize_agent_server_url(raw_url: str) -> str:
    candidate = (raw_url or "").strip().rstrip("/")
    if not candidate:
        raise ValueError("Server URL is required.")
    if len(candidate) > 300:
        raise ValueError("Server URL is too long.")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("Server URL cannot contain spaces.")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Server URL must start with http:// or https:// and include a host.")
    if parsed.username or parsed.password:
        raise ValueError("Server URL must not contain credentials.")
    return candidate


def normalize_agent_poll_seconds(raw_value: Any) -> int:
    try:
        return min(3600, max(1, int(raw_value)))
    except (TypeError, ValueError):
        return DEFAULT_AGENT_POLL_SECONDS


def mask_secret(value: str) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def load_agent_source() -> str:
    try:
        return AGENT_SOURCE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        abort(500, description=f"Agent source is unavailable: {exc}")


def build_agent_bundle(
    source: str,
    server_url: str,
    register_token: str,
    poll_seconds: int,
    verify_ssl: bool,
) -> str:
    if AGENT_SOURCE_MARKER not in source:
        abort(500, description="Agent source layout is not supported.")
    profile = (
        f"{AGENT_SOURCE_MARKER}\n\n"
        "# --- Control Nexus deployment profile (generated by the web panel) ---\n"
        f"os.environ[\"SERVER_URL\"] = {json.dumps(server_url)}\n"
        f"os.environ[\"REGISTER_TOKEN\"] = {json.dumps(register_token)}\n"
        f"os.environ[\"POLL_SECONDS\"] = {json.dumps(str(poll_seconds))}\n"
        f"os.environ[\"VERIFY_SSL\"] = {json.dumps('1' if verify_ssl else '0')}\n"
        "# --- end deployment profile ---\n"
    )
    return source.replace(AGENT_SOURCE_MARKER, profile, 1)


def normalize_timeout_value(raw_value: str, default_value: int) -> int:
    try:
        parsed = int(raw_value)
    except ValueError:
        return default_value
    if parsed == 0:
        return 0
    if parsed < 0:
        return -1
    return max(5, min(parsed, 86400))


def get_upload_limit_bytes() -> int:
    try:
        limit_mb = int(os.getenv("UPLOAD_MAX_MB", "20"))
    except ValueError:
        limit_mb = 20
    return max(1, limit_mb) * 1024 * 1024


def decode_script_bytes(script_row: sqlite3.Row) -> bytes:
    raw_content = script_row["script_content"] or ""
    encoding = (script_row["content_encoding"] or "utf8").strip().lower()
    if encoding == "base64":
        return base64.b64decode(raw_content.encode("ascii"))
    return raw_content.encode("utf-8")


def insert_task_record(
    conn: sqlite3.Connection,
    task_payload: dict[str, Any],
    task_file: Path,
    source_type: str = "manual",
    schedule_id: str | None = None,
    root_task_id: str | None = None,
    attempt_number: int = 1,
    max_attempts: int = 1,
    created_by: str | None = None,
) -> None:
    task_id = str(task_payload["task_id"])
    meta = task_payload.get("meta")
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, agent_id, type, value, status, created_at, updated_at,
            task_file, meta_json, source_type, schedule_id, root_task_id,
            attempt_number, max_attempts, created_by
        )
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            task_payload["agent_id"],
            task_payload["type"],
            task_payload["value"],
            task_payload["created_at"],
            task_payload["updated_at"],
            str(task_file),
            json.dumps(meta) if meta else None,
            source_type,
            schedule_id,
            root_task_id or task_id,
            attempt_number,
            max_attempts,
            created_by,
        ),
    )


def record_audit(
    conn: sqlite3.Connection,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    success: bool = True,
    remote_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (
            occurred_at, actor_type, actor_id, action, resource_type,
            resource_id, success, remote_address, details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            actor_type,
            actor_id,
            action,
            resource_type,
            resource_id,
            1 if success else 0,
            remote_address,
            json.dumps(details) if details else None,
        ),
    )


def mark_offline_agents_db(conn: sqlite3.Connection, offline_after_seconds: int, tasks_dir: Path) -> None:
    now = datetime.now(UTC)
    rows = conn.execute("SELECT agent_id, last_seen, status FROM agents").fetchall()
    for row in rows:
        if not row["last_seen"]:
            continue
        try:
            last_seen = datetime.fromisoformat(row["last_seen"])
        except (TypeError, ValueError):
            continue
        is_online = (now - last_seen).total_seconds() <= offline_after_seconds
        new_status = "online" if is_online else "offline"
        if row["status"] != new_status:
            conn.execute(
                "UPDATE agents SET status = ?, updated_at = ? WHERE agent_id = ?",
                (new_status, utc_now(), row["agent_id"]),
            )
        if new_status == "offline":
            open_alert = conn.execute(
                "SELECT alert_id FROM health_alerts WHERE agent_id = ? AND alert_type = 'offline' AND status IN ('open', 'acknowledged')",
                (row["agent_id"],),
            ).fetchone()
            if not open_alert:
                conn.execute(
                    """
                    INSERT INTO health_alerts (
                        agent_id, alert_type, severity, status, message, opened_at
                    ) VALUES (?, 'offline', 'critical', 'open', ?, ?)
                    """,
                    (row["agent_id"], "Agent has not checked in recently.", utc_now()),
                )
        else:
            conn.execute(
                """
                UPDATE health_alerts
                SET status = 'resolved', resolved_at = ?
                WHERE agent_id = ? AND alert_type = 'offline' AND status IN ('open', 'acknowledged')
                """,
                (utc_now(), row["agent_id"]),
            )

    stale_tasks = conn.execute(
        """
        SELECT tasks.task_id, tasks.task_file
        FROM tasks
        JOIN agents ON agents.agent_id = tasks.agent_id
        WHERE tasks.status = 'running' AND agents.status = 'offline'
        """
    ).fetchall()
    for task in stale_tasks:
        now_text = utc_now()
        error = "Agent stopped reporting while the task was running."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (error, now_text, now_text, task["task_id"]),
        )
        update_task_file(
            task["task_file"],
            {"status": "failed", "error": error, "completed_at": now_text, "updated_at": now_text},
            tasks_dir,
        )
    conn.commit()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def upsert_agent_telemetry(
    conn: sqlite3.Connection,
    agent_id: str,
    system: dict[str, Any],
    health: dict[str, Any],
) -> None:
    now_text = utc_now()
    raw_system = json.dumps(system)
    raw_health = json.dumps(health)
    conn.execute(
        """
        INSERT INTO agent_system_info (
            agent_id, reported_at, agent_version, architecture, cpu_model,
            logical_cpu_count, memory_total_bytes, disk_total_bytes, boot_time,
            ip_addresses_json, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            reported_at = excluded.reported_at,
            agent_version = excluded.agent_version,
            architecture = excluded.architecture,
            cpu_model = excluded.cpu_model,
            logical_cpu_count = excluded.logical_cpu_count,
            memory_total_bytes = excluded.memory_total_bytes,
            disk_total_bytes = excluded.disk_total_bytes,
            boot_time = excluded.boot_time,
            ip_addresses_json = excluded.ip_addresses_json,
            raw_json = excluded.raw_json
        """,
        (
            agent_id,
            now_text,
            str(system.get("agent_version", ""))[:100],
            str(system.get("architecture", ""))[:100],
            str(system.get("cpu_model", ""))[:300],
            _integer(system.get("logical_cpu_count")),
            _integer(system.get("memory_total_bytes")),
            _integer(system.get("disk_total_bytes")),
            str(system.get("boot_time", ""))[:100],
            json.dumps(system.get("ip_addresses", [])),
            raw_system,
        ),
    )
    conn.execute(
        """
        INSERT INTO agent_health (
            agent_id, reported_at, cpu_percent, memory_percent, memory_used_bytes,
            disk_percent, load_1m, uptime_seconds, battery_percent, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            reported_at = excluded.reported_at,
            cpu_percent = excluded.cpu_percent,
            memory_percent = excluded.memory_percent,
            memory_used_bytes = excluded.memory_used_bytes,
            disk_percent = excluded.disk_percent,
            load_1m = excluded.load_1m,
            uptime_seconds = excluded.uptime_seconds,
            battery_percent = excluded.battery_percent,
            raw_json = excluded.raw_json
        """,
        (
            agent_id,
            now_text,
            _number(health.get("cpu_percent")),
            _number(health.get("memory_percent")),
            _integer(health.get("memory_used_bytes")),
            _number(health.get("disk_percent")),
            _number(health.get("load_1m")),
            _integer(health.get("uptime_seconds")),
            _number(health.get("battery_percent")),
            raw_health,
        ),
    )
    metric_names = {
        "cpu_percent": "CPU usage",
        "memory_percent": "Memory usage",
        "disk_percent": "Disk usage",
    }
    for metric, label in metric_names.items():
        observed = _number(health.get(metric))
        threshold = HEALTH_THRESHOLDS[metric]
        existing = conn.execute(
            "SELECT alert_id FROM health_alerts WHERE agent_id = ? AND alert_type = ? AND status IN ('open', 'acknowledged')",
            (agent_id, metric),
        ).fetchone()
        if observed is not None and observed >= threshold:
            message = f"{label} is {observed:.1f}%, threshold {threshold:.0f}%."
            if existing:
                conn.execute(
                    "UPDATE health_alerts SET observed_value = ?, threshold_value = ?, message = ? WHERE alert_id = ?",
                    (observed, threshold, message, existing["alert_id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO health_alerts (
                        agent_id, alert_type, severity, status, message,
                        observed_value, threshold_value, opened_at
                    ) VALUES (?, ?, 'warning', 'open', ?, ?, ?, ?)
                    """,
                    (agent_id, metric, message, observed, threshold, now_text),
                )
        elif existing:
            conn.execute(
                "UPDATE health_alerts SET status = 'resolved', resolved_at = ? WHERE alert_id = ? AND status IN ('open', 'acknowledged')",
                (now_text, existing["alert_id"]),
            )
    conn.commit()


def dispatch_due_schedules(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE enabled = 1 AND next_run_at <= ?
        ORDER BY next_run_at ASC
        """,
        (now.replace(microsecond=0).isoformat(),),
    ).fetchall()
    for schedule in rows:
        try:
            interval_minutes = max(1, min(int(schedule["interval_minutes"]), 10080))
            schedule_timeout = int(schedule["timeout_seconds"])
        except (TypeError, ValueError):
            interval_minutes = 60
            schedule_timeout = 180
        next_run = (now + timedelta(minutes=interval_minutes)).replace(microsecond=0).isoformat()
        claim = conn.execute(
            """
            UPDATE scheduled_tasks
            SET next_run_at = ?, last_run_at = ?, updated_at = ?
            WHERE schedule_id = ? AND enabled = 1 AND next_run_at = ?
            """,
            (next_run, utc_now(), utc_now(), schedule["schedule_id"], schedule["next_run_at"]),
        )
        if claim.rowcount != 1:
            continue
        if schedule["target_type"] == "group":
            agent_rows = conn.execute(
                "SELECT agent_id FROM agent_group_members WHERE group_id = ? ORDER BY agent_id",
                (schedule["target_id"],),
            ).fetchall()
        else:
            agent_rows = conn.execute(
                "SELECT agent_id FROM agents WHERE agent_id = ?",
                (schedule["target_id"],),
            ).fetchall()
        dispatched = 0
        for agent_row in agent_rows:
            task_id = str(uuid.uuid4())
            now_text = utc_now()
            payload = {
                "task_id": task_id,
                "agent_id": agent_row["agent_id"],
                "type": schedule["task_type"],
                "value": schedule["task_value"],
                "status": "pending",
                "created_at": now_text,
                "updated_at": now_text,
            }
            if schedule["task_type"] in {"run_agent_file", "execute_shell"}:
                payload["meta"] = {
                    "timeout_seconds": schedule_timeout,
                    "allow_nonzero_success": int(schedule["allow_nonzero_success"]),
                }
                if schedule["task_type"] == "execute_shell":
                    payload["meta"]["shell_command"] = schedule["task_value"]
            try:
                task_file = write_task_file(TASKS_DIR, payload)
                insert_task_record(
                    conn,
                    payload,
                    task_file,
                    source_type="schedule",
                    schedule_id=schedule["schedule_id"],
                    root_task_id=task_id,
                    attempt_number=1,
                    max_attempts=1,
                    created_by=schedule["created_by"],
                )
            except (OSError, sqlite3.Error) as exc:
                # One bad target or sidecar file must not block other schedules.
                print(f"Schedule {schedule['schedule_id']} dispatch failed: {exc}")
                continue
            dispatched += 1
        record_audit(
            conn,
            "scheduler",
            None,
            "schedule.dispatched",
            "schedule",
            schedule["schedule_id"],
            True,
            details={"task_count": dispatched},
        )
    conn.commit()


def start_scheduler(app: Flask) -> None:
    if app.extensions.get("scheduler_started"):
        return
    app.extensions["scheduler_started"] = True
    interval = max(5, int(app.config.get("SCHEDULER_INTERVAL_SECONDS", 15)))

    def scheduler_loop() -> None:
        while True:
            conn = get_connection(DB_PATH)
            try:
                mark_offline_agents_db(conn, app.config["OFFLINE_AFTER_SECONDS"], TASKS_DIR)
                dispatch_due_schedules(conn)
            except Exception as exc:
                conn.rollback()
                print(f"Background scheduler error: {exc}")
            finally:
                conn.close()
            time.sleep(interval)

    threading.Thread(target=scheduler_loop, daemon=True).start()


def create_app() -> Flask:
    app = Flask(__name__)
    # PaaS platforms (Railway, Render, Fly) terminate TLS on their proxy.
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "1" if os.getenv("PORT") else "0") == "1"
    if trust_proxy_headers:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()
    if secret_key in {"", "change-this-production-secret", "replace-with-random-hex"}:
        # Never run with the public default: it would allow forged admin sessions.
        secret_key = secrets.token_urlsafe(48)
        print("FLASK_SECRET_KEY is unset or default; using a random per-process key. Set it to keep sessions across restarts.")
    app.config["SECRET_KEY"] = secret_key
    app.config["REGISTER_TOKEN"] = os.getenv("REGISTER_TOKEN", "change-me-register-token")
    try:
        offline_after_seconds = int(os.getenv("OFFLINE_AFTER_SECONDS", "35"))
    except ValueError:
        offline_after_seconds = 35
    app.config["OFFLINE_AFTER_SECONDS"] = max(5, offline_after_seconds)
    app.config["ADMIN_USERNAME"] = os.getenv("ADMIN_USERNAME", "admin")
    app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "change-this-now")
    app.config["ADMIN_PASSWORD_HASH"] = os.getenv("ADMIN_PASSWORD_HASH", "")
    upload_limit_bytes = get_upload_limit_bytes()
    try:
        max_content_length = int(os.getenv("MAX_CONTENT_LENGTH", str(upload_limit_bytes)))
    except ValueError:
        max_content_length = upload_limit_bytes
    app.config["MAX_CONTENT_LENGTH"] = max(1, max_content_length)
    app.config["UPLOAD_MAX_BYTES"] = min(upload_limit_bytes, app.config["MAX_CONTENT_LENGTH"])
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv(
        "SESSION_COOKIE_SECURE", "1" if os.getenv("PORT") else "0"
    ) == "1"
    app.config["RATE_LIMIT_ENABLED"] = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"
    try:
        rate_limit_per_minute = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "300")))
    except ValueError:
        rate_limit_per_minute = 300
    app.config["RATE_LIMIT_PER_MINUTE"] = rate_limit_per_minute
    app.config["SCHEDULER_ENABLED"] = os.getenv("SCHEDULER_ENABLED", "1") == "1"
    try:
        scheduler_interval = max(5, int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "15")))
    except ValueError:
        scheduler_interval = 15
    app.config["SCHEDULER_INTERVAL_SECONDS"] = scheduler_interval

    cors_origins = os.getenv("CORS_ORIGINS", "").strip()
    socketio = SocketIO(app, cors_allowed_origins=cors_origins or None, async_mode="threading")
    
    # Rate limiting
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[f"{app.config['RATE_LIMIT_PER_MINUTE']}/minute"] if app.config["RATE_LIMIT_ENABLED"] else [],
        storage_uri="memory://",
    )

    @app.get("/healthz")
    @limiter.exempt
    def healthz() -> Any:
        try:
            g.db.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return jsonify({"status": "error", "database": "unavailable"}), 503
        return jsonify({"status": "ok"})
    
    # Validate configuration on startup
    validation = ConfigManager.validate_config()
    if not validation["valid"]:
        print("\n⚠️  SECURITY WARNING: Configuration issues detected!")
        for issue in validation["issues"]:
            print(f"   ❌ {issue}")
        print("\n   Run setup_wizard.py to generate secure configuration.\n")
    if validation["warnings"]:
        for warning in validation["warnings"]:
            print(f"   ⚠️  {warning}")

    init_db(DB_PATH)
    connection = get_connection(DB_PATH)
    try:
        repair_task_file_paths(connection, TASKS_DIR)
        admin_user = connection.execute(
            "SELECT user_id, password_hash FROM users WHERE username = ?",
            (app.config["ADMIN_USERNAME"],),
        ).fetchone()
        password_hash = app.config["ADMIN_PASSWORD_HASH"]
        if not admin_user and not password_hash:
            setting = connection.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = 'admin_password_hash'"
            ).fetchone()
            password_hash = setting["setting_value"] if setting else ""

        hosted = bool(os.getenv("PORT")) or os.getenv("SERVER_HOST", "").strip().lower() not in {
            "", "127.0.0.1", "localhost", "::1",
        }
        if hosted:
            insecure_settings = []
            if app.config["REGISTER_TOKEN"].strip().lower() in PLACEHOLDER_REGISTER_TOKENS:
                insecure_settings.append("REGISTER_TOKEN")
            if not admin_user and not password_hash and app.config["ADMIN_PASSWORD"] in {
                "", "change-this-now", "replace-with-strong-password",
            }:
                insecure_settings.append("ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH)")
            if admin_user:
                try:
                    stored_default = any(
                        check_password_hash(admin_user["password_hash"], value)
                        for value in ("change-this-now", "replace-with-strong-password")
                    )
                except ValueError:
                    stored_default = False
                if stored_default:
                    insecure_settings.append("ADMIN_PASSWORD (stored password is a public default)")
            if insecure_settings:
                raise SystemExit(
                    "Refusing to start with default credentials while listening on a network interface: "
                    + ", ".join(insecure_settings)
                    + ". Set these environment variables (see .env.example) and restart."
                )

        if not admin_user:
            if not password_hash:
                password_hash = generate_password_hash(app.config["ADMIN_PASSWORD"])
            now_text = utc_now()
            connection.execute(
                """
                INSERT INTO users (
                    user_id, username, password_hash, role, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'admin', 1, ?, ?)
                """,
                (str(uuid.uuid4()), app.config["ADMIN_USERNAME"], password_hash, now_text, now_text),
            )
            connection.commit()
    finally:
        connection.close()

    def has_permission(permission: str) -> bool:
        role = str(session.get("role", "admin" if session.get("admin_authenticated") else ""))
        return permission in ROLE_PERMISSIONS.get(role, set())

    @app.context_processor
    def inject_template_helpers() -> dict[str, Any]:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {"csrf_token": lambda: token, "can": has_permission, "current_role": session.get("role", "")}

    @app.before_request
    def open_db_connection() -> None:
        if request.endpoint == "static":
            return
        g.db = get_connection(DB_PATH)

    csrf_exempt_endpoints = {
        "register_agent",
        "sync_inventory",
        "heartbeat_agent",
        "task_control",
        "poll_task",
        "report_task",
        "task_output",
    }

    @app.before_request
    def validate_csrf_token() -> None:
        if request.method != "POST" or request.endpoint in csrf_exempt_endpoints:
            return
        expected = session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRFToken", "") or request.form.get("csrf_token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Invalid CSRF token.")

    @app.teardown_request
    def close_db_connection(exc: Exception | None) -> None:
        conn = g.pop("db", None)
        if conn:
            conn.close()

    @app.after_request
    def audit_request(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if request.endpoint in {"heartbeat_agent", "task_output"}:
            return response
        conn = g.get("db")
        if conn is None or request.method != "POST" or not request.endpoint:
            return response
        try:
            actor_type = "agent" if request.headers.get("X-Agent-Token") else "admin"
            actor_id = request.view_args.get("agent_id") if actor_type == "agent" else session.get("user_id")
            record_audit(
                conn,
                actor_type,
                actor_id,
                request.endpoint,
                "http",
                request.path,
                response.status_code < 400,
                request.remote_addr,
                {"method": request.method, "status_code": response.status_code},
            )
            conn.commit()
        except sqlite3.Error as exc:
            # Auditing must never turn a successful request into a 500.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            print(f"Audit write failed for {request.path}: {exc}")
        return response

    @app.after_request
    def async_form_response(response: Any) -> Any:
        """Translate form redirects into JSON for fetch-based panel actions."""
        if request.method != "POST" or request.headers.get("X-Requested-With") != "fetch":
            return response
        messages = [
            {"category": category, "message": message}
            for category, message in get_flashed_messages(with_categories=True)
        ]
        if response.status_code in {301, 302, 303, 307, 308}:
            return jsonify(
                {
                    "ok": True,
                    "redirect": response.headers.get("Location", request.url),
                    "messages": messages,
                }
            )
        if response.status_code >= 400:
            error_text = re.sub(r"<[^>]+>", " ", response.get_data(as_text=True))
            error_text = re.sub(r"\s+", " ", error_text).strip()[:300]
            error_response = jsonify(
                {
                    "ok": False,
                    "error": error_text or f"Request failed with status {response.status_code}.",
                    "messages": messages,
                }
            )
            error_response.status_code = response.status_code
            return error_response
        return response

    @app.errorhandler(413)
    def request_too_large(error: Exception) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Request exceeds the configured upload limit."}), 413
        return "Request exceeds the configured upload limit.", 413

    def execute(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cursor = g.db.execute(query, params)
        g.db.commit()
        return cursor

    def get_setting(key: str) -> str | None:
        row = g.db.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            (key,),
        ).fetchone()
        return row["setting_value"] if row else None

    def set_setting(key: str, value: str) -> None:
        now = utc_now()
        execute(
            """
            INSERT INTO app_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )

    def save_file_script_content(
        script_id: str,
        display_name: str,
        description: str,
        file_name: str,
        script_bytes: bytes,
        timeout_seconds: int,
        allow_nonzero_success: int,
    ) -> None:
        now = utc_now()
        content_size = len(script_bytes)
        content_sha256 = hashlib.sha256(script_bytes).hexdigest()
        content_encoding = "base64"
        content_payload = base64.b64encode(script_bytes).decode("ascii")
        existing = g.db.execute("SELECT script_id FROM scripts WHERE script_id = ?", (script_id,)).fetchone()
        if existing:
            execute(
                """
                UPDATE scripts
                SET display_name = ?, description = ?, windows_command = '', linux_command = '',
                    darwin_command = '', timeout_seconds = ?, enabled = 1, updated_at = ?,
                    script_kind = 'file', file_name = ?, script_content = ?, content_encoding = ?,
                    content_size = ?, content_sha256 = ?, allow_nonzero_success = ?
                WHERE script_id = ?
                """,
                (
                    display_name,
                    description,
                    timeout_seconds,
                    now,
                    file_name,
                    content_payload,
                    content_encoding,
                    content_size,
                    content_sha256,
                    allow_nonzero_success,
                    script_id,
                ),
            )
            flash(f"File script '{script_id}' updated.", "success")
        else:
            execute(
                """
                INSERT INTO scripts (
                    script_id, display_name, description, windows_command, linux_command,
                    darwin_command, timeout_seconds, enabled, created_at, updated_at,
                    script_kind, file_name, script_content, content_encoding, content_size,
                    content_sha256, allow_nonzero_success
                )
                VALUES (?, ?, ?, '', '', '', ?, 1, ?, ?, 'file', ?, ?, ?, ?, ?, ?)
                """,
                (
                    script_id,
                    display_name,
                    description,
                    timeout_seconds,
                    now,
                    now,
                    file_name,
                    content_payload,
                    content_encoding,
                    content_size,
                    content_sha256,
                    allow_nonzero_success,
                ),
            )
            flash(f"File script '{script_id}' added.", "success")

    def login_required(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            if not session.get("admin_authenticated") and not session.get("user_id"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                flash("Please log in to access the control dashboard.", "warning")
                return redirect(url_for("login", next=request.path))
            return view_func(*args, **kwargs)

        return wrapped_view

    def permission_required(permission: str):
        def decorator(view_func):
            @wraps(view_func)
            @login_required
            def wrapped_view(*args, **kwargs):
                if not has_permission(permission):
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "forbidden"}), 403
                    flash("You do not have permission to perform that action.", "danger")
                    return redirect(request.referrer or url_for("dashboard"))
                return view_func(*args, **kwargs)

            return wrapped_view

        return decorator

    @app.get("/uploads")
    @permission_required("script.manage")
    def upload_page() -> Any:
        scripts = g.db.execute(
            """
            SELECT script_id, display_name, description, timeout_seconds, enabled,
                   script_kind, file_name, windows_command, linux_command, darwin_command
            FROM scripts
            ORDER BY display_name COLLATE NOCASE
            """
        ).fetchall()
        return render_template(
            "upload.html",
            scripts=decorate_scripts(scripts),
            upload_limit_mb=app.config["UPLOAD_MAX_BYTES"] // (1024 * 1024),
        )

    @app.get("/agent")
    @permission_required("agent.manage")
    def agent_download_page() -> Any:
        default_url = request.url_root.rstrip("/")
        raw_url = request.args.get("url")
        requested_url = default_url if raw_url is None else raw_url
        poll_seconds = normalize_agent_poll_seconds(request.args.get("poll", DEFAULT_AGENT_POLL_SECONDS))
        raw_ssl = request.args.get("ssl")
        verify_ssl = (raw_ssl == "1") if raw_ssl is not None else requested_url.startswith("https://")
        config_error = ""
        try:
            server_url = normalize_agent_server_url(requested_url)
        except ValueError as exc:
            config_error = str(exc)
            server_url = normalize_agent_server_url(default_url)
        register_token = app.config["REGISTER_TOKEN"]
        source = load_agent_source()
        agent_source = build_agent_bundle(source, server_url, register_token, poll_seconds, verify_ssl)
        response = make_response(
            render_template(
                "agent_download.html",
                page_title="Deploy Agent",
                server_url=server_url,
                poll_seconds=poll_seconds,
                verify_ssl=verify_ssl,
                register_token_masked=mask_secret(register_token),
                token_is_placeholder=register_token.strip().lower() in PLACEHOLDER_REGISTER_TOKENS,
                insecure_url=server_url.startswith("http://"),
                config_error=config_error,
                agent_source=agent_source,
                agent_source_name=AGENT_SOURCE_PATH.name,
                agent_line_count=len(agent_source.splitlines()),
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/agent/download")
    @permission_required("agent.manage")
    def agent_download() -> Any:
        raw_url = request.args.get("url") or request.url_root
        try:
            server_url = normalize_agent_server_url(raw_url)
        except ValueError as exc:
            abort(400, description=str(exc))
        poll_seconds = normalize_agent_poll_seconds(request.args.get("poll", DEFAULT_AGENT_POLL_SECONDS))
        raw_ssl = request.args.get("ssl")
        verify_ssl = (raw_ssl == "1") if raw_ssl is not None else server_url.startswith("https://")
        bundle = build_agent_bundle(
            load_agent_source(),
            server_url,
            app.config["REGISTER_TOKEN"],
            poll_seconds,
            verify_ssl,
        )
        response = make_response(bundle)
        response.headers["Content-Type"] = "text/x-python; charset=utf-8"
        response.headers["Content-Disposition"] = 'attachment; filename="agent.py"'
        response.headers["Cache-Control"] = "no-store"
        try:
            record_audit(
                g.db,
                "admin",
                session.get("user_id"),
                "agent.download",
                "agent_bundle",
                server_url,
                True,
                request.remote_addr,
                {"poll_seconds": poll_seconds, "verify_ssl": verify_ssl},
            )
            g.db.commit()
        except sqlite3.Error:
            g.db.rollback()
        return response

    @app.post("/api/uploads/script")
    @permission_required("script.manage")
    def upload_script_api() -> Any:
        script_id = request.form.get("script_id", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        description = request.form.get("description", "").strip()
        file_name = request.form.get("file_name", "").strip()
        timeout_seconds_raw = request.form.get("timeout_seconds", "120").strip()
        allow_nonzero_success = 1 if request.form.get("allow_nonzero_success") == "on" else 0
        uploaded_file = request.files.get("script_upload")

        if not uploaded_file or not uploaded_file.filename:
            return jsonify({"ok": False, "error": "Upload file is required."}), 400
        if not is_safe_script_id(script_id):
            return jsonify({"ok": False, "error": "Invalid script_id."}), 400
        if not display_name:
            return jsonify({"ok": False, "error": "Display name is required."}), 400

        inferred_name = uploaded_basename(uploaded_file.filename)
        file_name = file_name or inferred_name
        if not is_safe_file_name(file_name) or not is_supported_script_extension(file_name):
            return jsonify({"ok": False, "error": "Unsafe or unsupported file name."}), 400

        script_bytes = uploaded_file.read()
        if not script_bytes:
            return jsonify({"ok": False, "error": "Uploaded file is empty."}), 400
        if len(script_bytes) > app.config["UPLOAD_MAX_BYTES"]:
            return jsonify(
                {
                    "ok": False,
                    "error": f"File too large. Limit is {app.config['UPLOAD_MAX_BYTES'] // (1024 * 1024)}MB.",
                }
            ), 413

        timeout_seconds = normalize_timeout_value(timeout_seconds_raw, 120)
        if timeout_seconds < 0:
            return jsonify({"ok": False, "error": "Timeout must be 0 or between 5 and 86400 seconds."}), 400
        save_file_script_content(
            script_id,
            display_name,
            description,
            file_name,
            script_bytes,
            timeout_seconds,
            allow_nonzero_success,
        )
        return jsonify(
            {
                "ok": True,
                "script_id": script_id,
                "file_name": file_name,
                "content_size": len(script_bytes),
                "sha256": hashlib.sha256(script_bytes).hexdigest(),
            }
        )

    @app.get("/api/uploads/debug-summary")
    @permission_required("script.manage")
    def upload_debug_summary() -> Any:
        rows = g.db.execute(
            """
            SELECT script_id, file_name, content_encoding, content_size, content_sha256, updated_at
            FROM scripts
            WHERE script_kind = 'file'
            ORDER BY updated_at DESC
            LIMIT 200
            """
        ).fetchall()
        total_size = sum(int(row["content_size"] or 0) for row in rows)
        base64_count = sum(1 for row in rows if (row["content_encoding"] or "").lower() == "base64")
        agents_count = g.db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        tasks_count = g.db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        tasks_pending = g.db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending'").fetchone()["c"]
        tasks_running = g.db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'running'").fetchone()["c"]
        tasks_failed = g.db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'failed'").fetchone()["c"]
        scripts_total = g.db.execute("SELECT COUNT(*) AS c FROM scripts").fetchone()["c"]
        return jsonify(
            {
                "upload_limit_bytes": app.config["UPLOAD_MAX_BYTES"],
                "max_content_length": app.config["MAX_CONTENT_LENGTH"],
                "file_script_count": len(rows),
                "base64_encoded_count": base64_count,
                "total_script_bytes": total_size,
                "system": {
                    "agents_count": agents_count,
                    "tasks_count": tasks_count,
                    "tasks_pending": tasks_pending,
                    "tasks_running": tasks_running,
                    "tasks_failed": tasks_failed,
                    "scripts_total": scripts_total,
                },
                "items": [dict(row) for row in rows],
                "server_time": utc_now(),
            }
        )

    def is_agent_authenticated(agent_id: str, token: str | None) -> bool:
        if not token:
            return False
        row = g.db.execute(
            "SELECT agent_id FROM agents WHERE agent_id = ? AND token = ?",
            (agent_id, token),
        ).fetchone()
        return row is not None

    def verify_admin_password(password: str, username: str | None = None) -> bool:
        login_username = username or app.config["ADMIN_USERNAME"]
        user = g.db.execute(
            "SELECT password_hash, enabled FROM users WHERE username = ?",
            (login_username,),
        ).fetchone()
        if user:
            if not user["enabled"]:
                return False
            return check_password_hash(user["password_hash"], password)
        if login_username != app.config["ADMIN_USERNAME"]:
            return False
        db_hash = get_setting("admin_password_hash")
        if db_hash:
            return check_password_hash(db_hash, password)
        env_hash = app.config["ADMIN_PASSWORD_HASH"]
        if env_hash:
            return check_password_hash(env_hash, password)
        return secrets.compare_digest(password, app.config["ADMIN_PASSWORD"])

    def touch_agent(agent_id: str) -> None:
        now = utc_now()
        execute(
            """
            UPDATE agents
            SET status = 'online', last_seen = ?, updated_at = ?
            WHERE agent_id = ?
            """,
            (now, now, agent_id),
        )

    offline_check_lock = threading.Lock()
    offline_check_state = {"at": 0.0}

    def mark_offline_agents() -> None:
        # Page loads used to rescan every agent and stale task on each request.
        # The scheduler already does this; throttle the inline sweep to 5s.
        now_ts = time.time()
        if now_ts - offline_check_state["at"] < 5.0:
            return
        with offline_check_lock:
            if now_ts - offline_check_state["at"] < 5.0:
                return
            offline_check_state["at"] = now_ts
            mark_offline_agents_db(g.db, app.config["OFFLINE_AFTER_SECONDS"], TASKS_DIR)

    def parse_meta(meta_json: str | None) -> dict[str, Any]:
        if not meta_json:
            return {}
        try:
            payload = json.loads(meta_json)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}

    def queue_task(
        agent_id: str,
        task_type: str,
        task_value: str,
        meta: dict[str, Any] | None = None,
        source_type: str = "manual",
        schedule_id: str | None = None,
        root_task_id: str | None = None,
        attempt_number: int = 1,
        max_attempts: int = 1,
        created_by: str | None = None,
    ) -> str:
        now = utc_now()
        task_id = str(uuid.uuid4())
        task_payload: dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "type": task_type,
            "value": task_value,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
        if meta:
            task_payload["meta"] = meta
        task_payload["lineage"] = {
            "source_type": source_type,
            "schedule_id": schedule_id,
            "root_task_id": root_task_id or task_id,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }

        task_file = write_task_file(TASKS_DIR, task_payload)
        insert_task_record(
            g.db,
            task_payload,
            task_file,
            source_type,
            schedule_id,
            root_task_id,
            attempt_number,
            max_attempts,
            created_by,
        )
        g.db.commit()
        return task_id

    def fail_task(task: sqlite3.Row, error: str) -> str:
        now = utc_now()
        execute(
            """
            UPDATE tasks
            SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (error, now, now, task["task_id"]),
        )
        update_task_file(
            task["task_file"],
            {
                "status": "failed",
                "error": error,
                "completed_at": now,
                "updated_at": now,
            },
            TASKS_DIR,
        )
        return now

    def get_os_command(script_row: sqlite3.Row, os_family: str) -> str:
        if os_family == "windows":
            return script_row["windows_command"]
        if os_family == "darwin":
            return script_row["darwin_command"]
        return script_row["linux_command"]

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if session.get("admin_authenticated") or session.get("user_id"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if verify_admin_password(password, username):
                user = g.db.execute(
                    "SELECT user_id, role FROM users WHERE username = ? AND enabled = 1",
                    (username,),
                ).fetchone()
                session.clear()
                session["admin_authenticated"] = True
                session["admin_username"] = username
                session["user_id"] = user["user_id"] if user else None
                session["role"] = user["role"] if user else "admin"
                session["csrf_token"] = secrets.token_urlsafe(32)
                if user:
                    execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?", (utc_now(), utc_now(), user["user_id"]))
                flash("Welcome back. Admin session started.", "success")
                next_url = request.args.get("next", "")
                if not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
                    next_url = url_for("dashboard")
                return redirect(next_url)

            flash("Invalid username or password.", "danger")

        return render_template("login.html")

    # Apply rate limiting to login. The decorator must wrap the function registered
    # with Flask: reassigning the local name after @app.route does not alter
    # app.view_functions, so the limiter was previously a no-op.
    if app.config["RATE_LIMIT_ENABLED"]:
        app.view_functions["login"] = limiter.limit("10/minute")(app.view_functions["login"])

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        session.clear()
        flash("Logged out successfully.", "success")
        return redirect(url_for("login"))

    @app.get("/health")
    @permission_required("health.view")
    def health_page() -> Any:
        mark_offline_agents()
        agents = g.db.execute(
            """
            SELECT agents.agent_id, agents.hostname, agents.status, agents.last_seen,
                   agent_system_info.architecture, agent_system_info.cpu_model,
                   agent_system_info.logical_cpu_count, agent_system_info.memory_total_bytes,
                   agent_system_info.disk_total_bytes, agent_health.cpu_percent,
                   agent_health.memory_percent, agent_health.disk_percent,
                   agent_health.reported_at
            FROM agents
            LEFT JOIN agent_system_info ON agent_system_info.agent_id = agents.agent_id
            LEFT JOIN agent_health ON agent_health.agent_id = agents.agent_id
            ORDER BY agents.hostname
            """
        ).fetchall()
        alerts = g.db.execute(
            """
            SELECT alerts.*, agents.hostname
            FROM health_alerts AS alerts
            JOIN agents ON agents.agent_id = alerts.agent_id
            WHERE alerts.status = 'open'
            ORDER BY alerts.opened_at DESC
            """
        ).fetchall()
        return render_template("health.html", agents=agents, alerts=alerts)

    @app.get("/api/health")
    @permission_required("health.view")
    def health_api() -> Any:
        mark_offline_agents()
        requested_agent = request.args.get("agent_id", "").strip()
        agent_rows = g.db.execute(
            """
            SELECT agents.agent_id, agents.hostname, agents.status, agents.last_seen,
                   agent_system_info.architecture, agent_system_info.cpu_model,
                   agent_system_info.logical_cpu_count, agent_system_info.memory_total_bytes,
                   agent_system_info.disk_total_bytes, agent_health.cpu_percent,
                   agent_health.memory_percent, agent_health.disk_percent,
                   agent_health.reported_at
            FROM agents
            LEFT JOIN agent_system_info ON agent_system_info.agent_id = agents.agent_id
            LEFT JOIN agent_health ON agent_health.agent_id = agents.agent_id
            WHERE (? = '' OR agents.agent_id = ?)
            ORDER BY agents.hostname
            """,
            (requested_agent, requested_agent),
        ).fetchall()
        alert_rows = g.db.execute(
            """
            SELECT alerts.alert_id, alerts.agent_id, alerts.alert_type, alerts.severity,
                   alerts.status, alerts.message, alerts.opened_at, agents.hostname
            FROM health_alerts AS alerts
            JOIN agents ON agents.agent_id = alerts.agent_id
            WHERE alerts.status = 'open' AND (? = '' OR alerts.agent_id = ?)
            ORDER BY alerts.opened_at DESC
            """,
            (requested_agent, requested_agent),
        ).fetchall()
        return jsonify({
            "agents": [dict(row) for row in agent_rows],
            "alerts": [dict(row) for row in alert_rows],
            "open_alert_count": len(alert_rows),
            "updated_at": utc_now(),
        })

    @app.post("/health-alerts/<int:alert_id>/acknowledge")
    @permission_required("health.manage")
    def acknowledge_health_alert(alert_id: int) -> Any:
        execute(
            "UPDATE health_alerts SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ? WHERE alert_id = ?",
            (utc_now(), session.get("user_id"), alert_id),
        )
        flash("Health alert acknowledged.", "success")
        return redirect(url_for("health_page"))

    @app.get("/audit")
    @permission_required("audit.view")
    def audit_page() -> Any:
        entries = g.db.execute(
            """
            SELECT audit_id, occurred_at, actor_type, actor_id, action,
                   resource_type, resource_id, success, remote_address, details_json
            FROM audit_logs
            ORDER BY occurred_at DESC, audit_id DESC
            LIMIT 300
            """
        ).fetchall()
        return render_template("audit.html", entries=entries)

    @app.route("/users-roles", methods=["GET", "POST"])
    @permission_required("user.manage")
    def users_roles() -> Any:
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "viewer").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", username) or len(password) < 10 or role not in ROLE_PERMISSIONS:
                flash("Use a valid username, 10+ character password, and known role.", "danger")
                return redirect(url_for("users_roles"))
            now_text = utc_now()
            try:
                execute(
                    """
                    INSERT INTO users (user_id, username, password_hash, role, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (str(uuid.uuid4()), username, generate_password_hash(password), role, now_text, now_text),
                )
                flash(f"User '{username}' created.", "success")
            except sqlite3.IntegrityError:
                flash("Username already exists.", "danger")
            return redirect(url_for("users_roles"))
        users = g.db.execute(
            "SELECT user_id, username, role, enabled, last_login_at, created_at FROM users ORDER BY username"
        ).fetchall()
        return render_template("users_roles.html", users=users, roles=sorted(ROLE_PERMISSIONS))

    @app.post("/users/<user_id>/enabled")
    @permission_required("user.manage")
    def toggle_user(user_id: str) -> Any:
        user = g.db.execute("SELECT username, enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if user:
            if user_id == session.get("user_id") and user["enabled"]:
                flash("You cannot disable your own account.", "warning")
            else:
                execute("UPDATE users SET enabled = ?, updated_at = ? WHERE user_id = ?", (0 if user["enabled"] else 1, utc_now(), user_id))
                flash(f"User '{user['username']}' updated.", "success")
        else:
            flash("User not found.", "danger")
        return redirect(url_for("users_roles"))

    @app.get("/settings")
    @login_required
    def settings_page() -> Any:
        return render_template(
            "settings.html",
            admin_username=session.get("admin_username", app.config["ADMIN_USERNAME"]),
            limits={
                "upload_mb": app.config["UPLOAD_MAX_BYTES"] // (1024 * 1024),
                "rate_limit": app.config["RATE_LIMIT_PER_MINUTE"] if app.config["RATE_LIMIT_ENABLED"] else 0,
                "scheduler_interval": app.config["SCHEDULER_INTERVAL_SECONDS"],
                "offline_after": app.config["OFFLINE_AFTER_SECONDS"],
            },
        )

    @app.post("/settings/password")
    @login_required
    def change_password() -> Any:
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not verify_admin_password(current_password, session.get("admin_username")):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("settings_page"))

        if len(new_password) < 10:
            flash("New password must be at least 10 characters.", "danger")
            return redirect(url_for("settings_page"))

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for("settings_page"))

        password_hash = generate_password_hash(new_password)
        if session.get("user_id"):
            execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (password_hash, utc_now(), session["user_id"]),
            )
            if session.get("role") == "admin":
                set_setting("admin_password_hash", password_hash)
        else:
            set_setting("admin_password_hash", password_hash)
        flash("Password updated successfully.", "success")
        return redirect(url_for("settings_page"))

    @app.get("/scripts/editor")
    @permission_required("script.manage")
    def script_editor() -> Any:
        script_id = request.args.get("script_id", "").strip()
        script = None
        if script_id:
            script = g.db.execute(
                """
                SELECT script_id, display_name, description, windows_command, linux_command,
                       darwin_command, timeout_seconds, enabled, created_at, updated_at,
                       script_kind, file_name, script_content, content_encoding, allow_nonzero_success
                FROM scripts
                WHERE script_id = ?
                """,
                (script_id,),
            ).fetchone()
            if not script:
                flash("Script not found.", "danger")
                return redirect(url_for("dashboard"))
            script_data = dict(script)
            script_data["binary_content"] = False
            if script_data["script_kind"] == "file" and script_data["script_content"]:
                try:
                    script_data["script_content"] = decode_script_bytes(script).decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    script_data["script_content"] = ""
                    script_data["binary_content"] = True
            script = decorate_scripts([script_data])[0]
        return render_template("script_editor.html", script=script)

    @app.route("/")
    @permission_required("dashboard.view")
    def dashboard() -> str:
        mark_offline_agents()
        agents = g.db.execute(
            """
            SELECT agent_id, hostname, status, last_seen, created_at, os_family, platform
            FROM agents
            ORDER BY hostname
            """
        ).fetchall()
        open_alerts = g.db.execute(
            "SELECT COUNT(*) AS count FROM health_alerts WHERE status = 'open'"
        ).fetchone()["count"]
        task_stats = {
            row["status"]: row["count"]
            for row in g.db.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        }
        script_count = g.db.execute("SELECT COUNT(*) AS count FROM scripts").fetchone()["count"]
        tasks = g.db.execute(
            """
            SELECT t.task_id, t.agent_id, a.hostname AS agent_hostname, t.type, t.value,
                   t.status, t.created_at, t.started_at, t.completed_at, t.source_type,
                   t.schedule_id, t.attempt_number, t.max_attempts
            FROM tasks t
            LEFT JOIN agents a ON a.agent_id = t.agent_id
            ORDER BY t.created_at DESC
            LIMIT 200
            """
        ).fetchall()
        return render_template(
            "dashboard.html",
            agents=agents,
            tasks=tasks,
            script_count=script_count,
            open_alerts=open_alerts,
            task_stats=task_stats,
        )

    @app.route("/schedules", methods=["GET", "POST"])
    @permission_required("schedule.manage")
    def schedules() -> Any:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            task_type = request.form.get("task_type", "run_script").strip()
            target_type = request.form.get("target_type", "agent").strip()
            target_id = request.form.get("target_id", "").strip()
            task_value = request.form.get("task_value", "").strip()
            try:
                interval_minutes = max(1, min(int(request.form.get("interval_minutes", "60")), 10080))
            except ValueError:
                interval_minutes = 60
            timeout_seconds = normalize_timeout_value(request.form.get("timeout_seconds", "180").strip(), 180)
            allow_nonzero_success = 1 if request.form.get("allow_nonzero_success") == "on" else 0
            if not name or not target_id or timeout_seconds < 0:
                flash("Name, target, and a valid timeout are required.", "danger")
                return redirect(url_for("schedules"))
            if target_type not in {"agent", "group"}:
                flash("Schedule target type is invalid.", "danger")
                return redirect(url_for("schedules"))
            if task_type not in {"run_script", "deploy_script", "run_agent_file", "execute_shell"}:
                flash("Unsupported scheduled task type.", "danger")
                return redirect(url_for("schedules"))
            if not task_value:
                flash("A script, file, or command is required.", "danger")
                return redirect(url_for("schedules"))
            if task_type in {"run_script", "deploy_script"}:
                script = g.db.execute("SELECT script_kind, enabled FROM scripts WHERE script_id = ?", (task_value,)).fetchone()
                if not script or not script["enabled"] or (task_type == "deploy_script" and script["script_kind"] != "file"):
                    flash("Choose an enabled script compatible with the schedule.", "danger")
                    return redirect(url_for("schedules"))
            if task_type == "run_agent_file" and not is_safe_file_name(task_value):
                flash("Agent file name is invalid.", "danger")
                return redirect(url_for("schedules"))
            if target_type == "group":
                group_exists = g.db.execute("SELECT group_id FROM agent_groups WHERE group_id = ?", (target_id,)).fetchone()
            else:
                group_exists = g.db.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (target_id,)).fetchone()
            if not group_exists:
                flash("Schedule target was not found.", "danger")
                return redirect(url_for("schedules"))
            schedule_id = f"schedule-{uuid.uuid4().hex[:12]}"
            now_text = utc_now()
            execute(
                """
                INSERT INTO scheduled_tasks (
                    schedule_id, name, task_type, task_value, target_type, target_id,
                    interval_minutes, timeout_seconds, allow_nonzero_success, enabled,
                    next_run_at, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    schedule_id, name, task_type, task_value, target_type, target_id,
                    interval_minutes, timeout_seconds, allow_nonzero_success, now_text,
                    session.get("user_id"), now_text, now_text,
                ),
            )
            flash(f"Schedule '{name}' created.", "success")
            return redirect(url_for("schedules"))

        schedule_rows = g.db.execute(
            """
            SELECT schedules.*, groups.name AS group_name, agents.hostname AS agent_hostname
            FROM scheduled_tasks AS schedules
            LEFT JOIN agent_groups AS groups ON groups.group_id = schedules.target_id AND schedules.target_type = 'group'
            LEFT JOIN agents ON agents.agent_id = schedules.target_id AND schedules.target_type = 'agent'
            ORDER BY schedules.enabled DESC, schedules.next_run_at ASC
            """
        ).fetchall()
        agents = g.db.execute("SELECT agent_id, hostname FROM agents ORDER BY hostname").fetchall()
        groups = g.db.execute("SELECT group_id, name FROM agent_groups ORDER BY name").fetchall()
        scripts = g.db.execute("SELECT script_id, display_name, script_kind FROM scripts WHERE enabled = 1 ORDER BY script_id").fetchall()
        return render_template("schedules.html", schedules=schedule_rows, agents=agents, groups=groups, scripts=scripts)

    @app.post("/schedules/<schedule_id>/toggle")
    @permission_required("schedule.manage")
    def toggle_schedule(schedule_id: str) -> Any:
        schedule = g.db.execute("SELECT enabled FROM scheduled_tasks WHERE schedule_id = ?", (schedule_id,)).fetchone()
        if not schedule:
            flash("Schedule not found.", "danger")
        else:
            execute("UPDATE scheduled_tasks SET enabled = ?, updated_at = ? WHERE schedule_id = ?", (0 if schedule["enabled"] else 1, utc_now(), schedule_id))
            flash("Schedule updated.", "success")
        return redirect(url_for("schedules"))

    @app.post("/schedules/<schedule_id>/run-now")
    @permission_required("schedule.manage")
    def run_schedule_now(schedule_id: str) -> Any:
        schedule = g.db.execute("SELECT schedule_id FROM scheduled_tasks WHERE schedule_id = ?", (schedule_id,)).fetchone()
        if not schedule:
            flash("Schedule not found.", "danger")
        else:
            execute("UPDATE scheduled_tasks SET next_run_at = ?, enabled = 1, updated_at = ? WHERE schedule_id = ?", (utc_now(), utc_now(), schedule_id))
            flash("Schedule queued for the next scheduler pass.", "success")
        return redirect(url_for("schedules"))

    @app.post("/schedules/<schedule_id>/delete")
    @permission_required("schedule.manage")
    def delete_schedule(schedule_id: str) -> Any:
        execute("DELETE FROM scheduled_tasks WHERE schedule_id = ?", (schedule_id,))
        flash("Schedule deleted.", "success")
        return redirect(url_for("schedules"))

    @app.route("/agent-groups", methods=["GET", "POST"])
    @permission_required("group.manage")
    def agent_groups() -> Any:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name or len(name) > 80:
                flash("Group name is required and must be 80 characters or fewer.", "danger")
                return redirect(url_for("agent_groups"))
            group_id = f"group-{uuid.uuid4().hex[:10]}"
            now_text = utc_now()
            try:
                execute(
                    "INSERT INTO agent_groups (group_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (group_id, name, description, now_text, now_text),
                )
                flash(f"Group '{name}' created.", "success")
            except sqlite3.IntegrityError:
                flash("Group name already exists.", "danger")
            return redirect(url_for("agent_groups"))

        groups = g.db.execute(
            """
            SELECT groups.*, COUNT(members.agent_id) AS member_count
            FROM agent_groups AS groups
            LEFT JOIN agent_group_members AS members ON members.group_id = groups.group_id
            GROUP BY groups.group_id
            ORDER BY groups.name
            """
        ).fetchall()
        agents = g.db.execute("SELECT agent_id, hostname, status FROM agents ORDER BY hostname").fetchall()
        membership_rows = g.db.execute(
            "SELECT group_id, agent_id FROM agent_group_members ORDER BY group_id, agent_id"
        ).fetchall()
        memberships: dict[str, list[str]] = {}
        for row in membership_rows:
            memberships.setdefault(row["group_id"], []).append(row["agent_id"])
        return render_template("agent_groups.html", groups=groups, agents=agents, memberships=memberships)

    @app.post("/agent-groups/<group_id>/members")
    @permission_required("group.manage")
    def update_group_members(group_id: str) -> Any:
        group = g.db.execute("SELECT group_id FROM agent_groups WHERE group_id = ?", (group_id,)).fetchone()
        if not group:
            flash("Group not found.", "danger")
            return redirect(url_for("agent_groups"))
        action = request.form.get("action", "add")
        agent_id = request.form.get("agent_id", "").strip()
        agent = g.db.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not agent:
            flash("Agent not found.", "danger")
            return redirect(url_for("agent_groups"))
        if action == "remove":
            execute("DELETE FROM agent_group_members WHERE group_id = ? AND agent_id = ?", (group_id, agent_id))
            flash(f"Agent removed from group {group_id}.", "success")
        else:
            execute(
                "INSERT OR IGNORE INTO agent_group_members (group_id, agent_id, added_at) VALUES (?, ?, ?)",
                (group_id, agent_id, utc_now()),
            )
            flash(f"Agent added to group {group_id}.", "success")
        return redirect(url_for("agent_groups"))

    @app.post("/agent-groups/<group_id>/delete")
    @permission_required("group.manage")
    def delete_agent_group(group_id: str) -> Any:
        execute("DELETE FROM scheduled_tasks WHERE target_type = 'group' AND target_id = ?", (group_id,))
        execute("DELETE FROM agent_groups WHERE group_id = ?", (group_id,))
        flash("Agent group deleted.", "success")
        return redirect(url_for("agent_groups"))

    @app.route("/agents/<agent_id>")
    @permission_required("agent.view")
    def agent_detail(agent_id: str) -> str:
        mark_offline_agents()
        agent = g.db.execute(
            """
            SELECT agent_id, hostname, status, last_seen, created_at, os_family, platform
            FROM agents
            WHERE agent_id = ?
            """,
            (agent_id,),
        ).fetchone()
        if not agent:
            abort(404)

        tasks = g.db.execute(
            """
            SELECT task_id, type, value, status, output, error, created_at, started_at, completed_at,
                   source_type, attempt_number, max_attempts
            FROM tasks
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (agent_id,),
        ).fetchall()
        scripts = g.db.execute(
            """
            SELECT script_id, display_name, script_kind, file_name, timeout_seconds,
                   windows_command, linux_command, darwin_command
            FROM scripts
            WHERE enabled = 1
            ORDER BY script_id
            """
        ).fetchall()
        agent_files = g.db.execute(
            """
            SELECT file_name, rel_path, file_size, modified_at, synced_at
            FROM agent_files
            WHERE agent_id = ?
            ORDER BY file_name
            """,
            (agent_id,),
        ).fetchall()
        system_info = g.db.execute(
            "SELECT * FROM agent_system_info WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        health_info = g.db.execute(
            "SELECT * FROM agent_health WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        alerts = g.db.execute(
            """
            SELECT alert_id, alert_type, severity, status, message, opened_at
            FROM health_alerts
            WHERE agent_id = ? AND status = 'open'
            ORDER BY opened_at DESC
            """,
            (agent_id,),
        ).fetchall()
        return render_template(
            "agent_detail.html",
            agent=agent,
            tasks=tasks,
            scripts=decorate_scripts(scripts),
            agent_files=agent_files,
            system_info=system_info,
            health_info=health_info,
            alerts=alerts,
        )

    @app.post("/agents/<agent_id>/delete")
    @permission_required("agent.manage")
    def delete_agent(agent_id: str) -> Any:
        agent = g.db.execute(
            "SELECT agent_id FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if not agent:
            flash("Agent not found.", "danger")
            return redirect(url_for("dashboard"))

        task_rows = g.db.execute(
            "SELECT task_id, task_file FROM tasks WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()

        for row in task_rows:
            task_file = str(row["task_file"] or "").strip()
            if task_file:
                try:
                    task_path = resolve_task_file(TASKS_DIR, task_file)
                    if task_path.exists():
                        task_path.unlink()
                except OSError:
                    # Best-effort cleanup for task JSON files.
                    pass

        execute("DELETE FROM task_controls WHERE agent_id = ?", (agent_id,))
        execute("DELETE FROM scheduled_tasks WHERE target_type = 'agent' AND target_id = ?", (agent_id,))
        execute("DELETE FROM agent_files WHERE agent_id = ?", (agent_id,))
        execute("DELETE FROM tasks WHERE agent_id = ?", (agent_id,))
        execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))

        flash(f"Agent '{agent_id}' deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/tasks/create", methods=["POST"])
    @permission_required("task.create")
    def create_task() -> Any:
        agent_id = request.form.get("agent_id", "").strip()
        task_type = request.form.get("task_type", "run_script").strip()
        task_value = ""
        meta: dict[str, Any] = {}

        if task_type in BLOCKED_TASK_TYPES:
            flash("Reverse shell tasks are blocked for safety.", "danger")
            return redirect(request.referrer or url_for("dashboard"))

        if task_type not in FORM_TASK_TYPES:
            flash("Unsupported task type.", "danger")
            return redirect(request.referrer or url_for("dashboard"))

        target_type = request.form.get("target_type", "agent").strip()
        if target_type == "group":
            group_id = request.form.get("group_id", "").strip()
            target_rows = g.db.execute(
                "SELECT agent_id FROM agent_group_members WHERE group_id = ? ORDER BY agent_id",
                (group_id,),
            ).fetchall()
            if not target_rows:
                flash("Group has no agents or does not exist.", "danger")
                return redirect(request.referrer or url_for("dashboard"))
            agent_ids = [row["agent_id"] for row in target_rows]
        else:
            agent_ids = [agent_id]
            agent = g.db.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if not agent:
                flash("Unknown agent.", "danger")
                return redirect(request.referrer or url_for("dashboard"))

        if task_type in {"run_script", "deploy_script"}:
            task_value = request.form.get("task_value", "").strip()
            script = g.db.execute(
                "SELECT script_id, enabled, script_kind FROM scripts WHERE script_id = ?",
                (task_value,),
            ).fetchone()
            if not script or not script["enabled"]:
                flash("Invalid or disabled script ID.", "danger")
                return redirect(request.referrer or url_for("dashboard"))

            if task_type == "deploy_script" and script["script_kind"] != "file":
                flash("Deploy task only supports file-based scripts.", "danger")
                return redirect(request.referrer or url_for("dashboard"))
        else:
            task_value = request.form.get("agent_file_name", "").strip()
            timeout_raw = request.form.get("run_timeout_seconds", "180").strip()
            allow_nonzero_success = 1 if request.form.get("run_allow_nonzero_success") == "on" else 0
            if not is_safe_file_name(task_value):
                flash("Invalid agent file name.", "danger")
                return redirect(request.referrer or url_for("dashboard"))
            timeout_value = normalize_timeout_value(timeout_raw, 180)
            if timeout_value < 0:
                flash("Timeout must be 0 (unlimited) or between 5 and 86400 seconds.", "danger")
                return redirect(request.referrer or url_for("dashboard"))
            meta["timeout_seconds"] = timeout_value
            meta["allow_nonzero_success"] = allow_nonzero_success

        task_ids = [
            queue_task(
                target_agent_id,
                task_type,
                task_value,
                meta if meta else None,
                created_by=session.get("user_id"),
            )
            for target_agent_id in agent_ids
        ]
        target_label = f"{len(task_ids)} agents" if len(task_ids) != 1 else agent_ids[0]
        flash(f"Task {task_ids[0][:8]} queued for {target_label}.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/scripts/save", methods=["GET", "POST"])
    @app.route("/scripts/save/", methods=["GET", "POST"])
    @permission_required("script.manage")
    def save_script() -> Any:
        if request.method == "GET":
            return redirect(url_for("script_editor"))

        script_id = request.form.get("script_id", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        description = request.form.get("description", "").strip()
        windows_command = request.form.get("windows_command", "").strip()
        linux_command = request.form.get("linux_command", "").strip()
        darwin_command = request.form.get("darwin_command", "").strip()
        timeout_seconds_raw = request.form.get("timeout_seconds", "120").strip()
        allow_nonzero_success = 1 if request.form.get("allow_nonzero_success") == "on" else 0

        if not is_safe_script_id(script_id):
            flash("Script ID must be 2-64 chars: letters, numbers, _ or -.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        if not display_name:
            flash("Display name is required.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        if not windows_command and not linux_command and not darwin_command:
            flash("At least one OS command must be provided.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        timeout_seconds = normalize_timeout_value(timeout_seconds_raw, 120)
        if timeout_seconds < 0:
            flash("Timeout must be 0 (unlimited) or between 5 and 86400 seconds.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        now = utc_now()
        existing = g.db.execute("SELECT script_id FROM scripts WHERE script_id = ?", (script_id,)).fetchone()
        if existing:
            execute(
                """
                UPDATE scripts
                SET display_name = ?, description = ?, windows_command = ?, linux_command = ?,
                    darwin_command = ?, timeout_seconds = ?, enabled = 1, updated_at = ?,
                    script_kind = 'command', file_name = NULL, script_content = NULL,
                    allow_nonzero_success = ?
                WHERE script_id = ?
                """,
                (
                    display_name,
                    description,
                    windows_command,
                    linux_command,
                    darwin_command,
                    timeout_seconds,
                    now,
                    allow_nonzero_success,
                    script_id,
                ),
            )
            flash(f"Script '{script_id}' updated.", "success")
        else:
            execute(
                """
                INSERT INTO scripts (
                    script_id, display_name, description, windows_command,
                    linux_command, darwin_command, timeout_seconds, enabled,
                    created_at, updated_at, script_kind, file_name, script_content, allow_nonzero_success
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'command', NULL, NULL, ?)
                """,
                (
                    script_id,
                    display_name,
                    description,
                    windows_command,
                    linux_command,
                    darwin_command,
                    timeout_seconds,
                    now,
                    now,
                    allow_nonzero_success,
                ),
            )
            flash(f"Script '{script_id}' added.", "success")

        return redirect(url_for("script_editor", script_id=script_id))

    @app.route("/scripts/save-file", methods=["GET", "POST"])
    @app.route("/scripts/save-file/", methods=["GET", "POST"])
    @permission_required("script.manage")
    def save_file_script() -> Any:
        if request.method == "GET":
            return redirect(url_for("script_editor"))

        script_id = request.form.get("script_id", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        description = request.form.get("description", "").strip()
        file_name = request.form.get("file_name", "").strip()
        script_content = request.form.get("script_content", "")
        timeout_seconds_raw = request.form.get("timeout_seconds", "120").strip()
        allow_nonzero_success = 1 if request.form.get("allow_nonzero_success") == "on" else 0
        uploaded_file = request.files.get("script_upload")

        if uploaded_file and uploaded_file.filename:
            file_name = uploaded_basename(uploaded_file.filename)
            uploaded_bytes = uploaded_file.read()
        else:
            uploaded_bytes = b""

        if not is_safe_script_id(script_id):
            flash("Script ID must be 2-64 chars: letters, numbers, _ or -.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        if not display_name:
            flash("Display name is required.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        if not is_safe_file_name(file_name) or not is_supported_script_extension(file_name):
            flash("Provide a safe script file name with an extension.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        if uploaded_bytes:
            script_bytes = uploaded_bytes
        else:
            script_bytes = script_content.encode("utf-8")

        if not script_bytes.strip():
            flash("Script content is empty.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))
        if len(script_bytes) > app.config["UPLOAD_MAX_BYTES"]:
            flash(
                f"File exceeds upload limit ({app.config['UPLOAD_MAX_BYTES'] // (1024 * 1024)}MB).",
                "danger",
            )
            return redirect(url_for("script_editor", script_id=script_id))

        timeout_seconds = normalize_timeout_value(timeout_seconds_raw, 120)
        if timeout_seconds < 0:
            flash("Timeout must be 0 (unlimited) or between 5 and 86400 seconds.", "danger")
            return redirect(url_for("script_editor", script_id=script_id))

        save_file_script_content(
            script_id,
            display_name,
            description,
            file_name,
            script_bytes,
            timeout_seconds,
            allow_nonzero_success,
        )

        return redirect(url_for("script_editor", script_id=script_id))

    @app.post("/scripts/<script_id>/toggle")
    @permission_required("script.manage")
    def toggle_script(script_id: str) -> Any:
        script = g.db.execute(
            "SELECT script_id, enabled FROM scripts WHERE script_id = ?",
            (script_id,),
        ).fetchone()
        if not script:
            flash("Script not found.", "danger")
            return redirect(url_for("dashboard"))

        new_state = 0 if script["enabled"] else 1
        execute(
            "UPDATE scripts SET enabled = ?, updated_at = ? WHERE script_id = ?",
            (new_state, utc_now(), script_id),
        )
        flash(f"Script '{script_id}' {'enabled' if new_state else 'disabled'}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/scripts/<script_id>/deploy/<agent_id>")
    @permission_required("script.manage")
    def deploy_script_to_agent(script_id: str, agent_id: str) -> Any:
        script = g.db.execute(
            "SELECT script_id, script_kind, enabled FROM scripts WHERE script_id = ?",
            (script_id,),
        ).fetchone()
        if not script or not script["enabled"]:
            flash("Script not found or disabled.", "danger")
            return redirect(request.referrer or url_for("dashboard"))
        if script["script_kind"] != "file":
            flash("Only file-based scripts can be transferred.", "danger")
            return redirect(request.referrer or url_for("dashboard"))

        agent = g.db.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not agent:
            flash("Target agent not found.", "danger")
            return redirect(request.referrer or url_for("dashboard"))

        task_id = queue_task(agent_id, "deploy_script", script_id)
        flash(f"Transfer task {task_id[:8]} queued for {agent_id}.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/tasks/<task_id>/kill")
    @permission_required("task.kill")
    def kill_task(task_id: str) -> Any:
        task = g.db.execute(
            "SELECT task_id, agent_id, status, task_file FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            flash("Task not found.", "danger")
            return redirect(request.referrer or url_for("dashboard"))

        now = utc_now()
        if task["status"] == "pending":
            execute(
                """
                UPDATE tasks
                SET status = 'cancelled', error = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                ("Cancelled before execution by admin.", now, now, task_id),
            )
            update_task_file(
                task["task_file"],
                {
                    "status": "cancelled",
                    "error": "Cancelled before execution by admin.",
                    "completed_at": now,
                    "updated_at": now,
                },
                TASKS_DIR,
            )
            flash(f"Task {task_id[:8]} cancelled.", "success")
            return redirect(request.referrer or url_for("dashboard"))

        if task["status"] != "running":
            flash("Only pending or running tasks can be stopped.", "warning")
            return redirect(request.referrer or url_for("dashboard"))

        execute(
            """
            INSERT INTO task_controls (task_id, agent_id, action, created_at, consumed_at)
            VALUES (?, ?, 'stop', ?, NULL)
            ON CONFLICT(task_id) DO UPDATE SET
                action = excluded.action,
                created_at = excluded.created_at,
                consumed_at = NULL
            """,
            (task_id, task["agent_id"], now),
        )
        flash(f"Stop request sent for task {task_id[:8]}.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/tasks/<task_id>/retry")
    @permission_required("task.retry")
    def retry_task(task_id: str) -> Any:
        task = g.db.execute(
            """
            SELECT task_id, agent_id, type, value, status, meta_json,
                   root_task_id, attempt_number, max_attempts
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if not task:
            flash("Task not found.", "danger")
            return redirect(request.referrer or url_for("dashboard"))
        if task["status"] not in {"failed", "killed", "cancelled"}:
            flash("Only failed, killed, or cancelled tasks can be retried.", "warning")
            return redirect(request.referrer or url_for("dashboard"))
        next_task_id = queue_task(
            task["agent_id"],
            task["type"],
            task["value"],
            parse_meta(task["meta_json"]),
            source_type="retry",
            root_task_id=task["root_task_id"] or task["task_id"],
            attempt_number=int(task["attempt_number"]) + 1,
            max_attempts=max(2, int(task["max_attempts"]) + 1),
            created_by=session.get("user_id"),
        )
        flash(f"Retry {next_task_id[:8]} queued for {task['agent_id']}.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/api/register")
    @limiter.exempt
    def register_agent() -> Any:
        register_token = request.headers.get("X-Register-Token", "")
        if not secrets.compare_digest(register_token, app.config["REGISTER_TOKEN"]):
            return jsonify({"error": "unauthorized"}), 401
        
        payload = request.get_json(silent=True) or {}
        agent_id = str(payload.get("agent_id", "")).strip()
        hostname = str(payload.get("hostname", "")).strip()

        if not hostname or len(hostname) > 255:
            return jsonify({"error": "invalid hostname"}), 400
        if agent_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id):
            return jsonify({"error": "invalid agent_id"}), 400

        hostname = re.sub(r"[^\w\-.]", "_", hostname)[:255]
        os_family = normalize_os_family(str(payload.get("os_family", "")))
        platform_name = str(payload.get("platform", "")).strip()[:200]

        if not agent_id:
            agent_id = f"agent-{uuid.uuid4().hex[:10]}"

        now = utc_now()
        existing = g.db.execute("SELECT token FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if existing:
            token = existing["token"]
            execute(
                """
                UPDATE agents
                SET hostname = ?, status = 'online', last_seen = ?, updated_at = ?,
                    os_family = ?, platform = ?
                WHERE agent_id = ?
                """,
                (hostname, now, now, os_family, platform_name, agent_id),
            )
        else:
            token = secrets.token_hex(32)
            execute(
                """
                INSERT INTO agents (
                    agent_id, hostname, token, status, last_seen, created_at, updated_at, os_family, platform
                ) VALUES (?, ?, ?, 'online', ?, ?, ?, ?, ?)
                """,
                (agent_id, hostname, token, now, now, now, os_family, platform_name),
            )

        system_info = payload.get("system")
        health_info = payload.get("health")
        if isinstance(system_info, dict) and isinstance(health_info, dict):
            upsert_agent_telemetry(g.db, agent_id, system_info, health_info)

        return jsonify(

            {
                "agent_id": agent_id,
                "agent_token": token,
                "poll_interval_seconds": 10,
                "server_time": now,
            }
        )

    @app.post("/api/heartbeat/<agent_id>")
    @limiter.exempt
    def heartbeat_agent(agent_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401
        payload = request.get_json(silent=True) or {}
        system_info = payload.get("system")
        health_info = payload.get("health")
        touch_agent(agent_id)
        if isinstance(system_info, dict):
            reported_os = str(system_info.get("os_family") or "").strip()
            if reported_os:
                # Agents that keep their saved token never re-register, so refresh
                # the OS columns here instead of only at registration time.
                execute(
                    "UPDATE agents SET os_family = ?, platform = ? WHERE agent_id = ?",
                    (
                        normalize_os_family(reported_os),
                        str(system_info.get("platform") or "").strip()[:200],
                        agent_id,
                    ),
                )
        if isinstance(system_info, dict) and isinstance(health_info, dict):
            upsert_agent_telemetry(g.db, agent_id, system_info, health_info)
        return jsonify({"ok": True, "server_time": utc_now()})

    @app.post("/api/inventory/<agent_id>")
    @limiter.exempt
    def sync_inventory(agent_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        files = payload.get("files", [])
        if not isinstance(files, list):
            return jsonify({"error": "files must be a list"}), 400

        now = utc_now()
        rows: list[tuple[Any, ...]] = []
        for item in files[:2000]:
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file_name", "")).strip()
            rel_path = str(item.get("rel_path", "")).strip()
            modified_at = str(item.get("modified_at", "")).strip()[:100]
            try:
                file_size = int(item.get("file_size", 0))
            except (TypeError, ValueError):
                file_size = 0

            if not is_safe_file_name(file_name):
                continue
            if not rel_path or ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
                continue
            rows.append((agent_id, file_name, rel_path, max(file_size, 0), modified_at, now))

        g.db.execute("DELETE FROM agent_files WHERE agent_id = ?", (agent_id,))
        if rows:
            g.db.executemany(
                """
                INSERT OR REPLACE INTO agent_files (
                    agent_id, file_name, rel_path, file_size, modified_at, synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        g.db.commit()
        touch_agent(agent_id)
        return jsonify({"ok": True, "synced_at": now})

    @app.get("/api/task-control/<agent_id>/<task_id>")
    @limiter.exempt
    def task_control(agent_id: str, task_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401

        touch_agent(agent_id)
        row = g.db.execute(
            """
            SELECT task_id, action
            FROM task_controls
            WHERE task_id = ? AND agent_id = ? AND consumed_at IS NULL
            """,
            (task_id, agent_id),
        ).fetchone()

        if not row:
            return jsonify({"action": None, "server_time": utc_now()})

        now = utc_now()
        execute(
            "UPDATE task_controls SET consumed_at = ? WHERE task_id = ?",
            (now, task_id),
        )
        return jsonify({"action": row["action"], "server_time": now})

    @app.get("/api/poll/<agent_id>")
    @limiter.exempt
    def poll_task(agent_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401

        touch_agent(agent_id)

        agent = g.db.execute(
            "SELECT os_family FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        agent_os = normalize_os_family(agent["os_family"] if agent else "linux")

        task = g.db.execute(
            """
            SELECT task_id, type, value, status, created_at, task_file, meta_json, attempt_number
            FROM tasks
            WHERE agent_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (agent_id,),
        ).fetchone()

        if not task:
            return jsonify({"task": None, "server_time": utc_now()})

        task_type = task["type"]
        if task_type in BLOCKED_TASK_TYPES:
            now = fail_task(task, "Blocked task type.")
            return jsonify({"task": None, "server_time": now})

        execution: dict[str, Any] = {}
        meta = parse_meta(task["meta_json"])

        if task_type == "run_script":
            script = g.db.execute(
                """
                SELECT script_id, display_name, windows_command, linux_command, darwin_command,
                       timeout_seconds, enabled, script_kind, file_name, script_content,
                       allow_nonzero_success, content_encoding, content_sha256
                FROM scripts
                WHERE script_id = ?
                """,
                (task["value"],),
            ).fetchone()

            if not script or not script["enabled"]:
                now = fail_task(task, f"Script '{task['value']}' is missing or disabled")
                return jsonify({"task": None, "server_time": now})

            if script["script_kind"] == "file":
                if not script["file_name"] or not script["script_content"]:
                    now = fail_task(task, f"File script '{task['value']}' has no content")
                    return jsonify({"task": None, "server_time": now})
                execution = {
                    "mode": "file_inline",
                    "file_name": script["file_name"],
                    "content": base64.b64encode(decode_script_bytes(script)).decode("ascii"),
                    "content_encoding": "base64",
                    "sha256": script["content_sha256"],
                    "timeout_seconds": int(script["timeout_seconds"]),
                    "allow_nonzero_success": bool(script["allow_nonzero_success"]),
                }
            else:
                command = get_os_command(script, agent_os).strip()
                if not command:
                    now = fail_task(task, f"Script '{task['value']}' has no command for os={agent_os}")
                    return jsonify({"task": None, "server_time": now})
                execution = {
                    "mode": "command",
                    "command": command,
                    "timeout_seconds": int(script["timeout_seconds"]),
                    "allow_nonzero_success": bool(script["allow_nonzero_success"]),
                }

        elif task_type == "deploy_script":
            script = g.db.execute(
                """
                SELECT script_id, enabled, script_kind, file_name, script_content
                     , content_encoding, content_sha256
                FROM scripts
                WHERE script_id = ?
                """,
                (task["value"],),
            ).fetchone()
            if not script or not script["enabled"] or script["script_kind"] != "file":
                now = fail_task(task, "Deploy requires an enabled file-based script.")
                return jsonify({"task": None, "server_time": now})
            execution = {
                "mode": "deploy_file",
                "file_name": script["file_name"],
                "content": base64.b64encode(decode_script_bytes(script)).decode("ascii"),
                "content_encoding": "base64",
                "sha256": script["content_sha256"],
            }

        elif task_type == "run_agent_file":
            if not is_safe_file_name(task["value"]):
                now = fail_task(task, "Unsafe agent file name.")
                return jsonify({"task": None, "server_time": now})
            execution = {
                "mode": "agent_file",
                "file_name": task["value"],
                "timeout_seconds": int(meta.get("timeout_seconds", 180)),
                "allow_nonzero_success": bool(meta.get("allow_nonzero_success", 0)),
            }

        elif task_type == "execute_shell":
            command = str(meta.get("shell_command") or task["value"]).strip()
            if not command:
                now = fail_task(task, "No command provided for shell execution.")
                return jsonify({"task": None, "server_time": now})
            execution = {
                "mode": "command",
                "command": command,
                "timeout_seconds": int(meta.get("timeout_seconds", 180)),
                "allow_nonzero_success": bool(meta.get("allow_nonzero_success", 0)),
            }

        else:
            now = fail_task(task, f"Unsupported task type: {task_type}")
            return jsonify({"task": None, "server_time": now})

        now = utc_now()
        claim = execute(
            """
            UPDATE tasks
            SET status = 'running', started_at = ?, updated_at = ?
            WHERE task_id = ? AND status = 'pending'
            """,
            (now, now, task["task_id"]),
        )
        if claim.rowcount != 1:
            return jsonify({"task": None, "server_time": now})
        update_task_file(
            task["task_file"],
            {
                "status": "running",
                "started_at": now,
                "updated_at": now,
                "execution_mode": execution.get("mode"),
            },
            TASKS_DIR,
        )

        return jsonify(
            {
                "task": {
                    "task_id": task["task_id"],
                    "type": task["type"],
                    "value": task["value"],
                    "status": "running",
                    "created_at": task["created_at"],
                    "attempt_number": task["attempt_number"],
                    "execution": execution,
                },
                "server_time": now,
            }
        )

    @app.post("/api/report/<agent_id>/<task_id>")
    @limiter.exempt
    def report_task(agent_id: str, task_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        status = payload.get("status")
        output = str(payload.get("output", ""))[:20000]
        error = str(payload.get("error", ""))[:20000]
        try:
            attempt_number = int(payload.get("attempt_number", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid attempt number"}), 400

        if status not in {"completed", "failed", "killed", "cancelled"}:
            return jsonify({"error": "invalid status"}), 400

        task = g.db.execute(
            "SELECT task_id, agent_id, type, status, attempt_number, task_file FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()

        if not task or task["agent_id"] != agent_id:
            return jsonify({"error": "task not found"}), 404
        if attempt_number != int(task["attempt_number"]):
            return jsonify({"error": "stale task attempt"}), 409

        if task["status"] in {"completed", "failed", "killed", "cancelled"}:
            return jsonify({"ok": True, "task_id": task_id, "status": task["status"]})

        now = utc_now()
        file_updates = {
            "status": status,
            "output": output,
            "error": error,
            "completed_at": now,
            "updated_at": now,
        }
        if task["type"] == "execute_shell":
            execute(
                """
                UPDATE tasks
                SET status = ?, value = ?, output = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (status, "[direct shell command]", output, error, now, now, task_id),
            )
            file_updates["value"] = "[direct shell command]"
        else:
            execute(
                """
                UPDATE tasks
                SET status = ?, output = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (status, output, error, now, now, task_id),
            )
        update_task_file(task["task_file"], file_updates, TASKS_DIR)
        touch_agent(agent_id)

        return jsonify({"ok": True, "task_id": task_id, "status": status})

    @app.post("/api/task-output/<agent_id>/<task_id>")
    @limiter.exempt
    def task_output(agent_id: str, task_id: str) -> Any:
        agent_token = request.headers.get("X-Agent-Token")
        if not is_agent_authenticated(agent_id, agent_token):
            return jsonify({"error": "unauthorized"}), 401
        task = g.db.execute(
            "SELECT agent_id, status, attempt_number FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not task or task["agent_id"] != agent_id:
            return jsonify({"error": "task not found"}), 404
        if task["status"] != "running":
            return jsonify({"error": "task is no longer running"}), 409
        payload = request.get_json(silent=True) or {}
        try:
            attempt_number = int(payload.get("attempt_number", task["attempt_number"]))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid attempt number"}), 400
        if attempt_number != int(task["attempt_number"]):
            return jsonify({"error": "invalid output position"}), 409

        raw_chunks = payload.get("chunks")
        if not isinstance(raw_chunks, list):
            raw_chunks = [{
                "stream": payload.get("stream"),
                "sequence": payload.get("sequence", 0),
                "text": payload.get("text", ""),
                "eof": payload.get("eof", False),
            }]

        received_at = utc_now()
        rows: list[tuple[Any, ...]] = []
        for chunk in raw_chunks[:256]:
            if not isinstance(chunk, dict):
                continue
            stream = str(chunk.get("stream", "")).strip().lower()
            if stream not in {"stdout", "stderr"}:
                return jsonify({"error": "invalid stream"}), 400
            try:
                sequence = int(chunk.get("sequence", 0))
            except (TypeError, ValueError):
                return jsonify({"error": "invalid sequence"}), 400
            if sequence < 0 or sequence > 100000:
                return jsonify({"error": "invalid output position"}), 409
            text = str(chunk.get("text", ""))[:20000]
            if text:
                rows.append((task_id, attempt_number, stream, sequence, text, received_at))

        if rows:
            g.db.executemany(
                """
                INSERT OR IGNORE INTO task_output_chunks (
                    task_id, attempt_number, stream, sequence, text, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            g.db.commit()
        return jsonify({"ok": True, "accepted": len(rows)})

    @app.get("/api/agents")
    @permission_required("dashboard.view")
    def list_agents() -> Any:
        agents = g.db.execute(
            """
            SELECT agent_id, hostname, status, last_seen, created_at, os_family, platform
            FROM agents
            ORDER BY hostname
            """
        ).fetchall()
        return jsonify([dict(agent) for agent in agents])

    @app.get("/api/scripts")
    @permission_required("dashboard.view")
    def list_scripts() -> Any:
        scripts = g.db.execute(
            """
            SELECT script_id, display_name, description, timeout_seconds, enabled, created_at,
                   updated_at, script_kind, file_name
            FROM scripts
            ORDER BY script_id
            """
        ).fetchall()
        return jsonify([dict(script) for script in scripts])

    @app.get("/shell/<agent_id>")
    @permission_required("shell.execute")
    def shell_page(agent_id: str) -> Any:
        """Render the direct shell command page."""
        mark_offline_agents()
        agent = g.db.execute(
            "SELECT agent_id, hostname, status, os_family, platform FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        
        if not agent:
            flash("Agent not found", "danger")
            return redirect(url_for("dashboard"))
        
        return render_template("shell.html", agent=dict(agent))

    @app.post("/api/shell_execute/<agent_id>")
    @permission_required("shell.execute")
    def create_shell_task(agent_id: str) -> Any:
        """Queue one direct shell command for an agent."""
        payload = request.get_json(silent=True) or {}
        command = str(payload.get("command", "")).strip()
        
        if not command:
            return jsonify({"error": "command is required"}), 400
        
        agent = g.db.execute(
            "SELECT agent_id, hostname FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        
        if not agent:
            return jsonify({"error": "agent not found"}), 404

        timeout_seconds = normalize_timeout_value(str(payload.get("timeout_seconds", "180")), 180)
        if timeout_seconds < 0:
            return jsonify({"error": "timeout_seconds must be 0 or between 5 and 86400"}), 400
        task_id = queue_task(
            agent_id,
            "execute_shell",
            command,
            {
                "timeout_seconds": timeout_seconds,
                "allow_nonzero_success": 0,
                # value is redacted after completion; keep the real command for retries
                "shell_command": command,
            },
            created_by=session.get("user_id"),
        )
        return jsonify({"task_id": task_id, "agent_id": agent_id})

    @app.get("/api/shell_task/<task_id>")
    @permission_required("shell.execute")
    def get_shell_task(task_id: str) -> Any:
        task = g.db.execute(
            """
            SELECT task_id, agent_id, type, value, status, output, error, created_at, started_at, completed_at,
                   attempt_number, source_type
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if not task or task["type"] != "execute_shell":
            return jsonify({"error": "task not found"}), 404
        chunks = g.db.execute(
            """
            SELECT stream, sequence, text
            FROM task_output_chunks
            WHERE task_id = ? AND attempt_number = ?
            ORDER BY stream, sequence
            """,
            (task_id, task["attempt_number"]),
        ).fetchall()
        result = dict(task)
        result["output_chunks"] = [dict(chunk) for chunk in chunks]
        return jsonify(result)

    @app.get("/api/tasks/<task_id>/live")
    @permission_required("agent.view")
    def task_live_output(task_id: str) -> Any:
        """Return streamed output for an active task, or final output once it ends."""
        task = g.db.execute(
            """
            SELECT task_id, agent_id, status, output, error, attempt_number
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if not task:
            return jsonify({"error": "task not found"}), 404
        status = task["status"]
        if status in {"completed", "failed", "killed", "cancelled"}:
            return jsonify(
                {
                    "task_id": task_id,
                    "status": status,
                    "attempt_number": task["attempt_number"],
                    "stdout": task["output"] or "",
                    "stderr": task["error"] or "",
                    "final": True,
                }
            )
        chunks = g.db.execute(
            """
            SELECT stream, sequence, text
            FROM task_output_chunks
            WHERE task_id = ? AND attempt_number = ?
            ORDER BY stream, sequence
            """,
            (task_id, task["attempt_number"]),
        ).fetchall()
        stdout_parts = [chunk["text"] for chunk in chunks if chunk["stream"] == "stdout"]
        stderr_parts = [chunk["text"] for chunk in chunks if chunk["stream"] == "stderr"]
        return jsonify(
            {
                "task_id": task_id,
                "status": status,
                "attempt_number": task["attempt_number"],
                "stdout": "".join(stdout_parts)[-20000:],
                "stderr": "".join(stderr_parts)[-20000:],
                "final": False,
            }
        )

    app.socketio = socketio
    if app.config["SCHEDULER_ENABLED"]:
        start_scheduler(app)

    return app


app = create_app()
socketio = app.socketio

if __name__ == "__main__":
    raw_port = os.getenv("SERVER_PORT") or os.getenv("PORT") or "5000"
    try:
        server_port = int(raw_port)
    except ValueError:
        server_port = 5000
    # Platforms such as Railway inject PORT and require binding to all interfaces.
    server_host = os.getenv("SERVER_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    socketio.run(
        app,
        host=server_host,
        port=server_port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
