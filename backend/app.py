"""Cron UI - a small web app to schedule and run scripts via a real cron
scheduler, designed to run as root inside a container."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
from config import AUTH_TOKEN, DEFAULT_TZ, ensure_dirs
from scheduler import kuma_push, next_run_after, scheduler, validate_schedule

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    db.init()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Cron UI", version="1.0.0", lifespan=lifespan)


# ----------------------------------------------------------------- auth
async def require_auth(authorization: Optional[str] = Header(default=None),
                       x_auth_token: Optional[str] = Header(default=None)):
    if not AUTH_TOKEN:
        return
    token = x_auth_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


# ----------------------------------------------------------------- schemas
class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    schedule: str
    script: str = ""
    enabled: bool = True
    timezone: Optional[str] = None
    env: dict[str, str] = {}
    timeout: int = 0
    working_dir: Optional[str] = None
    kuma_url: Optional[str] = None


class JobPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    schedule: Optional[str] = None
    script: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    env: Optional[dict[str, str]] = None
    timeout: Optional[int] = None
    working_dir: Optional[str] = None
    kuma_url: Optional[str] = None


def _decorate(job: dict[str, Any]) -> dict[str, Any]:
    st = scheduler.status().get(job["id"], {})
    job = dict(job)
    job["next_run"] = st.get("next_run") or (
        next_run_after(job["schedule"], job["timezone"]) if job["enabled"] else None
    )
    job["is_running"] = st.get("is_running", False)
    job["last_run"] = db.last_run(job["id"])
    return job


# ----------------------------------------------------------------- API
@app.get("/api/health")
async def health():
    return {"status": "ok", "tz": DEFAULT_TZ, "uid": os.getuid(), "auth": bool(AUTH_TOKEN)}


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def list_jobs():
    return [_decorate(j) for j in db.list_jobs()]


@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def create_job(payload: JobIn):
    if not validate_schedule(payload.schedule):
        raise HTTPException(400, "invalid cron expression")
    job = db.create_job(payload.model_dump())
    scheduler.write_script(job["id"], payload.script)
    scheduler.recompute_job(job["id"])
    return _decorate(db.get_job(job["id"]))


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return _decorate(job)


@app.patch("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def update_job(job_id: int, payload: JobPatch):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    data = payload.model_dump(exclude_unset=True)
    if "schedule" in data and not validate_schedule(data["schedule"]):
        raise HTTPException(400, "invalid cron expression")
    job = db.update_job(job_id, data)
    if "script" in data:
        scheduler.write_script(job_id, data["script"])
    scheduler.recompute_job(job_id)
    return _decorate(job)


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def delete_job(job_id: int):
    if not db.get_job(job_id):
        raise HTTPException(404, "job not found")
    db.delete_job(job_id)
    scheduler.recompute_job(job_id)
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/run", dependencies=[Depends(require_auth)])
async def run_job(job_id: int):
    if not db.get_job(job_id):
        raise HTTPException(404, "job not found")
    if scheduler.is_running(job_id):
        raise HTTPException(409, "job is already running")
    run_id = scheduler.run_now(job_id)
    if run_id is None:
        raise HTTPException(500, "failed to start job")
    return {"run_id": run_id}


@app.post("/api/jobs/{job_id}/toggle", dependencies=[Depends(require_auth)])
async def toggle_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job = db.update_job(job_id, {"enabled": not job["enabled"]})
    scheduler.recompute_job(job_id)
    return _decorate(job)


@app.get("/api/jobs/{job_id}/runs", dependencies=[Depends(require_auth)])
async def job_runs(job_id: int, limit: int = 50):
    if not db.get_job(job_id):
        raise HTTPException(404, "job not found")
    return db.list_runs(job_id, limit=limit)


@app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse,
         dependencies=[Depends(require_auth)])
async def run_log(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    path = run.get("log_path")
    if not path or not os.path.isfile(path):
        return PlainTextResponse("(no log output)")
    with open(path, "r", errors="replace") as fh:
        return PlainTextResponse(fh.read())


@app.post("/api/validate", dependencies=[Depends(require_auth)])
async def validate(payload: dict):
    expr = payload.get("schedule", "")
    ok = validate_schedule(expr)
    return {
        "valid": ok,
        "next_run": next_run_after(expr, payload.get("timezone")) if ok else None,
    }


@app.post("/api/kuma/test", dependencies=[Depends(require_auth)])
async def kuma_test(payload: dict):
    url = (payload.get("kuma_url") or "").strip()
    if not url:
        raise HTTPException(400, "kuma_url is required")
    ok = kuma_push(url, "up", "cron-ui test heartbeat", ping_ms=1)
    return {"ok": ok}


# ----------------------------------------------------------------- static UI
@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
