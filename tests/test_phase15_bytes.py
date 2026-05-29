"""Issue #39 (normalise byte sizes): the Recent-runs 'Bytes' column renders
human-readable units (B/KB/MB/GB/TB) with comma thousands-separators rather
than a raw integer byte count.

Covers ``_human_bytes`` directly (unit rounding + comma grouping) and an
end-to-end render of the dashboard proving the humanised value reaches the
HTML in place of the raw byte count.
"""

import pytest
from fastapi.testclient import TestClient

from pibackup.common.auth import hash_password
from pibackup.common.config import Config, JobSpec
from pibackup.common.store import Store
from pibackup.server.app import create_app
from pibackup.server.dashboard import _human_bytes, render_dashboard


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


# ---- _human_bytes unit behaviour ----
@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1,023 B"),  # comma grouping in the bytes branch (<1024)
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 ** 2, "1.0 MB"),
        (int(1.5 * 1024 ** 3), "1.5 GB"),
        (1024 ** 4, "1.0 TB"),
        (5 * 1024 ** 5, "5,120.0 TB"),  # comma grouping at huge TB scale
    ],
)
def test_human_bytes_units_and_commas(value, expected):
    assert _human_bytes(value) == expected


# ---- end-to-end: dashboard renders humanised per-run bytes ----
def test_dashboard_recent_runs_humanises_bytes(tmp_path):
    cfg = _config(tmp_path)
    store = _store_with_admin(cfg)
    store.ensure_client("alpha", "alpha.local")
    job_id = store.ensure_job(
        store.get_client_by_name("alpha")["id"],
        JobSpec(name="docs", sources=["/home"]),
    )
    # 1.5 GB transferred -> "1.5 GB" in the table, never the raw integer.
    raw = int(1.5 * 1024 ** 3)
    store.record_run(job_id, "success", raw, "done")

    html = render_dashboard(store)

    assert "1.5 GB" in html
    assert str(raw) not in html


def test_dashboard_bytes_branch_uses_commas(tmp_path):
    cfg = _config(tmp_path)
    store = _store_with_admin(cfg)
    store.ensure_client("alpha", "alpha.local")
    job_id = store.ensure_job(
        store.get_client_by_name("alpha")["id"],
        JobSpec(name="docs", sources=["/home"]),
    )
    store.record_run(job_id, "success", 1023, "done")

    html = render_dashboard(store)

    assert "1,023 B" in html
    # And it is reachable through the authed dashboard route too.
    client = TestClient(create_app(cfg))
    client.post("/login", data={"username": "admin", "password": "secret"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert "1,023 B" in resp.text
