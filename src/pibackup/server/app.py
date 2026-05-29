"""FastAPI application: job config, run/snapshot reporting, and retention.

The server is the source of truth. Clients register, fetch their job config,
run backups (rsync to ``repo_target``), then report each run + snapshot here.
Reporting a run also prunes that job's expired snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Request
from pydantic import BaseModel

from pibackup import __version__
from pibackup.common.config import Config, JobSpec, load_config
from pibackup.common.db import init_db
from pibackup.common.auth import verify_session
from pibackup.common.store import Store
from pibackup.server import retention
from pibackup.server.dashboard import render_dashboard, render_login

# Name of the signed cookie that carries the dashboard login session.
SESSION_COOKIE = "pibackup_session"


# Request models live at module level so FastAPI can resolve them even with
# `from __future__ import annotations` turning the route hints into strings.
class ClientIn(BaseModel):
    name: str
    hostname: Optional[str] = None


class JobIn(BaseModel):
    name: str
    sources: list[str]
    retention_days: int = 30
    bwlimit_kbps: int = 0
    encrypted: bool = False
    schedule: Optional[str] = None


class RunIn(BaseModel):
    status: str
    bytes_transferred: int = 0
    message: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    snapshot_path: Optional[str] = None
    snapshot_size: int = 0
    encrypted: bool = False


class RunPatch(BaseModel):
    # progress tick (status omitted) ...
    percent: Optional[float] = None
    transferred: Optional[int] = None
    rate: Optional[str] = None
    eta: Optional[str] = None
    # ... or a terminal result (status = success|failure)
    status: Optional[str] = None
    bytes_transferred: int = 0
    message: str = ""
    snapshot_path: Optional[str] = None
    snapshot_size: int = 0
    encrypted: bool = False


class EnrollIn(BaseModel):
    name: str
    token: str
    hostname: Optional[str] = None
    ssh_public_key: Optional[str] = None


def _append_authorized_key(path: str, public_key: str, name: str) -> None:
    """Best-effort: add an enrolled client's SSH key to authorized_keys."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text() if p.exists() else ""
        if public_key.strip() in existing:
            return
        with p.open("a") as fh:
            fh.write(f"{public_key.strip()}  # pibackup:{name}\n")
        p.chmod(0o600)
    except OSError:
        pass


def _job_out(row: dict) -> dict:
    """Serialize a job row, decoding source_paths JSON back to a list."""
    return {
        "id": row["id"],
        "client_name": row.get("client_name"),
        "name": row["name"],
        "sources": json.loads(row["source_paths"]),
        "schedule": row.get("schedule"),
        "retention_days": row["retention_days"],
        "bwlimit_kbps": row["bwlimit_kbps"] or 0,
        "encrypted": bool(row["encrypted"]),
        "created_at": row.get("created_at"),
    }


