"""Phase 6: system manifest capture + file-level restore."""

import json

import pytest
from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.client.engine import BackupEngine
from pibackup.client.restore import restore_snapshot
from pibackup.common import manifest as manifest_mod
from pibackup.common.config import Config, JobSpec

runner = CliRunner()


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


# ---- manifest ----
def test_manifest_capture_has_core_keys():
    import socket

    m = manifest_mod.capture()
    assert m["hostname"] == socket.gethostname()
    assert "captured_at" in m
    # fstab/os_release are best-effort (str or None); key must exist.
    assert "fstab" in m and "os_release" in m


def test_manifest_to_json_roundtrips():
    text = manifest_mod.to_json({"hostname": "x", "captured_at": "now"})
    assert json.loads(text)["hostname"] == "x"


def test_manifest_write(tmp_path):
    path = manifest_mod.write(tmp_path / "manifest.json")
    assert json.loads(path.read_text())["hostname"]


def test_cli_manifest():
    result = runner.invoke(app, ["manifest"])
    assert result.exit_code == 0
    import socket

    assert socket.gethostname() in result.output


# ---- restore (plaintext) ----
def test_restore_plaintext_roundtrip(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.txt").write_text("bravo")

    cfg = _config(tmp_path)
    res = BackupEngine(cfg).run_job(JobSpec(name="home", sources=[str(src)]))
    assert res.ok

    snap = {"id": 1, "path": res.snapshot_path, "encrypted": 0}
    target = tmp_path / "restored"
    result = restore_snapshot(cfg, snap, str(target))
    assert result.ok, result.message

    rel = str(src).lstrip("/")
    assert (target / rel / "a.txt").read_text() == "alpha"
    assert (target / rel / "sub" / "b.txt").read_text() == "bravo"


# ---- restore (encrypted) ----
def test_restore_encrypted_roundtrip(tmp_path):
    pytest.importorskip("pyrage")
    pytest.importorskip("zstandard")
    from pibackup.client import keys  # uses XDG isolated by conftest

    recipient, _ = keys.create_key("restore-key")

    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.txt").write_text("classified")

    cfg = _config(tmp_path)
    res = BackupEngine(cfg).run_job(
        JobSpec(name="vault", sources=[str(src)], encrypted=True), recipient=recipient
    )
    assert res.ok, res.message

    snap = {"id": 1, "path": res.snapshot_path, "encrypted": 1}
    target = tmp_path / "restored"
    result = restore_snapshot(cfg, snap, str(target))
    assert result.ok, result.message
    assert (target / str(src).lstrip("/") / "secret.txt").read_text() == "classified"


# ---- CLI restore (standalone) ----
def test_cli_restore_standalone(tmp_path):
    from pibackup.common.config import config_file

    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("data")
    repo = tmp_path / "repo"

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'repo_target = "{repo}"\n'
        'client_name = "testpi"\n'
        'server_url = "http://127.0.0.1:9"\n'
        "\n"
        "[[job]]\n"
        'name = "home"\n'
        f'sources = ["{src}"]\n'
    )

    assert runner.invoke(app, ["run"]).exit_code == 0
    target = tmp_path / "out"
    result = runner.invoke(app, ["restore", "1", "--target", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / str(src).lstrip("/") / "file.txt").read_text() == "data"
