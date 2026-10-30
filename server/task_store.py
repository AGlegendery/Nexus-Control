import json
import os
import tempfile
from pathlib import Path
from typing import Any


def resolve_task_file(tasks_dir: Path, task_file_path: str | Path) -> Path:
    task_path = Path(task_file_path)
    if task_path.exists():
        return task_path
    fallback = tasks_dir / task_path.name
    return fallback if fallback.exists() else task_path


def repair_task_file_paths(conn: Any, tasks_dir: Path) -> None:
    rows = conn.execute("SELECT task_id, task_file FROM tasks").fetchall()
    for row in rows:
        current_path = resolve_task_file(tasks_dir, row["task_file"])
        if current_path.exists() and str(current_path) != str(row["task_file"]):
            conn.execute(
                "UPDATE tasks SET task_file = ? WHERE task_id = ?",
                (str(current_path), row["task_id"]),
            )
    conn.commit()


def write_task_file(tasks_dir: Path, task_payload: dict[str, Any]) -> Path:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_path = tasks_dir / f"task_{task_payload['task_id']}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=".task-", dir=str(tasks_dir), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as task_file:
            json.dump(task_payload, task_file, indent=2)
            task_file.flush()
            os.fsync(task_file.fileno())
        os.replace(tmp_name, task_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return task_path


def update_task_file(
    task_file_path: str | Path,
    updates: dict[str, Any],
    fallback_dir: Path | None = None,
) -> None:
    task_path = resolve_task_file(fallback_dir, task_file_path) if fallback_dir else Path(task_file_path)
    if not task_path.exists():
        return

    try:
        with task_path.open("r", encoding="utf-8") as task_file:
            payload = json.load(task_file)
        if not isinstance(payload, dict):
            return
        payload.update(updates)
        fd, tmp_name = tempfile.mkstemp(prefix=".task-", dir=str(task_path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as task_file:
                json.dump(payload, task_file, indent=2)
                task_file.flush()
                os.fsync(task_file.fileno())
            os.replace(tmp_name, task_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    except (OSError, ValueError):
        # The sidecar is informational; the database remains authoritative.
        return
