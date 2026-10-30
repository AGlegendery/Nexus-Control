"""Control Nexus agent v1.1.

Changes over v1.0:
- CPU usage is measured on Linux, Windows, and macOS instead of always reporting None
- Windows memory telemetry via GlobalMemoryStatusEx (was unavailable)
- static system info is cached so heartbeats do not re-read /proc or resolve DNS
- frozen builds resolve a real Python interpreter for .py files instead of relaunching the agent
- long-running tasks send keepalive heartbeats and poll stop requests at 1 Hz, not 5 Hz
- output uploads use a dedicated HTTP session (no cross-thread use of the main session)
- state file is written 0600 on POSIX; logging is safe when stdout is unavailable
- file-name validation rejects trailing dots/spaces
"""

import base64
import hashlib
import json
import os
import platform
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "agent_state.json"
SCRIPTS_DIR = BASE_DIR / "scripts"

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        return
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(BASE_DIR / ".env")


def get_int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:5000").rstrip("/")
REGISTER_TOKEN = os.getenv("REGISTER_TOKEN", "change-me-register-token")
REQUEST_TIMEOUT = get_int_env("REQUEST_TIMEOUT", 15)
DEFAULT_POLL_SECONDS = get_int_env("POLL_SECONDS", 10)
INVENTORY_SYNC_SECONDS = get_int_env("INVENTORY_SYNC_SECONDS", 60, 0)
VERIFY_SSL = os.getenv("VERIFY_SSL", "1") != "0"
POLL_JITTER = float(os.getenv("POLL_JITTER", "0.15") or "0.15")
MAX_OUTPUT_BYTES = get_int_env("MAX_OUTPUT_BYTES", 20000, 1024)
OUTPUT_BATCH_WINDOW = 0.15
KEEPALIVE_SECONDS = max(5, min(30, DEFAULT_POLL_SECONDS))
SYSTEM_INFO_REFRESH_SECONDS = get_int_env("SYSTEM_INFO_REFRESH_SECONDS", 300, 30)


def log(message: Any) -> None:
    # PyInstaller --noconsole builds have sys.stdout set to None.
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        print(message, file=stream, flush=True)


def detect_os_family() -> str:
    os_name = platform.system().strip().lower()
    if os_name.startswith("win"):
        return "windows"
    if os_name == "darwin":
        return "darwin"
    return "linux"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def is_safe_file_name(file_name: str) -> bool:
    if not file_name or len(file_name) > 128:
        return False
    if "/" in file_name or "\\" in file_name:
        return False
    if file_name in {".", ".."} or file_name.endswith((".", " ")):
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", file_name):
        return False
    stem = file_name.split(".", 1)[0].upper()
    return stem not in WINDOWS_RESERVED


def jittered(seconds: float) -> float:
    spread = seconds * max(0.0, min(POLL_JITTER, 0.9))
    return max(0.2, seconds + random.uniform(-spread, spread))


