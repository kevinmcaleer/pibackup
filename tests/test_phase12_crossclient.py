"""Issue #32: create & manage backup jobs for any enrolled client.

These cover the CLI ``--client/-c`` flag across job create/ls/rm/start/stop and
the shared ``_resolve_client`` validation (unknown client => friendly error,
no remote auto-registration).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pibackup.client import cli
from pibackup.common.config import Config

runner = CliRunner()

LOCAL = "thispi"
REMOTE = "dev01"


class FakeApi:
    """In-memory stand-in for ServerApi covering the calls the CLI makes."""

    def __init__(self, clients, jobs):
        # clients: list[str]; jobs: {client_name: [job dicts]}
        self._clients = list(clients)
        self._jobs = {k: list(v) for k, v in jobs.items()}
        self.registered = []
        self.created = []
        self.deleted = []
        self.started = []
        self.stopped = []
        self._next_id = 100

    def reachable(self):
        return True

    def list_clients(self):
        return [{"name": n} for n in self._clients]

    def register_client(self, name, hostname=None):
        self.registered.append((name, hostname))
        if name not in self._clients:
            self._clients.append(name)
        return {"name": name}

    def get_jobs(self, client_name):
        return list(self._jobs.get(client_name, []))

    def create_job(self, client_name, spec):
        self._next_id += 1
        job = {"id": self._next_id, **spec}
        self._jobs.setdefault(client_name, []).append(job)
        self.created.append((client_name, spec))
        return job

    def delete_job(self, job_id):
        self.deleted.append(job_id)
        return {"ok": True}

    def start_job(self, job_id):
        self.started.append(job_id)
        return {"id": 1, "job_id": job_id}

    def stop_job(self, job_id):
        self.stopped.append(job_id)
        return {"id": 2, "job_id": job_id}


@pytest.fixture
def fake(monkeypatch):
    api = FakeApi(
        clients=[LOCAL, REMOTE],
        jobs={
            LOCAL: [
                {"id": 1, "name": "home", "sources": ["/home"], "retention_days": 30,
                 "bwlimit_kbps": 0, "encrypted": False},
            ],
            REMOTE: [
                {"id": 2, "name": "etc", "sources": ["/etc"], "retention_days": 7,
                 "bwlimit_kbps": 0, "encrypted": False},
            ],
        },
    )
    monkeypatch.setattr(cli, "_server", lambda: api)
    # _resolve_client and job_create both read load_config().client_name.
    cfg = Config(data_dir=Path("/tmp"), repo_dir=Path("/tmp/repo"),
                 db_path=Path("/tmp/db"), client_name=LOCAL)
    monkeypatch.setattr("pibackup.common.config.load_config", lambda: cfg)
    return api


# ----- ls -----
def test_job_ls_defaults_to_local(fake):
    result = runner.invoke(cli.app, ["job", "ls", "-q"])
    assert result.exit_code == 0, result.output
    assert "home" in result.output
    assert "etc" not in result.output


def test_job_ls_targets_remote_client(fake):
    result = runner.invoke(cli.app, ["job", "ls", "-c", REMOTE, "-q"])
    assert result.exit_code == 0, result.output
    assert "etc" in result.output
    assert "home" not in result.output


def test_job_ls_unknown_client_errors(fake):
    result = runner.invoke(cli.app, ["job", "ls", "-c", "ghost"])
    assert result.exit_code == 1
    assert "No such enrolled client" in result.output


# ----- create -----
def test_job_create_remote_does_not_register(fake):
    result = runner.invoke(
        cli.app,
        ["job", "create", "logs", "-s", "/var/log", "-c", REMOTE],
    )
    assert result.exit_code == 0, result.output
    assert fake.created and fake.created[-1][0] == REMOTE
    # Targeting a remote enrolled client must NOT auto-register anything.
    assert fake.registered == []
    assert f"for {REMOTE}" in result.output


def test_job_create_local_registers_self(fake):
    result = runner.invoke(cli.app, ["job", "create", "docs", "-s", "/srv"])
    assert result.exit_code == 0, result.output
    assert fake.created[-1][0] == LOCAL
    assert fake.registered and fake.registered[0][0] == LOCAL


def test_job_create_unknown_client_errors(fake):
    result = runner.invoke(
        cli.app, ["job", "create", "x", "-s", "/x", "-c", "ghost"]
    )
    assert result.exit_code == 1
    assert "No such enrolled client" in result.output
    assert fake.created == []


# ----- rm / start / stop -----
def test_job_rm_remote(fake):
    result = runner.invoke(cli.app, ["job", "rm", "etc", "-c", REMOTE])
    assert result.exit_code == 0, result.output
    assert fake.deleted == [2]


def test_job_start_remote(fake):
    result = runner.invoke(cli.app, ["job", "start", "etc", "-c", REMOTE])
    assert result.exit_code == 0, result.output
    assert fake.started == [2]


def test_job_stop_remote(fake):
    result = runner.invoke(cli.app, ["job", "stop", "etc", "-c", REMOTE])
    assert result.exit_code == 0, result.output
    assert fake.stopped == [2]


def test_job_start_unknown_job_on_valid_client(fake):
    result = runner.invoke(cli.app, ["job", "start", "nope", "-c", REMOTE])
    assert result.exit_code == 1
    assert "No such job" in result.output
    assert fake.started == []
