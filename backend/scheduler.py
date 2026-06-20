"""Cron scheduler: computes due jobs and executes their scripts as the
container user (root by default), capturing output to per-run log files."""
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

import db
from config import (
    DEFAULT_SHELL,
    DEFAULT_TIMEOUT,
    DEFAULT_TZ,
    LOGS_DIR,
    SCRIPTS_DIR,
    TICK_SECONDS,
)


def validate_schedule(expr: str) -> bool:
    return croniter.is_valid(expr)


def kuma_push(base_url: str, status: str, msg: str = "", ping_ms: Optional[int] = None) -> bool:
    """Send an Uptime Kuma push heartbeat.

    `base_url` is the Kuma push URL, e.g. http://host:3001/api/push/<token>.
    Uptime Kuma's UI displays this URL with an *example* query string already
    appended (``?status=up&msg=OK&ping=``). We must NOT keep that: appending a
    second query string creates duplicate ``status``/``msg`` params, which Kuma
    (Express qs) parses as arrays — ``status`` then != "up", so the monitor is
    recorded DOWN. So we drop any existing query and send only our own params.
    Returns True on a 2xx response with ``ok: true``.
    """
    if not base_url:
        return False
    # Keep only the scheme://host/path part (the token); discard any query/fragment.
    base = base_url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    params = {"status": status, "msg": msg or "OK"}
    if ping_ms is not None:
        params["ping"] = str(int(ping_ms))
    url = f"{base}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if not (200 <= resp.status < 300):
                    raise OSError(f"HTTP {resp.status}")
                body = resp.read(4096).decode("utf-8", "replace").lower()
                # Kuma returns {"ok":true}; treat an explicit ok:false as failure.
                return '"ok":false' not in body.replace(" ", "")
        except (urllib.error.URLError, OSError, ValueError):
            if attempt < 2:
                time.sleep(2)
    return False


