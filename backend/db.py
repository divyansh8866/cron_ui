"""Thin SQLite data-access layer for jobs and run history."""
import json
import sqlite3
import threading
import time
from typing import Any, Optional

from config import DB_PATH, MAX_RUNS_PER_JOB

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    global _conn
    _conn = _connect()
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                schedule    TEXT NOT NULL,
                script      TEXT NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                timezone    TEXT,
                env         TEXT NOT NULL DEFAULT '{}',
                timeout     INTEGER NOT NULL DEFAULT 0,
                working_dir TEXT,
                kuma_url    TEXT,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER NOT NULL,
                trigger     TEXT NOT NULL,
                status      TEXT NOT NULL,
                exit_code   INTEGER,
                started_at  REAL NOT NULL,
                finished_at REAL,
                log_path    TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, started_at DESC);
            """
        )
        _migrate()
        _conn.commit()


def _migrate() -> None:
    """Apply additive schema changes to databases created by older versions."""
    cols = {row["name"] for row in _conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, ddl in (("kuma_url", "ALTER TABLE jobs ADD COLUMN kuma_url TEXT"),):
        if name not in cols:
            _conn.execute(ddl)


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["enabled"] = bool(job["enabled"])
    try:
        job["env"] = json.loads(job["env"]) if job["env"] else {}
    except json.JSONDecodeError:
        job["env"] = {}
    return job


# ---------------------------------------------------------------- jobs

def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return [_row_to_job(r) for r in rows]


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def create_job(data: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    with _lock:
        cur = _conn.execute(
            """INSERT INTO jobs (name, schedule, script, enabled, timezone, env,
                                 timeout, working_dir, kuma_url, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"],
                data["schedule"],
                data.get("script", ""),
                1 if data.get("enabled", True) else 0,
                data.get("timezone"),
                json.dumps(data.get("env", {})),
                int(data.get("timeout", 0)),
                data.get("working_dir"),
                data.get("kuma_url"),
                now,
                now,
            ),
        )
        _conn.commit()
        job_id = cur.lastrowid
    return get_job(job_id)


def update_job(job_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    fields = []
    values: list[Any] = []
    for key in ("name", "schedule", "script", "timezone", "working_dir", "kuma_url"):
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if "enabled" in data:
        fields.append("enabled=?")
        values.append(1 if data["enabled"] else 0)
    if "env" in data:
        fields.append("env=?")
        values.append(json.dumps(data["env"]))
    if "timeout" in data:
        fields.append("timeout=?")
        values.append(int(data["timeout"]))
    if not fields:
        return get_job(job_id)
    fields.append("updated_at=?")
    values.append(time.time())
    values.append(job_id)
    with _lock:
        _conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
        _conn.commit()
    return get_job(job_id)


def delete_job(job_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _conn.commit()


# ---------------------------------------------------------------- runs

def start_run(job_id: int, trigger: str, log_path: str) -> int:
    with _lock:
        cur = _conn.execute(
            """INSERT INTO runs (job_id, trigger, status, started_at, log_path)
               VALUES (?,?,?,?,?)""",
            (job_id, trigger, "running", time.time(), log_path),
        )
        _conn.commit()
        return cur.lastrowid


def finish_run(run_id: int, status: str, exit_code: Optional[int]) -> None:
    with _lock:
        _conn.execute(
            "UPDATE runs SET status=?, exit_code=?, finished_at=? WHERE id=?",
            (status, exit_code, time.time(), run_id),
        )
        _conn.commit()


def get_run(run_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(job_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def last_run(job_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def mark_orphan_runs() -> list[dict[str, Any]]:
    """Runs still 'running' at startup are leftovers from a crash/restart."""
    with _lock:
        rows = _conn.execute("SELECT * FROM runs WHERE status='running'").fetchall()
        _conn.execute(
            "UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'",
            (time.time(),),
        )
        _conn.commit()
    return [dict(r) for r in rows]


def prune_runs(job_id: int) -> list[str]:
    """Drop rows beyond MAX_RUNS_PER_JOB, returning removed log paths."""
    if MAX_RUNS_PER_JOB <= 0:
        return []
    with _lock:
        rows = _conn.execute(
            """SELECT id, log_path FROM runs WHERE job_id=?
               ORDER BY started_at DESC LIMIT -1 OFFSET ?""",
            (job_id, MAX_RUNS_PER_JOB),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            _conn.executemany("DELETE FROM runs WHERE id=?", [(i,) for i in ids])
            _conn.commit()
    return [r["log_path"] for r in rows if r["log_path"]]
