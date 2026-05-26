"""Phase 2: server REST API, retention engine, and the client->server loop."""

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from pibackup.client.api import ServerApi
from pibackup.client.engine import BackupEngine
from pibackup.client.reporter import ApiReporter
from pibackup.common.config import Config, JobSpec
from pibackup.common.store import Store
from pibackup.server import retention
from pibackup.server.app import create_app


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(_config(tmp_path)))


# ---- API ----
def test_register_and_list_clients(client):
    assert client.post("/clients", json={"name": "pi1", "hostname": "pi1.local"}).status_code == 200
    names = [c["name"] for c in client.get("/clients").json()]
    assert "pi1" in names


def test_job_crud(client):
    client.post("/clients", json={"name": "pi1"})
    created = client.post(
        "/clients/pi1/jobs",
        json={"name": "home", "sources": ["/home/pi"], "retention_days": 7},
    ).json()
    assert created["sources"] == ["/home/pi"]
    job_id = created["id"]

    assert [j["name"] for j in client.get("/clients/pi1/jobs").json()] == ["home"]
    assert client.get(f"/jobs/{job_id}").json()["retention_days"] == 7

    assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_jobs_for_unknown_client_is_empty(client):
    assert client.get("/clients/ghost/jobs").json() == []


def test_report_run_records_run_and_snapshot(client, tmp_path):
    client.post("/clients", json={"name": "pi1"})
    job = client.post("/clients/pi1/jobs", json={"name": "home", "sources": ["/x"]}).json()
    snap_dir = tmp_path / "repo" / "snap1"
    snap_dir.mkdir(parents=True)

    resp = client.post(
        f"/jobs/{job['id']}/runs",
        json={"status": "success", "bytes_transferred": 4096, "message": "ok",
              "snapshot_path": str(snap_dir), "snapshot_size": 4096},
    ).json()
    assert resp["run_id"] and resp["snapshot_id"]

    assert client.get("/runs").json()[0]["status"] == "success"
    assert client.get("/snapshots").json()[0]["job_name"] == "home"


# ---- retention ----
def test_retention_prunes_old_snapshots(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    cid = store.ensure_client("pi1", "pi1")
    job_id = store.ensure_job(cid, JobSpec(name="home", sources=["/x"], retention_days=7))
    run_id = store.record_run(job_id, "success", 0, "ok")

    old_dir = cfg.repo_dir / "old"
    new_dir = cfg.repo_dir / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    store.add_snapshot(job_id, run_id, str(old_dir), 0, False, created_at="2000-01-01 00:00:00")
    store.add_snapshot(job_id, run_id, str(new_dir), 0, False)

    pruned = retention.prune_all(store, str(cfg.repo_dir))
    assert len(pruned) == 1
    assert not old_dir.exists()  # directory deleted
    assert new_dir.exists()  # within retention, kept
    assert len(store.list_snapshots()) == 1


def test_retention_refuses_outside_repo_root(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    cid = store.ensure_client("pi1", "pi1")
    job_id = store.ensure_job(cid, JobSpec(name="home", sources=["/x"], retention_days=1))
    run_id = store.record_run(job_id, "success", 0, "ok")

    outside = tmp_path / "outside"
    outside.mkdir()
    store.add_snapshot(job_id, run_id, str(outside), 0, False, created_at="2000-01-01 00:00:00")

    pruned = retention.prune_all(store, str(cfg.repo_dir))
    assert pruned == []
    assert outside.exists()  # never touched


# ---- end-to-end client -> server loop over a real socket ----
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    import uvicorn

    port = _free_port()
    cfg = _config(tmp_path)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    api = ServerApi(f"http://127.0.0.1:{port}")
    for _ in range(100):
        if api.reachable():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    yield cfg, api
    server.should_exit = True
    thread.join(timeout=5)


def test_client_server_backup_loop(tmp_path, live_server):
    cfg, api = live_server
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("payload")

    # Admin creates a job on the server.
    api.register_client(cfg.client_name, "host")
    api.create_job(cfg.client_name, {"name": "home", "sources": [str(src)], "retention_days": 30})

    # Client fetches its config, runs the backup, and reports back.
    reporter = ApiReporter(cfg, api)
    specs = reporter.jobs()
    assert [s.name for s in specs] == ["home"]

    engine = BackupEngine(cfg)
    result = engine.run_job(specs[0])
    assert result.ok, result.message
    reporter.record(specs[0], result)

    # The server now shows the run and the snapshot.
    assert api.list_runs()[0]["status"] == "success"
    assert api.list_snapshots()[0]["job_name"] == "home"
    assert (cfg.repo_dir / cfg.client_name / "home" / "latest").is_symlink()