def _tz(name: Optional[str]) -> ZoneInfo:
    for candidate in (name, DEFAULT_TZ, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return ZoneInfo("UTC")


def next_run_after(expr: str, tz_name: Optional[str], after: Optional[float] = None) -> Optional[float]:
    """Return the next fire time (unix ts) strictly after `after`."""
    if not validate_schedule(expr):
        return None
    tz = _tz(tz_name)
    base = datetime.fromtimestamp(after if after is not None else time.time(), tz)
    return croniter(expr, base).get_next(float)


class Scheduler:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # job_id -> next scheduled unix ts
        self._next: dict[int, float] = {}
        # job_id -> Popen of an active run (cron or manual)
        self._running: dict[int, subprocess.Popen] = {}

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        db.mark_orphan_runs()
        self._sync_scripts()
        self._reload_schedule()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def _sync_scripts(self) -> None:
        """Rewrite every job's script file from the DB so on-disk scripts match
        persisted state after a restart (or a wiped/fresh scripts dir)."""
        for job in db.list_jobs():
            try:
                self.write_script(job["id"], job.get("script", ""))
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def reload(self) -> None:
        """Recompute next-fire times after a job is added/edited/removed."""
        self._reload_schedule()

    def _reload_schedule(self) -> None:
        now = time.time()
        with self._lock:
            live_ids = set()
            for job in db.list_jobs():
                live_ids.add(job["id"])
                if not job["enabled"]:
                    self._next.pop(job["id"], None)
                    continue
                # Preserve an existing pending fire time so edits don't skip a run.
                if job["id"] not in self._next:
                    nxt = next_run_after(job["schedule"], job["timezone"], now)
                    if nxt is not None:
                        self._next[job["id"]] = nxt
            # Drop deleted/disabled jobs.
            for jid in list(self._next):
                if jid not in live_ids:
                    self._next.pop(jid, None)

    def recompute_job(self, job_id: int) -> None:
        job = db.get_job(job_id)
        with self._lock:
            self._next.pop(job_id, None)
            if job and job["enabled"]:
                nxt = next_run_after(job["schedule"], job["timezone"], time.time())
                if nxt is not None:
                    self._next[job_id] = nxt

    # ----------------------------------------------------------- status
    def status(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            return {
                jid: {
                    "next_run": self._next.get(jid),
                    "is_running": jid in self._running,
                }
                for jid in set(self._next) | set(self._running)
            }

    def is_running(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._running

    # ----------------------------------------------------------- main loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            due: list[int] = []
            with self._lock:
                for jid, when in list(self._next.items()):
                    if when <= now and jid not in self._running:
                        due.append(jid)
            for jid in due:
                job = db.get_job(jid)
                if not job or not job["enabled"]:
                    with self._lock:
                        self._next.pop(jid, None)
                    continue
                self._fire(job, trigger="cron")
                with self._lock:
                    nxt = next_run_after(job["schedule"], job["timezone"], now)
                    if nxt is not None:
                        self._next[jid] = nxt
                    else:
                        self._next.pop(jid, None)
            self._stop.wait(TICK_SECONDS)

    # ----------------------------------------------------------- execution
    def _script_path(self, job_id: int) -> Path:
        return SCRIPTS_DIR / f"job_{job_id}.sh"

    def write_script(self, job_id: int, body: str) -> Path:
        path = self._script_path(job_id)
        if body and not body.startswith("#!"):
            body = "#!/bin/sh\n" + body
        path.write_text(body or "#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
        return path

    def run_now(self, job_id: int) -> Optional[int]:
        job = db.get_job(job_id)
        if not job:
            return None
        if self.is_running(job_id):
            return None
        return self._fire(job, trigger="manual")

    def _fire(self, job: dict[str, Any], trigger: str) -> Optional[int]:
        job_id = job["id"]
        script_path = self.write_script(job_id, job.get("script", ""))
        log_path = LOGS_DIR / f"run_{int(time.time() * 1000)}_{job_id}.log"
        run_id = db.start_run(job_id, trigger, str(log_path))

        env = os.environ.copy()
        env.update({k: str(v) for k, v in (job.get("env") or {}).items()})
        if job.get("timezone"):
            env["TZ"] = job["timezone"]

        timeout = job.get("timeout") or DEFAULT_TIMEOUT
        cwd = job.get("working_dir") or str(SCRIPTS_DIR)
        if not os.path.isdir(cwd):
            cwd = str(SCRIPTS_DIR)

        thread = threading.Thread(
            target=self._exec,
            args=(job, run_id, str(script_path), env, cwd, timeout, log_path),
            name=f"run-{run_id}",
            daemon=True,
        )
        thread.start()
        return run_id

    def _exec(self, job, run_id, script_path, env, cwd, timeout, log_path) -> None:
        job_id = job["id"]
        kuma_url = (job.get("kuma_url") or "").strip()
        started = time.time()
        # Honor the script's shebang (e.g. #!/usr/bin/env bash) by executing the
        # file directly; fall back to the default shell if it has no shebang.
        argv = [script_path] if self._has_shebang(script_path) else [DEFAULT_SHELL, script_path]
        header = (
            f"=== job {job_id} run {run_id} ===\n"
            f"started: {datetime.now().isoformat(timespec='seconds')}\n"
            f"exec:    {' '.join(argv)}\n"
            f"cwd:     {cwd}\n"
            f"user:    uid={os.getuid()} gid={os.getgid()}\n"
            f"{'-' * 48}\n"
        )
        status = "success"
        exit_code: Optional[int] = None
        try:
            with open(log_path, "w") as logf:
                logf.write(header)
                logf.flush()
                proc = subprocess.Popen(
                    argv,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                )
                with self._lock:
                    self._running[job_id] = proc
                try:
                    exit_code = proc.wait(timeout=timeout if timeout > 0 else None)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    exit_code = -1
                    status = "timeout"
                    logf.write(f"\n{'-' * 48}\n!! killed after {timeout}s timeout\n")
                else:
                    status = "success" if exit_code == 0 else "failed"
                logf.write(
                    f"\n{'-' * 48}\nfinished: "
                    f"{datetime.now().isoformat(timespec='seconds')} "
                    f"exit={exit_code} status={status}\n"
                )
        except Exception as exc:  # noqa: BLE001 - record any launch failure
            status = "failed"
            try:
                with open(log_path, "a") as logf:
                    logf.write(f"\n!! failed to execute: {exc}\n")
            except OSError:
                pass
        finally:
            with self._lock:
                self._running.pop(job_id, None)
            db.finish_run(run_id, status, exit_code)
            self._notify_kuma(kuma_url, job, status, exit_code, started, log_path)
            for stale in db.prune_runs(job_id):
                try:
                    os.remove(stale)
                except OSError:
                    pass

    @staticmethod
    def _has_shebang(path: str) -> bool:
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False

    @staticmethod
    def _notify_kuma(kuma_url, job, status, exit_code, started, log_path) -> None:
        if not kuma_url:
            return
        duration = max(0.0, time.time() - started)
        ok = status == "success"
        kuma_status = "up" if ok else "down"
        if ok:
            msg = f"{job['name']}: ok in {duration:.0f}s"
        else:
            msg = f"{job['name']}: {status} (exit {exit_code}) after {duration:.0f}s"
        try:
            sent = kuma_push(kuma_url, kuma_status, msg, ping_ms=int(duration * 1000))
            with open(log_path, "a") as logf:
                logf.write(
                    f"[cron-ui] uptime-kuma push '{kuma_status}': "
                    f"{'sent' if sent else 'FAILED'}\n"
                )
        except OSError:
            pass


scheduler = Scheduler()
