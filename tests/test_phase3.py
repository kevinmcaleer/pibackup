"""Phase 3: the web dashboard."""

import pytest
from fastapi.testclient import TestClient

from pibackup.common.config import Config
from pibackup.server.app import create_app
from pibackup.server.dashboard import _human_bytes


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        db_path=tmp_path / "pibackup.db",
        repo_target=str(tmp_path / "repo"),
        client_name="testpi",
    )


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(_config(tmp_path)))


def test_human_bytes():
    assert _human_bytes(0) == "0 B"
    assert _human_bytes(512) == "512 B"
    assert _human_bytes(2048) == "2.0 KB"
    assert _human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "pibackup" in resp.text
    assert "No jobs yet" in resp.text


def test_dashboard_shows_jobs_and_runs(client):
    client.post("/clients", json={"name": "kitchen-pi"})
    job = client.post(
        "/clients/kitchen-pi/jobs",
        json={"name": "home-backup", "sources": ["/home/pi"], "retention_days": 14},
    ).json()
    client.post(
        f"/jobs/{job['id']}/runs",
        json={"status": "success", "bytes_transferred": 4096, "message": "done"},
    )

    text = client.get("/").text
    assert "home-backup" in text
    assert "kitchen-pi" in text
    assert "14d" in text
    assert "badge success" in text  # status badge rendered


def test_dashboard_escapes_html(client):
    # A source path containing markup must be escaped, not rendered.
    client.post("/clients", json={"name": "pi1"})
    client.post(
        "/clients/pi1/jobs",
        json={"name": "evil", "sources": ["/x/<script>alert(1)</script>"]},
    )
    text = client.get("/").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text
