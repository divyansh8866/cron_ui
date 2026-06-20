"""Runtime configuration, all overridable via environment variables."""
import os
from pathlib import Path

# Root of all persistent state. In Docker this is a mounted volume (e.g. /DATA/cron-ui).
# Resolved to an absolute path so job execution is independent of the process cwd.
DATA_DIR = Path(os.environ.get("CRON_UI_DATA", "/data")).resolve()

SCRIPTS_DIR = DATA_DIR / "scripts"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "cron_ui.db"

# Default timezone used when a job does not specify its own.
DEFAULT_TZ = os.environ.get("TZ", "UTC")

# Default shell used to execute a job's script.
DEFAULT_SHELL = os.environ.get("CRON_UI_SHELL", "/bin/sh")

# How often (seconds) the scheduler wakes up to check for due jobs.
TICK_SECONDS = float(os.environ.get("CRON_UI_TICK", "5"))

# Hard cap on a single run's wall-clock time (seconds). 0 disables the timeout.
DEFAULT_TIMEOUT = int(os.environ.get("CRON_UI_TIMEOUT", "0"))

# Optional shared secret. When set, the UI/API require this token.
AUTH_TOKEN = os.environ.get("CRON_UI_TOKEN", "").strip()

# Trim run history per job beyond this many rows (0 = keep everything).
MAX_RUNS_PER_JOB = int(os.environ.get("CRON_UI_MAX_RUNS", "200"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, SCRIPTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
