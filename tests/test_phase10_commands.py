"""Phase 10: run/stop backup jobs from the server (issue #21).

The server queues 'start'/'stop' commands per job; push-based clients poll for
them via the agent and act — running a backup or cancelling an in-flight one.
A cancel is a cross-process filesystem flag the rsync layer notices and tears
the transfer down on.
"""

import os
import shutil

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pibackup.client import cancel
from pibackup.client.api import ServerApi
from pibackup.common.config import Config, JobSpec, config_file
from pibackup.common.db import init_db
from pibackup.common.store import Store
from pibackup.common.transfer import (
    CANCELLED_EXIT_CODE,
    build_rsync_command,
    run_rsync,
)
from pibackup.server.app import create_app
from pibackup.server.dashboard import render_dashboard

cli_runner = CliRunner()


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


# ---- store: command queue ----
def test_store_command_lifecycle(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi1")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))

    cmd_id = store.enqueue_command(jid, "start")
    pending = store.pending_commands_for_client("pi1")
    assert [c["action"] for c in pending] == ["start"]
    assert pending[0]["client_name"] == "pi1" and pending[0]["job_name"] == "home"

    store.update_command(cmd_id, "running", "starting", run_id=None)
    # No longer pending once it leaves 'pending'.
    assert store.pending_commands_for_client("pi1") == []
    assert store.get_command(cmd_id)["status"] == "running"

    store.update_command(cmd_id, "done", "ok")
    assert store.get_command(cmd_id)["status"] == "done"


def test_pending_commands_are_oldest_first(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi1")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))
    first = store.enqueue_command(jid, "start")
    second = store.enqueue_command(jid, "stop")
    ids = [c["id"] for c in store.pending_commands_for_client("pi1")]
    assert ids == [first, second]


# ---- API: enqueue start/stop ----
def test_api_start_and_stop_enqueue_commands(client):
    client.post("/clients", json={"name": "pi1"})
    job = client.post("/clients/pi1/jobs", json={"name": "home", "sources": ["/x"]}).json()

    started = client.post(f"/jobs/{job['id']}/start").json()
    assert started["action"] == "start" and started["status"] == "pending"

    stopped = client.post(f"/jobs/{job['id']}/stop").json()
    assert stopped["action"] == "stop"

    pending = client.get("/clients/pi1/commands").json()
    assert {c["action"] for c in pending} == {"start", "stop"}
    assert len(client.get("/commands").json()) == 2


def test_api_start_unknown_job_is_404(client):
    assert client.post("/jobs/999/start").status_code == 404
    assert client.post("/jobs/999/stop").status_code == 404


def test_api_command_patch_lifecycle(client):
    client.post("/clients", json={"name": "pi1"})
    job = client.post("/clients/pi1/jobs", json={"name": "home", "sources": ["/x"]}).json()
    cmd = client.post(f"/jobs/{job['id']}/start").json()

    patched = client.patch(f"/commands/{cmd['id']}", json={"status": "done", "message": "ok"}).json()
    assert patched["status"] == "done" and patched["message"] == "ok"
    # Once acted on it's no longer pending.
    assert client.get("/clients/pi1/commands").json() == []
    assert client.patch("/commands/999", json={"status": "done"}).status_code == 404


def test_dashboard_shows_start_and_stop_buttons(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("pi1")
    idle = store.ensure_job(cid, JobSpec(name="home", sources=["/x"]))
    busy = store.ensure_job(cid, JobSpec(name="docs", sources=["/y"]))
    store.start_run(busy)  # an in-flight run => Stop button

    html = render_dashboard(store)
    assert f'action="/jobs/{idle}/start"' in html and ">Start<" in html
    assert f'action="/jobs/{busy}/stop"' in html and ">Stop<" in html


# ---- rsync cancellation ----
@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_rsync_cancels_mid_transfer(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "blob.bin").write_bytes(os.urandom(4_000_000))
    dst = tmp_path / "dst"
    dst.mkdir()

    # Throttle so the transfer lasts long enough to interrupt; cancel on the
    # first progress tick.
    cmd = build_rsync_command(str(src) + "/", str(dst) + "/", bwlimit_kbps=256, progress=True)
    res = run_rsync(cmd, lambda p: None, interval=0.05, should_cancel=lambda: True)

    assert not res.ok
    assert res.exit_code == CANCELLED_EXIT_CODE
    assert "cancelled" in res.message


# ---- cancel flags ----
def test_cancel_flag_roundtrip(tmp_path, monkeypatch):  # XDG isolated by conftest
    assert cancel.is_cancelled("home") is False
    cancel.request_cancel("home")
    assert cancel.is_cancelled("home") is True
    checker = cancel.cancel_checker("home")
    assert checker() is True
    cancel.clear_cancel("home")
    assert cancel.is_cancelled("home") is False
    assert cancel.cancel_checker(None) is None


# ---- agent: act on queued commands ----
def test_agent_stop_sets_cancel_flag(tmp_path):
    cfg = _config(tmp_path)  # client_name = "testpi"
    tc = TestClient(create_app(cfg))
    tc.post("/clients", json={"name": "testpi"})
    job = tc.post("/clients/testpi/jobs", json={"name": "home", "sources": ["/x"]}).json()
    tc.post(f"/jobs/{job['id']}/stop")

    from pibackup.client import agent

    lines = agent.poll_once(api=_ApiOverTestClient(tc), cfg=cfg)

    assert any("stop home" in line for line in lines)
    assert cancel.is_cancelled("home") is True
    # The command is marked done and no longer pending.
    assert tc.get("/clients/testpi/commands").json() == []
    cancel.clear_cancel("home")


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_agent_start_runs_backup(tmp_path):
    """A queued 'start' makes the agent run the job through the runner.

    The agent reports command status to the server it polls, but the *backup*
    itself goes through ``runner.run_jobs``, which resolves jobs from this
    client's own server/config. In tests the runner falls to standalone mode
    (conftest points server_url at an unreachable port), so the job lives in
    config.toml.
    """
    from pibackup.client import agent

    repo = tmp_path / "repo"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("payload")

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'repo_target = "{repo}"\n'
        'client_name = "testpi"\n'
        'server_url = "http://127.0.0.1:9"\n'  # unreachable => standalone runner
        "\n"
        "[[job]]\n"
        'name = "home"\n'
        f'sources = ["{src}"]\n'
    )

    # The agent polls a separate (in-memory) server for the queued command.
    srv_cfg = Config(
        data_dir=tmp_path / "srv", repo_dir=tmp_path / "srv-repo",
        db_path=tmp_path / "srv.db", client_name="testpi",
    )
    tc = TestClient(create_app(srv_cfg))
    tc.post("/clients", json={"name": "testpi"})
    job = tc.post("/clients/testpi/jobs", json={"name": "home", "sources": [str(src)]}).json()
    tc.post(f"/jobs/{job['id']}/start")

    from pibackup.common.config import load_config

    lines = agent.poll_once(api=_ApiOverTestClient(tc), cfg=load_config())

    assert any("start home" in line for line in lines)
    assert (repo / "testpi" / "home" / "latest").is_symlink()


# ---- CLI: job start / stop need a reachable server ----
def test_cli_job_start_no_server():
    # conftest points server_url at an unreachable port => standalone.
    result = cli_runner.invoke(__import__("pibackup.client.cli", fromlist=["app"]).app,
                               ["job", "start", "home"])
    assert result.exit_code == 1
    assert "No server reachable" in result.output


# ---- helpers ----
class _ApiOverTestClient(ServerApi):
    """A ServerApi that routes through a FastAPI TestClient instead of sockets,
    so agent tests need no live server."""

    def __init__(self, test_client: TestClient):
        super().__init__("http://testserver")
        self._tc = test_client

    def _request(self, method: str, path: str, body=None):
        resp = self._tc.request(method, path, json=body)
        if resp.status_code >= 400:
            from pibackup.client.api import ApiError

            raise ApiError(f"{resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None