class CpuSampler:
    """Percent CPU usage from platform counters, with no external dependency."""

    def __init__(self, os_family: str) -> None:
        self.os_family = os_family
        self._last: tuple[float, float] | None = None
        self._lock = threading.Lock()

    def _windows_times(self) -> tuple[float, float]:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32),
            ]

        idle = FileTime()
        kernel = FileTime()
        user = FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError("GetSystemTimes failed")

        def as_int(value: FileTime) -> int:
            return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

        idle_total = as_int(idle)
        total = as_int(kernel) + as_int(user)
        return float(idle_total), float(total)

    def _linux_times(self) -> tuple[float, float]:
        first_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = [int(value) for value in first_line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return float(idle), float(sum(parts))

    def sample(self) -> float | None:
        with self._lock:
            try:
                if self.os_family == "windows":
                    idle, total = self._windows_times()
                elif self.os_family == "linux":
                    idle, total = self._linux_times()
                else:
                    # macOS: load average is the cheapest dependency-free proxy.
                    cores = max(1, os.cpu_count() or 1)
                    return round(min(100.0, (os.getloadavg()[0] / cores) * 100.0), 2)
            except Exception:
                return None

            if self._last is None:
                self._last = (idle, total)
                return None
            idle_delta = idle - self._last[0]
            total_delta = total - self._last[1]
            self._last = (idle, total)
            if total_delta <= 0:
                return None
            busy = max(0.0, total_delta - idle_delta)
            return round(min(100.0, (busy / total_delta) * 100.0), 2)


class AgentRuntime:
    def __init__(self) -> None:
        self.session = self._build_session()
        self.output_session = self._build_session()

        self.agent_id = ""
        self.agent_token = ""
        self.poll_interval = DEFAULT_POLL_SECONDS
        self.hostname = socket.gethostname()
        self.os_family = detect_os_family()
        self.platform_name = platform.platform()
        self.cpu_sampler = CpuSampler(self.os_family)
        self.last_inventory_sync = 0.0
        self.last_health_sent = 0.0
        self.pending_report: tuple[str, str, str, str, int] | None = None
        self.agent_version = "1.1"
        self._stop_event = threading.Event()
        self._system_info_cache: dict[str, Any] | None = None
        self._system_info_cached_at = 0.0

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        session.verify = VERIFY_SSL
        retry = Retry(
            total=2,
            backoff_factor=0.4,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    # ---------- host telemetry ----------

    def memory_info(self) -> tuple[int | None, int | None]:
        if self.os_family == "windows":
            try:
                import ctypes

                class MemoryStatusEx(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                status = MemoryStatusEx()
                status.dwLength = ctypes.sizeof(MemoryStatusEx)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    return int(status.ullTotalPhys), int(status.ullAvailPhys)
            except (AttributeError, OSError, ValueError):
                pass
            return None, None
        if self.os_family == "linux":
            try:
                values: dict[str, int] = {}
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                    key, raw_value = line.split(":", 1)
                    values[key] = int(raw_value.strip().split()[0]) * 1024
                return values.get("MemTotal"), values.get("MemAvailable")
            except (OSError, ValueError):
                pass
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            return page * os.sysconf("SC_PHYS_PAGES"), page * os.sysconf("SC_AVPHYS_PAGES")
        except (AttributeError, ValueError, OSError):
            return None, None

    def system_info(self, force: bool = False) -> dict[str, Any]:
        if (
            not force
            and self._system_info_cache is not None
            and (time.time() - self._system_info_cached_at) < SYSTEM_INFO_REFRESH_SECONDS
        ):
            return dict(self._system_info_cache)

        total_memory, _ = self.memory_info()
        try:
            disk_total = shutil.disk_usage(str(BASE_DIR)).total
        except OSError:
            disk_total = None
        cpu_model = platform.processor()
        if self.os_family == "windows" and not cpu_model:
            cpu_model = os.getenv("PROCESSOR_IDENTIFIER", "")
        if self.os_family == "linux" and not cpu_model:
            try:
                for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                    if line.lower().startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass
        if not cpu_model:
            cpu_model = platform.machine()
        boot_time = None
        if self.os_family == "linux":
            try:
                for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
                    if line.startswith("btime "):
                        boot_time = datetime.fromtimestamp(int(line.split()[1]), UTC).isoformat()
                        break
            except (OSError, ValueError):
                pass
        elif self.os_family == "windows":
            try:
                import ctypes

                uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
                boot_time = (
                    datetime.now(UTC) - timedelta(seconds=uptime_ms / 1000)
                ).replace(microsecond=0).isoformat()
            except (AttributeError, OSError, ValueError):
                pass
        try:
            ip_addresses = sorted(set(socket.gethostbyname_ex(self.hostname)[2]))
        except OSError:
            ip_addresses = []
        info = {
            "agent_version": self.agent_version,
            "os_family": self.os_family,
            "platform": self.platform_name,
            "architecture": platform.machine(),
            "cpu_model": cpu_model,
            "logical_cpu_count": os.cpu_count(),
            "memory_total_bytes": total_memory,
            "disk_total_bytes": disk_total,
            "boot_time": boot_time,
            "ip_addresses": ip_addresses,
        }
        self._system_info_cache = info
        self._system_info_cached_at = time.time()
        return dict(info)

    def health_info(self) -> dict[str, Any]:
        total_memory, available_memory = self.memory_info()
        memory_percent = None
        if total_memory and available_memory is not None:
            memory_percent = round((1 - available_memory / total_memory) * 100, 2)
        disk_percent = None
        try:
            disk = shutil.disk_usage(str(BASE_DIR))
            disk_percent = round((disk.used / disk.total) * 100, 2) if disk.total else None
        except OSError:
            pass
        load_1m = None
        uptime_seconds = None
        if self.os_family == "linux":
            try:
                load_1m = os.getloadavg()[0]
            except OSError:
                pass
            try:
                uptime_seconds = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
            except (OSError, ValueError):
                pass
        elif self.os_family == "windows":
            try:
                import ctypes

                uptime_seconds = int(ctypes.windll.kernel32.GetTickCount64() // 1000)
            except (AttributeError, OSError, ValueError):
                pass
        return {
            "cpu_percent": self.cpu_sampler.sample(),
            "memory_percent": memory_percent,
            "memory_used_bytes": total_memory - available_memory if total_memory and available_memory is not None else None,
            "disk_percent": disk_percent,
            "load_1m": load_1m,
            "uptime_seconds": uptime_seconds,
            "battery_percent": None,
        }

    # ---------- state ----------

    def load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        self.agent_id = str(payload.get("agent_id", ""))
        self.agent_token = str(payload.get("agent_token", ""))
        try:
            self.poll_interval = max(1, int(payload.get("poll_interval_seconds", DEFAULT_POLL_SECONDS)))
        except (TypeError, ValueError):
            self.poll_interval = DEFAULT_POLL_SECONDS
        pending_report = payload.get("pending_report")
        if isinstance(pending_report, list) and len(pending_report) == 5:
            try:
                self.pending_report = (
                    str(pending_report[0]),
                    str(pending_report[1]),
                    str(pending_report[2]),
                    str(pending_report[3]),
                    int(pending_report[4]),
                )
            except (TypeError, ValueError):
                self.pending_report = None

    def save_state(self) -> None:
        payload = {
            "agent_id": self.agent_id,
            "agent_token": self.agent_token,
            "poll_interval_seconds": self.poll_interval,
            "server_url": SERVER_URL,
            "os_family": self.os_family,
            "platform": self.platform_name,
            "pending_report": list(self.pending_report) if self.pending_report else None,
        }
        tmp_name = None
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(STATE_FILE.parent),
                prefix=".state-",
                delete=False,
            ) as handle:
                tmp_name = handle.name
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, STATE_FILE)
            if os.name == "posix":
                os.chmod(STATE_FILE, 0o600)
        except OSError:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    # ---------- server calls ----------

    def register(self) -> None:
        payload = {
            "agent_id": self.agent_id,
            "hostname": self.hostname,
            "os_family": self.os_family,
            "platform": self.platform_name,
            "system": self.system_info(),
            "health": self.health_info(),
        }
        response = self.session.post(
            f"{SERVER_URL}/api/register",
            headers={"X-Register-Token": REGISTER_TOKEN},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        body = response.json()
        self.agent_id = body["agent_id"]
        self.agent_token = body["agent_token"]
        self.poll_interval = max(1, min(int(body.get("poll_interval_seconds", DEFAULT_POLL_SECONDS)), 300))
        self.save_state()
        log(f"Registered as {self.agent_id} ({self.hostname}, os={self.os_family})")

    def heartbeat(self) -> None:
        response = self.session.post(
            f"{SERVER_URL}/api/heartbeat/{self.agent_id}",
            headers={"X-Agent-Token": self.agent_token},
            json={"sent_at": utc_now(), "system": self.system_info(), "health": self.health_info()},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError("Heartbeat unauthorized; registration required")
        response.raise_for_status()
        self.last_health_sent = time.time()

    def poll(self) -> dict[str, Any] | None:
        response = self.session.get(
            f"{SERVER_URL}/api/poll/{self.agent_id}",
            headers={"X-Agent-Token": self.agent_token},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError("Agent unauthorized; registration required")
        response.raise_for_status()
        return response.json().get("task")

    def inventory_payload(self) -> list[dict[str, Any]]:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        for path in sorted(SCRIPTS_DIR.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                files.append(
                    {
                        "file_name": path.name,
                        "rel_path": str(path.relative_to(SCRIPTS_DIR)).replace("\\", "/"),
                        "file_size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    }
                )
            except OSError:
                continue
        return files

    def sync_inventory(self) -> None:
        response = self.session.post(
            f"{SERVER_URL}/api/inventory/{self.agent_id}",
            headers={"X-Agent-Token": self.agent_token},
            json={"files": self.inventory_payload(), "sent_at": utc_now()},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError("Inventory sync unauthorized; registration required")
        response.raise_for_status()
        self.last_inventory_sync = time.time()

    def maybe_sync_inventory(self, force: bool = False) -> None:
        if force or (time.time() - self.last_inventory_sync) >= INVENTORY_SYNC_SECONDS:
            self.sync_inventory()

    def write_script_file(self, file_name: str, content: bytes) -> Path:
        if not is_safe_file_name(file_name):
            raise ValueError("Unsafe file name")
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        target = SCRIPTS_DIR / file_name
        tmp = target.with_name(f".{target.name}.part")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return target

    def should_stop_task(self, task_id: str) -> bool:
        try:
            response = self.session.get(
                f"{SERVER_URL}/api/task-control/{self.agent_id}/{task_id}",
                headers={"X-Agent-Token": self.agent_token},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 401:
                raise PermissionError("Task control unauthorized; registration required")
            response.raise_for_status()
            return response.json().get("action") == "stop"
        except requests.RequestException:
            return False

    def send_output_chunks(self, task_id: str, attempt_number: int, chunks: list[tuple[str, int, str]]) -> None:
        if not chunks:
            return
        try:
            response = self.output_session.post(
                f"{SERVER_URL}/api/task-output/{self.agent_id}/{task_id}",
                headers={"X-Agent-Token": self.agent_token},
                json={
                    "attempt_number": attempt_number,
                    "chunks": [
                        {"stream": stream, "sequence": seq, "text": text, "eof": False}
                        for stream, seq, text in chunks
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 401:
                raise PermissionError("Task output unauthorized; registration required")
        except requests.RequestException:
            return

    def terminate_process(self, proc: subprocess.Popen, force: bool = False) -> None:
        if proc.poll() is not None:
            return
        if self.os_family == "windows":
            command = ["taskkill", "/PID", str(proc.pid), "/T"]
            if force:
                command.append("/F")
            subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

    def run_process(
        self,
        cmd: list[str],
        timeout_seconds: int,
        task_id: str,
        allow_nonzero_success: bool,
        attempt_number: int = 1,
    ) -> tuple[str, str, str]:
        output_parts: list[str] = []
        error_parts: list[str] = []
        chunk_queue: queue.Queue[tuple[str, int, str] | None] = queue.Queue(maxsize=2048)
        captured = {"stdout": 0, "stderr": 0}
        sequences = {"stdout": 0, "stderr": 0}

        def collect(stream: Any, stream_name: str) -> None:
            try:
                for chunk in iter(stream.readline, ""):
                    if not chunk:
                        break
                    if captured[stream_name] < MAX_OUTPUT_BYTES:
                        text = chunk[: MAX_OUTPUT_BYTES - captured[stream_name]]
                        captured[stream_name] += len(text)
                        parts = output_parts if stream_name == "stdout" else error_parts
                        parts.append(text)
                        try:
                            chunk_queue.put((stream_name, sequences[stream_name], text), timeout=1)
                        except queue.Full:
                            pass
                        sequences[stream_name] += 1
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        def sender() -> None:
            batch: list[tuple[str, int, str]] = []
            deadline = time.time() + OUTPUT_BATCH_WINDOW
            while True:
                try:
                    item = chunk_queue.get(timeout=max(0.01, deadline - time.time()))
                except queue.Empty:
                    if batch:
                        self.send_output_chunks(task_id, attempt_number, batch)
                        batch = []
                    deadline = time.time() + OUTPUT_BATCH_WINDOW
                    continue
                if item is None:
                    break
                batch.append(item)
                if len(batch) >= 32:
                    self.send_output_chunks(task_id, attempt_number, batch)
                    batch = []
                    deadline = time.time() + OUTPUT_BATCH_WINDOW
            if batch:
                self.send_output_chunks(task_id, attempt_number, batch)

        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if self.os_family == "windows":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc: subprocess.Popen | None = None
        stdout_thread = stderr_thread = sender_thread = None
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            sender_thread = threading.Thread(target=sender, daemon=True)
            stdout_thread = threading.Thread(target=collect, args=(proc.stdout, "stdout"), daemon=True)
            stderr_thread = threading.Thread(target=collect, args=(proc.stderr, "stderr"), daemon=True)
            sender_thread.start()
            stdout_thread.start()
            stderr_thread.start()

            started = time.time()
            last_control_check = 0.0
            last_keepalive = started
            final_status: str | None = None
            final_error = ""
            while proc.poll() is None:
                now_ts = time.time()
                if now_ts - last_control_check >= 1.0:
                    last_control_check = now_ts
                    if self.should_stop_task(task_id):
                        final_status = "killed"
                        final_error = "Stopped by admin"
                        self.terminate_process(proc)
                        break
                if now_ts - last_keepalive >= KEEPALIVE_SECONDS:
                    last_keepalive = now_ts
                    try:
                        self.heartbeat()
                    except (requests.RequestException, PermissionError):
                        # The report path surfaces auth failures once the task ends.
                        pass
                if timeout_seconds > 0 and (now_ts - started) > timeout_seconds:
                    final_status = "failed"
                    final_error = f"Timed out after {timeout_seconds}s"
                    self.terminate_process(proc)
                    break
                time.sleep(0.2)

            if final_status is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.terminate_process(proc, force=True)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            if sender_thread.is_alive():
                while sender_thread.is_alive():
                    try:
                        chunk_queue.put(None, timeout=1)
                        break
                    except queue.Full:
                        continue
            sender_thread.join(timeout=15)

            output = "".join(output_parts).strip()[:MAX_OUTPUT_BYTES]
            error = "".join(error_parts).strip()[:MAX_OUTPUT_BYTES]
            if final_status == "killed":
                return "killed", output, final_error
            if final_status == "failed":
                return "failed", output, final_error
            status = "completed" if (proc.returncode == 0 or allow_nonzero_success) else "failed"
            if not error and proc.returncode != 0 and not allow_nonzero_success:
                error = f"Failed with return code {proc.returncode}"
            return status, output, error
        except Exception as exc:
            return "failed", "", f"Execution error: {exc}"
        finally:
            if proc is not None and proc.poll() is None:
                self.terminate_process(proc, force=True)

    def execute_command(
        self,
        command: str,
        timeout_seconds: int,
        task_id: str,
        allow_nonzero_success: bool,
        attempt_number: int = 1,
    ) -> tuple[str, str, str]:
        if self.os_family == "windows":
            cmd = ["cmd", "/c", command]
        else:
            cmd = ["/bin/bash", "-lc", command]
        return self.run_process(cmd, timeout_seconds, task_id, allow_nonzero_success, attempt_number)

    def python_interpreter(self) -> str | None:
        # In a frozen build sys.executable is the agent itself, so using it to
        # run a .py file would relaunch the agent instead of the script.
        if not getattr(sys, "frozen", False):
            return sys.executable
        candidates = ["py", "python3", "python"] if self.os_family == "windows" else ["python3", "python"]
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    def execute_file(
        self,
        script_path: Path,
        timeout_seconds: int,
        task_id: str,
        allow_nonzero_success: bool,
        attempt_number: int = 1,
    ) -> tuple[str, str, str]:
        ext = script_path.suffix.lower()
        if self.os_family == "windows":
            if ext in {".bat", ".cmd"}:
                cmd = ["cmd", "/c", str(script_path)]
            elif ext == ".ps1":
                cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
            elif ext == ".vbs":
                cmd = ["cscript", "//NoLogo", str(script_path)]
            elif ext == ".py":
                interpreter = self.python_interpreter()
                if not interpreter:
                    return "failed", "", "No Python interpreter is available on the host"
                cmd = [interpreter, str(script_path)]
            else:
                cmd = ["cmd", "/c", str(script_path)]
        else:
            if ext == ".py":
                interpreter = self.python_interpreter()
                if not interpreter:
                    return "failed", "", "No Python interpreter is available on the host"
                cmd = [interpreter, str(script_path)]
            elif ext in {".sh", ".bash"}:
                cmd = ["/bin/bash", str(script_path)]
            else:
                cmd = ["/bin/bash", str(script_path)]
        return self.run_process(cmd, timeout_seconds, task_id, allow_nonzero_success, attempt_number)

    def run_task(self, task: dict[str, Any]) -> tuple[str, str, str]:
        task_id = str(task.get("task_id", ""))
        attempt_number = int(task.get("attempt_number", 1))
        execution = task.get("execution") or {}
        mode = str(execution.get("mode", "")).strip()

        if mode == "command":
            command = str(execution.get("command", "")).strip()
            raw_timeout = int(execution.get("timeout_seconds", 120))
            timeout_seconds = 0 if raw_timeout == 0 else max(5, min(raw_timeout, 86400))
            allow_nonzero_success = bool(execution.get("allow_nonzero_success", False))
            if not command:
                return "failed", "", "No command provided"
            return self.execute_command(command, timeout_seconds, task_id, allow_nonzero_success, attempt_number)

        if mode == "file_inline":
            file_name = str(execution.get("file_name", "")).strip()
            content = execution.get("content", "")
            content_encoding = str(execution.get("content_encoding", "utf8")).strip().lower()
            expected_sha256 = str(execution.get("sha256", "")).strip().lower()
            raw_timeout = int(execution.get("timeout_seconds", 120))
            timeout_seconds = 0 if raw_timeout == 0 else max(5, min(raw_timeout, 86400))
            allow_nonzero_success = bool(execution.get("allow_nonzero_success", False))
            if not file_name or not is_safe_file_name(file_name):
                return "failed", "", "Unsafe or missing file name"
            try:
                script_bytes = (
                    base64.b64decode(str(content).encode("ascii"))
                    if content_encoding == "base64"
                    else str(content).encode("utf-8")
                )
                if expected_sha256:
                    actual_sha256 = hashlib.sha256(script_bytes).hexdigest().lower()
                    if actual_sha256 != expected_sha256:
                        return "failed", "", "File integrity check failed (sha256 mismatch)"
                script_path = self.write_script_file(file_name, script_bytes)
                status, output, error = self.execute_file(
                    script_path, timeout_seconds, task_id, allow_nonzero_success, attempt_number
                )
                self._sync_inventory_safe()
                return status, output, error
            except Exception as exc:
                return "failed", "", f"File deployment error: {exc}"

        if mode == "deploy_file":
            file_name = str(execution.get("file_name", "")).strip()
            content = execution.get("content", "")
            content_encoding = str(execution.get("content_encoding", "utf8")).strip().lower()
            expected_sha256 = str(execution.get("sha256", "")).strip().lower()
            if not file_name or not is_safe_file_name(file_name):
                return "failed", "", "Unsafe or missing file name"
            try:
                script_bytes = (
                    base64.b64decode(str(content).encode("ascii"))
                    if content_encoding == "base64"
                    else str(content).encode("utf-8")
                )
                if expected_sha256:
                    actual_sha256 = hashlib.sha256(script_bytes).hexdigest().lower()
                    if actual_sha256 != expected_sha256:
                        return "failed", "", "File integrity check failed (sha256 mismatch)"
                self.write_script_file(file_name, script_bytes)
                self._sync_inventory_safe()
                return "completed", f"Transferred {file_name}", ""
            except Exception as exc:
                return "failed", "", f"Transfer error: {exc}"

        if mode == "agent_file":
            file_name = str(execution.get("file_name", "")).strip()
            raw_timeout = int(execution.get("timeout_seconds", 180))
            timeout_seconds = 0 if raw_timeout == 0 else max(5, min(raw_timeout, 86400))
            allow_nonzero_success = bool(execution.get("allow_nonzero_success", False))
            if not file_name or not is_safe_file_name(file_name):
                return "failed", "", "Unsafe or missing file name"
            script_path = SCRIPTS_DIR / file_name
            if not script_path.exists() or not script_path.is_file():
                return "failed", "", f"File not found in scripts folder: {file_name}"
            return self.execute_file(script_path, timeout_seconds, task_id, allow_nonzero_success, attempt_number)

        return "failed", "", f"Unsupported execution mode: {mode}"

    def _sync_inventory_safe(self) -> None:
        try:
            self.sync_inventory()
        except requests.RequestException as exc:
            log(f"Inventory sync deferred: {exc}")

    # ---------- reporting ----------

    def report(self, task_id: str, status: str, output: str, error: str, attempt_number: int = 1) -> None:
        response = self.session.post(
            f"{SERVER_URL}/api/report/{self.agent_id}/{task_id}",
            headers={"X-Agent-Token": self.agent_token},
            json={"status": status, "output": output, "error": error, "attempt_number": attempt_number},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError("Report rejected; registration required")
        response.raise_for_status()

    def flush_pending_report(self) -> bool:
        if not self.pending_report:
            return True
        try:
            self.report(*self.pending_report)
        except PermissionError:
            raise
        except requests.RequestException:
            return False
        self.pending_report = None
        self.save_state()
        return True

    # ---------- main loop ----------

    def _sleep(self, seconds: float) -> None:
        self._stop_event.wait(timeout=jittered(seconds))

    def request_stop(self, *_args: Any) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        self.load_state()
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self.request_stop)
                except (OSError, ValueError):
                    pass

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                if not self.agent_token:
                    self.register()
                    self.heartbeat()
                    self.sync_inventory()
                    backoff = 1.0
                    self._sleep(1)
                    continue

                self.heartbeat()
                self.maybe_sync_inventory()
                if not self.flush_pending_report():
                    self._sleep(self.poll_interval)
                    continue
                task = self.poll()
                backoff = 1.0
                if not task:
                    self._sleep(self.poll_interval)
                    continue

                task_id = str(task.get("task_id", ""))
                log(f"Running task {task_id}: {task.get('type')}:{task.get('value')}")
                status, output, error = self.run_task(task)
                self.pending_report = (
                    task_id,
                    status,
                    output,
                    error,
                    int(task.get("attempt_number", 1)),
                )
                self.save_state()
                self.flush_pending_report()
                log(f"Task {task_id} finished with status={status}")
                self._sleep(1)
            except PermissionError as auth_error:
                log(auth_error)
                self.agent_token = ""
                self.save_state()
                backoff = 1.0
                self._sleep(2)
            except requests.RequestException as req_error:
                log(f"Network issue: {req_error}")
                self._sleep(backoff)
                backoff = min(backoff * 1.7, max(5.0, self.poll_interval * 3))
            except Exception as exc:
                log(f"Unexpected failure: {exc}")
                self._sleep(self.poll_interval)

        log("Agent stopped.")


def main() -> None:
    AgentRuntime().run_forever()


if __name__ == "__main__":
    main()
