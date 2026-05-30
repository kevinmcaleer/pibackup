"""Issue #40: the 'Running now' progress component must update independently
of the rest of the page, so an open '+ New job' form is never wiped by a
full-page auto-refresh.

Covers the new ``GET /running`` poll endpoint (auth gating + JSON shape) and
asserts the dashboard no longer carries a page-level meta refresh.
"""

import pytest
from fastapi.testclient import TestClient

from pibackup.common.auth import hash_password
from pibackup.common.config import Config, JobSpec
from pibackup.common.store import Store
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


def _store_with_admin(cfg, username="admin", password="secret") -> Store:
    store = Store(cfg.db_path)
    ph = hash_password(password)
    store.set_admin(username, ph.hash, ph.salt, ph.iterations, "sign-secret")
    return store


@pytest.fixture
def setup(tmp_path):
    cfg = _config(tmp_path)
    store = _store_with_admin(cfg)
    store.ensure_client("alpha", "alpha.local")
    client = TestClient(create_app(cfg))
    return cfg, store, client


def _login(client):
    client.post("/login", data={"username": "admin", "password": "secret"})


def _running_run(store):
    """Create a job with a live 'running' run and return (job_id, run_id)."""
    cid = store.get_client_by_name("alpha")["id"]
    job_id = store.ensure_job(cid, JobSpec(name="documents", sources=["/home"]))
    run_id = store.start_run(job_id)
    store.update_progress(run_id, 42, 1234, "5 MB/s", "00:30")
    return job_id, run_id


# ---- auth gating ----
def test_running_requires_login(setup):
    _, _, client = setup
    resp = client.get("/running", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---- JSON shape when a run is live ----
def test_running_returns_live_progress(setup):
    _, store, client = setup
    _login(client)
    _running_run(store)
    resp = client.get("/running")
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert len(data["running"]) == 1
    row = data["running"][0]
    assert row["client"] == "alpha"
    assert row["job"] == "documents"
    assert row["percent"] == 42
    assert row["rate"] == "5 MB/s"
    assert row["eta"] == "00:30"
    assert row["stalled"] is False


def test_running_empty_when_nothing_runs(setup):
    _, _, client = setup
    _login(client)
    resp = client.get("/running")
    assert resp.status_code == 200
    assert resp.json() == {"running": []}


# ---- the page-level auto-refresh is gone ----
def test_dashboard_has_no_meta_refresh(setup):
    _, _, client = setup
    _login(client)
    page = client.get("/")
    assert page.status_code == 200
    assert 'http-equiv="refresh"' not in page.text
    # The independent poller and its target section are present instead.
    assert 'id="running"' in page.text
    assert "/running" in page.text