def create_app(config: Optional[Config] = None):
    from fastapi import FastAPI, Form, HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse

    cfg = config or load_config()
    init_db(cfg.db_path)
    store = Store(cfg.db_path)
    repo_root = str(cfg.repo_dir)

    api = FastAPI(title="pibackup", version=__version__)

    def _require_client(name: str) -> int:
        client = store.get_client_by_name(name)
        if client is None:
            raise HTTPException(404, f"unknown client: {name}")
        return int(client["id"])

    def _logged_in(request: Request) -> bool:
        """True if the request carries a valid session cookie for the admin.

        With no administrator configured the dashboard stays locked (the login
        page tells the operator to set one with `pibackup admin set-password`).
        """
        admin = store.get_admin()
        if admin is None:
            return False
        token = request.cookies.get(SESSION_COOKIE, "")
        username = verify_session(token, admin["session_secret"])
        return username == admin["username"]

    # ---- meta ----
    @api.get("/health")
    def health():
        return {"status": "ok"}

    # ---- dashboard + auth ----
    @api.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not _logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return render_dashboard(store)

    @api.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if _logged_in(request):
            return RedirectResponse("/", status_code=303)
        return render_login(needs_setup=not store.has_admin())

    @api.post("/login")
    def login(username: str = Form(""), password: str = Form("")):
        from pibackup.common.auth import PasswordHash, sign_session, verify_password

        admin = store.get_admin()
        if admin is None:
            return HTMLResponse(render_login(needs_setup=True), status_code=503)
        stored = PasswordHash(admin["salt"], admin["password_hash"], admin["iterations"])
        if username != admin["username"] or not verify_password(password, stored):
            return HTMLResponse(
                render_login(error="Invalid username or password."), status_code=401
            )
        token = sign_session(admin["username"], admin["session_secret"])
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400, path="/"
        )
        return resp

    @api.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ---- enrollment ----
    @api.post("/enroll")
    def enroll(body: EnrollIn):
        if not store.consume_enroll_token(body.name, body.token):
            raise HTTPException(403, "invalid or already-used enrollment token")
        store.record_enrollment(body.name, body.hostname, body.ssh_public_key)
        # Ensure the client's repo directory exists so rsync can write to it.
        client_repo = Path(repo_root) / body.name
        client_repo.mkdir(parents=True, exist_ok=True)
        if cfg.authorized_keys and body.ssh_public_key:
            _append_authorized_key(cfg.authorized_keys, body.ssh_public_key, body.name)
        return {
            "ok": True,
            "repo_target": cfg.repo_target or str(cfg.repo_dir),
            "jobs": [_job_out(r) for r in store.jobs_for_client(body.name)],
        }

    # ---- clients ----
    @api.post("/clients")
    def register_client(body: ClientIn):
        cid = store.ensure_client(body.name, body.hostname)
        return {"id": cid, "name": body.name}

    @api.get("/clients")
    def list_clients():
        return store.list_clients()

    # ---- jobs ----
    @api.post("/clients/{client_name}/jobs")
    def create_job(client_name: str, body: JobIn):
        cid = _require_client(client_name)
        spec = JobSpec(
            name=body.name,
            sources=body.sources,
            retention_days=body.retention_days,
            bwlimit_kbps=body.bwlimit_kbps,
            encrypted=body.encrypted,
        )
        job_id = store.ensure_job(cid, spec)
        return _job_out(store.get_job(job_id))

    @api.get("/clients/{client_name}/jobs")
    def jobs_for_client(client_name: str):
        # Reads are lenient: an unknown client simply has no jobs.
        return [_job_out(row) for row in store.jobs_for_client(client_name)]

    @api.get("/jobs")
    def list_jobs():
        return [_job_out(row) for row in store.list_jobs()]

    @api.get("/jobs/{job_id}")
    def get_job(job_id: int):
        row = store.get_job(job_id)
        if row is None:
            raise HTTPException(404, f"unknown job: {job_id}")
        return _job_out(row)

    @api.delete("/jobs/{job_id}")
    def delete_job(job_id: int):
        if store.get_job(job_id) is None:
            raise HTTPException(404, f"unknown job: {job_id}")
        store.delete_job(job_id)
        return {"deleted": job_id}

    # ---- runs + snapshots ----
    def _finalize_run(run_id: int, job_id: int, status: str, bytes_transferred: int,
                      message: str, snapshot_path: Optional[str], snapshot_size: int,
                      encrypted: bool) -> dict:
        store.finish_run(run_id, status, bytes_transferred, message)
        snapshot_id = None
        if status == "success" and snapshot_path:
            snapshot_id = store.add_snapshot(job_id, run_id, snapshot_path, snapshot_size, encrypted)
        # Server owns retention: prune this job's expired snapshots now.
        pruned = retention.prune_job(store, job_id, repo_root)
        return {"run_id": run_id, "snapshot_id": snapshot_id, "pruned": len(pruned)}

    @api.post("/jobs/{job_id}/runs")
    def report_run(job_id: int, body: RunIn):
        if store.get_job(job_id) is None:
            raise HTTPException(404, f"unknown job: {job_id}")
        # status='running' opens a live run the client streams progress into;
        # any terminal status records a completed run in one shot (legacy path).
        if body.status == "running":
            return {"run_id": store.start_run(job_id)}
        run_id = store.record_run(
            job_id, body.status, body.bytes_transferred, body.message,
            body.started_at, body.finished_at,
        )
        snapshot_id = None
        if body.status == "success" and body.snapshot_path:
            snapshot_id = store.add_snapshot(
                job_id, run_id, body.snapshot_path, body.snapshot_size, body.encrypted,
            )
        pruned = retention.prune_job(store, job_id, repo_root)
        return {"run_id": run_id, "snapshot_id": snapshot_id, "pruned": len(pruned)}

    @api.patch("/runs/{run_id}")
    def patch_run(run_id: int, body: RunPatch):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        if body.status in ("success", "failure"):
            return _finalize_run(
                run_id, run["job_id"], body.status, body.bytes_transferred, body.message,
                body.snapshot_path, body.snapshot_size, body.encrypted,
            )
        store.update_progress(run_id, body.percent or 0, body.transferred or 0, body.rate, body.eta)
        return {"run_id": run_id}

    @api.get("/runs")
    def list_runs(limit: int = 50):
        return store.list_runs(limit)

    @api.get("/snapshots")
    def list_snapshots():
        return store.list_snapshots()

    @api.delete("/snapshots/{snap_id}")
    def delete_snapshot(snap_id: int):
        if not retention.delete_snapshot(store, snap_id, repo_root):
            raise HTTPException(404, f"unknown snapshot: {snap_id}")
        return {"deleted": snap_id}

    @api.post("/maintenance/prune")
    def prune():
        pruned = retention.prune_all(store, repo_root)
        return {"pruned": len(pruned), "snapshots": [s["id"] for s in pruned]}

    return api


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    cfg = load_config()
    print(f"pibackup {__version__} — repo: {cfg.repo_dir} — db: {cfg.db_path}")
    print(f"Serving API on http://{host}:{port}")
    uvicorn.run(create_app(cfg), host=host, port=port)
