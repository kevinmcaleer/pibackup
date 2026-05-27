"""Phase 9: live backup progress + ETA.

The client streams rsync's ``--info=progress2`` ticks into a 'running' run on
the server, which the dashboard renders as a live progress bar with an ETA and
flags as stalled if updates stop.
"""

import os
import shutil

import pytest
from fastapi.testclient import TestClient

from pibackup.common.config import Config, JobSpec
from pibackup.common.db import connect, init_db
from pibackup.common.store import Store
from pibackup.common.transfer import (
    Progress,
    build_rsync_command,
    parse_progress,
    run_rsync,
)
from pibackup.server.app import create_app
from pibackup.server.dashboard import render_dashboard


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "pibackup.db")
    init_db(store.db_path)
    return store


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(_config(tmp_path)))


# ---- progress parsing (real rsync 3.x progress2 lines) ----
def test_parse_progress_real_lines():
    p = parse_progress("        917,504  22%  801.48kB/s    0:00:03")
    assert (p.percent, p.transferred, p.rate, p.eta) == (22, 917504, "801.48kB/s", "0:00:03")

    # final line carries an xfr summary suffix
    q = parse_progress("      4,000,000 100%  807.30kB/s    0:00:04 (xfr#1, to-chk=0/1)")
    assert q.percent == 100 and q.transferred == 4000000


def test_parse_progress_ignores_non_progress():
    assert parse_progress("Number of regular files transferred: 12") is None
    assert parse_progress("") is None


# ---- streaming a real local rsync ----
@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_rsync_streams_progress(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "blob.bin").write_bytes(os.urandom(2_000_000))
    dst = tmp_path / "dst"
    dst.mkdir()

    # bwlimit makes the transfer last long enough to emit several ticks
    cmd = build_rsync_command(str(src) + "/", str(dst) + "/", bwlimit_kbps=1024, progress=True)
    ticks: list[Progress] = []
    res = run_rsync(cmd, ticks.append, interval=0.1)

    assert res.ok, res.message
    assert ticks, "expected at least one progress tick"
    assert ticks[-1].percent == 100


# ---- schema migration ----
def test_migration_adds_progress_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = connect(db)
    conn.executescript(
        """CREATE TABLE runs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               job_id INTEGER, started_at TEXT, finished_at TEXT,
               status TEXT, bytes_transferred INTEGER, message TEXT
           );"""
    )
    conn.commit()
    conn.close()

    init_db(db)  # should ALTER in the new columns, not fail

    conn = connect(db)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    conn.close()
    assert {"percent", "transferred", "rate", "eta", "updated_at"} <= cols


# ---- store progress lifecycle ----
def test_store_progress_lifecycle(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))

    rid = store.start_run(jid)
    store.update_progress(rid, 42, 1000, "1.0MB/s", "0:00:30")
    running = store.running_runs()
    assert len(running) == 1
    assert int(running[0]["percent"]) == 42
    assert running[0]["client_name"] == "pi" and running[0]["job_name"] == "home"

    store.finish_run(rid, "success", 2000, "done")
    assert store.running_runs() == []
    run = store.get_run(rid)
    assert run["status"] == "success" and int(run["percent"]) == 100

    # progress updates don't resurrect a finished run
    store.update_progress(rid, 10, 5, "x", "y")
    assert int(store.get_run(rid)["percent"]) == 100


# ---- dashboard ----
def test_dashboard_renders_running_bar(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))
    rid = store.start_run(jid)
    store.update_progress(rid, 37, 1000, "2.0MB/s", "0:01:00")

    html = render_dashboard(store)
    assert "Running now" in html
    assert "pi / home" in html
    assert "width: 37%" in html
    assert "ETA 0:01:00" in html


def test_dashboard_marks_stalled(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))
    rid = store.start_run(jid)
    # backdate well past the stall threshold
    conn = connect(store.db_path)
    conn.execute(
        "UPDATE runs SET started_at='2000-01-01 00:00:00', updated_at='2000-01-01 00:00:00' WHERE id=?",
        (rid,),
    )
    conn.commit()
    conn.close()

    assert "stalled" in render_dashboard(store)


# ---- API run lifecycle ----
def test_run_progress_lifecycle_over_api(client, tmp_path):
    client.post("/clients", json={"name": "pi1"})
    job = client.post("/clients/pi1/jobs", json={"name": "home", "sources": ["/x"]}).json()

    started = client.post(f"/jobs/{job['id']}/runs", json={"status": "running"}).json()
    rid = started["run_id"]
    assert rid

    assert client.patch(
        f"/runs/{rid}",
        json={"percent": 50, "transferred": 2048, "rate": "1.0MB/s", "eta": "0:00:10"},
    ).status_code == 200
    me = next(r for r in client.get("/runs").json() if r["id"] == rid)
    assert me["status"] == "running" and int(me["percent"]) == 50

    snap = tmp_path / "repo" / "snap1"
    snap.mkdir(parents=True)
    fin = client.patch(
        f"/runs/{rid}",
        json={"status": "success", "bytes_transferred": 2048, "snapshot_path": str(snap), "snapshot_size": 2048},
    ).json()
    assert fin["snapshot_id"]

    assert next(r for r in client.get("/runs").json() if r["id"] == rid)["status"] == "success"
    assert any(sn["path"] == str(snap) for sn in client.get("/snapshots").json())

    # patching an unknown run is a clean 404
    assert client.patch("/runs/999999", json={"percent": 1}).status_code == 404
