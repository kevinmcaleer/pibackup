"""Phase 11: `pibackup update` — install-method detection + safe self-upgrade.

Real upgrades shell out to pipx/pip, so every subprocess call here is mocked.
We assert the *right* command is built for each detected install layout, extras
are preserved on the venv path, migrations run against the freshly installed
binary, and --dry-run runs nothing.
"""

import subprocess

from typer.testing import CliRunner

from pibackup.client import update as update_mod
from pibackup.client.cli import app
from pibackup.common.db import connect

runner = CliRunner()


# ----- install-method detection -----
def _make_venv(tmp_path, name="pibackup", parent="custom"):
    """Build a fake venv layout (bin/, pyvenv.cfg) and return its python path."""
    root = tmp_path / parent / name
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return root, bindir / "python"


def test_detect_pipx(tmp_path):
    root, python = _make_venv(tmp_path, name="pibackup", parent="venvs")
    info = update_mod.detect_install(executable=str(python))
    assert info.method == "pipx"
    assert info.command == ["pipx", "upgrade", "pibackup"]
    assert info.new_binary == root / "bin" / "pibackup"


def test_detect_venv_fallback_preserves_extras(tmp_path, monkeypatch):
    root, python = _make_venv(tmp_path, name="venv", parent="pibackup")
    monkeypatch.setattr(update_mod, "_detect_extras", lambda *a, **k: ("server", "crypto"))
    info = update_mod.detect_install(executable=str(python), ref="main")
    assert info.method == "venv"
    assert info.command[0] == str(root / "bin" / "pip")
    assert info.command[1:3] == ["install", "--upgrade"]
    spec = info.command[-1]
    assert spec == (
        "pibackup[server,crypto] @ git+"
        "https://github.com/kevinmcaleer/pibackup@main"
    )
    assert info.extras == ("server", "crypto")


def test_detect_venv_respects_ref(tmp_path, monkeypatch):
    _, python = _make_venv(tmp_path, name="venv", parent="pibackup")
    monkeypatch.setattr(update_mod, "_detect_extras", lambda *a, **k: ())
    info = update_mod.detect_install(executable=str(python), ref="dev")
    assert info.command[-1].endswith("@dev")
    assert "[" not in info.command[-1]  # no extras → bare package name


def test_detect_unknown_for_non_venv(tmp_path):
    exe = tmp_path / "usr" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    info = update_mod.detect_install(executable=str(exe))
    assert info.method == "unknown"
    assert info.command == []


def test_detect_pipx_when_python_is_symlink_to_base_interpreter(tmp_path):
    """Regression: a real pipx venv's bin/python is a *symlink* to the base
    interpreter (e.g. /usr/bin/python3.13). Detection must not follow that
    symlink out of the venv — doing so missed pyvenv.cfg and wrongly reported
    'unknown', which broke `pibackup update` on every pipx install."""
    root, python = _make_venv(tmp_path, name="pibackup", parent="venvs")
    # Make bin/python a symlink pointing outside the venv (the real pipx layout).
    base = tmp_path / "usr" / "bin" / "python3.13"
    base.parent.mkdir(parents=True)
    base.write_text("")
    python.symlink_to(base)

    info = update_mod.detect_install(executable=str(python))
    assert info.method == "pipx"
    assert info.command == ["pipx", "upgrade", "pibackup"]
    assert info.new_binary == root / "bin" / "pibackup"


# ----- the update command -----
def _patch_detect(monkeypatch, method, command, new_binary, extras=()):
    info = update_mod.InstallInfo(
        method=method, command=command, new_binary=new_binary, extras=extras
    )
    monkeypatch.setattr("pibackup.client.update.detect_install", lambda **k: info)
    return info


def test_update_dry_run_runs_nothing(monkeypatch, tmp_path):
    _patch_detect(monkeypatch, "pipx", ["pipx", "upgrade", "pibackup"], tmp_path / "pibackup")

    def boom(*a, **k):
        raise AssertionError("subprocess should not run on --dry-run")

    monkeypatch.setattr(subprocess, "run", boom)
    result = runner.invoke(app, ["update", "--dry-run"])
    assert result.exit_code == 0
    assert "would run" in result.output
    assert "pipx upgrade pibackup" in result.output


def test_update_pipx_runs_upgrade_then_migrations(monkeypatch, tmp_path):
    new_bin = tmp_path / "pibackup"
    _patch_detect(monkeypatch, "pipx", ["pipx", "upgrade", "pibackup"], new_bin)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(cmd, 0, stdout="pibackup 9.9.9\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # No systemctl side effects.
    monkeypatch.setattr("pibackup.client.cli._active_services", lambda: [])

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert calls[0] == ["pipx", "upgrade", "pibackup"]
    assert calls[1] == [str(new_bin), "update", "--run-migrations-only"]
    assert "9.9.9" in result.output


def test_update_venv_preserves_extras_in_command(monkeypatch, tmp_path):
    new_bin = tmp_path / "pibackup"
    spec = "pibackup[server,crypto] @ git+https://github.com/kevinmcaleer/pibackup@main"
    cmd = [str(tmp_path / "pip"), "install", "--upgrade", spec]
    _patch_detect(monkeypatch, "venv", cmd, new_bin, extras=("server", "crypto"))
    calls = []

    def fake_run(c, **kwargs):
        calls.append(c)
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(c, 0, stdout="pibackup 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(c, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("pibackup.client.cli._active_services", lambda: [])

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert calls[0] == cmd
    assert "[server,crypto]" in calls[0][-1]
    assert "server, crypto" in result.output  # extras shown to the user


def test_update_unknown_install_errors(monkeypatch):
    _patch_detect(monkeypatch, "unknown", [], __import__("pathlib").Path("python"))
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1
    assert "source checkout" in result.output or "system Python" in result.output


def test_update_upgrade_failure_is_reported(monkeypatch, tmp_path):
    _patch_detect(monkeypatch, "pipx", ["pipx", "upgrade", "pibackup"], tmp_path / "pibackup")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1
    assert "Upgrade failed" in result.output


def test_run_migrations_only_initializes_db():
    """The internal flag runs init_db against the resolved (isolated) db."""
    result = runner.invoke(app, ["update", "--run-migrations-only"])
    assert result.exit_code == 0, result.output
    assert "Migrations applied" in result.output

    from pibackup.common.config import load_config

    db = load_config().db_path
    assert db.exists()
    conn = connect(db)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"clients", "jobs", "runs", "snapshots"} <= tables


def test_service_restart_prints_command_when_not_opted_in(monkeypatch, tmp_path):
    new_bin = tmp_path / "pibackup"
    _patch_detect(monkeypatch, "pipx", ["pipx", "upgrade", "pibackup"], new_bin)

    def fake_run(cmd, **kwargs):
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(cmd, 0, stdout="pibackup 1.0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("pibackup.client.cli._active_services", lambda: ["pibackup-server"])

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "systemctl restart pibackup-server" in result.output


def test_service_restart_opt_in_restarts(monkeypatch, tmp_path):
    new_bin = tmp_path / "pibackup"
    _patch_detect(monkeypatch, "pipx", ["pipx", "upgrade", "pibackup"], new_bin)
    restarted = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "restart"] or (len(cmd) > 1 and cmd[1] == "restart"):
            restarted.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(cmd, 0, stdout="pibackup 1.0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("pibackup.client.cli._active_services", lambda: ["pibackup-agent"])

    result = runner.invoke(app, ["update", "--restart"])
    assert result.exit_code == 0, result.output
    assert any("pibackup-agent" in c for c in restarted)
    assert "Restarted" in result.output
