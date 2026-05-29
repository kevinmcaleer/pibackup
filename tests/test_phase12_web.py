"""Issue #32 (web surface): create & delete backup jobs for any enrolled
client from the dashboard.

Covers the session-authed form handlers ``POST /jobs`` and
``POST /jobs/{id}/delete``: auth gating, the client dropdown source, happy-path
creation for an arbitrary enrolled client, validation errors, and deletion.
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
    # Two enrolled clients so we can prove cross-client targeting.
    store.ensure_client("alpha", "alpha.local")
    store.ensure_client("beta", "beta.local")
    client = TestClient(create_app(cfg))
    return cfg, store, client


def _login(client):
    client.post("/login", data={"username": "admin", "password": "secret"})


# ---- auth gating ----
def test_create_requires_login(setup):
    _, store, client = setup
    resp = client.post(
        "/jobs",
        data={"client": "beta", "name": "docs", "sources": "/home"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert store.jobs_for_client("beta") == []


def test_delete_requires_login(setup):
    cfg, store, client = setup
    job_id = store.ensure_job(
        store.get_client_by_name("beta")["id"],
        JobSpec(name="docs", sources=["/home"]),
    )
    resp = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert store.get_job(job_id) is not None  # untouched


# ---- dropdown source ----
def test_dashboard_lists_clients_in_new_job_form(setup):
    _, _, client = setup
    _login(client)
    page = client.get("/")
    assert page.status_code == 200
    assert "+ New job" in page.text
    assert '<option value="alpha">alpha</option>' in page.text
    assert '<option value="beta">beta</option>' in page.text


# ---- happy path: create for an arbitrary enrolled client ----
def test_create_job_for_remote_client(setup):
    _, store, client = setup
    _login(client)
    resp = client.post(
        "/jobs",
        data={
            "client": "beta",
            "name": "documents",
            "sources": "/home, /etc",
            "retention_days": "14",
            "bwlimit_kbps": "500",
            "encrypted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    jobs = store.jobs_for_client("beta")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == "documents"
    assert job["retention_days"] == 14
    assert job["bwlimit_kbps"] == 500
    assert bool(job["encrypted"]) is True
    # Sources parsed from the comma-separated field.
    import json
    assert json.loads(job["source_paths"]) == ["/home", "/etc"]
    # Nothing leaked onto the other client.
    assert store.jobs_for_client("alpha") == []


def test_create_job_unchecked_encrypted_defaults_false(setup):
    _, store, client = setup
    _login(client)
    client.post(
        "/jobs",
        data={"client": "alpha", "name": "logs", "sources": "/var/log"},
        follow_redirects=False,
    )
    job = store.jobs_for_client("alpha")[0]
    assert bool(job["encrypted"]) is False
    assert job["retention_days"] == 30  # form default


# ---- validation ----
def test_create_job_unknown_client_errors(setup):
    _, store, client = setup
    _login(client)
    resp = client.post(
        "/jobs",
        data={"client": "ghost", "name": "x", "sources": "/x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Unknown client" in resp.text
    assert store.list_jobs() == []


def test_create_job_blank_sources_errors(setup):
    _, store, client = setup
    _login(client)
    resp = client.post(
        "/jobs",
        data={"client": "alpha", "name": "x", "sources": "  ,  "},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "source path" in resp.text
    assert store.list_jobs() == []


# ---- delete ----
def test_delete_job_from_dashboard(setup):
    _, store, client = setup
    _login(client)
    job_id = store.ensure_job(
        store.get_client_by_name("beta")["id"],
        JobSpec(name="docs", sources=["/home"]),
    )
    resp = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert store.get_job(job_id) is None


def test_delete_missing_job_is_idempotent(setup):
    _, store, client = setup
    _login(client)
    resp = client.post("/jobs/9999/delete", follow_redirects=False)
    assert resp.status_code == 303  # no error, just redirects home
