"""Phase 13 (issue #33): Docker-like install — shared-state group access.

Three areas:
* config layering — /etc/pibackup/config.toml sits *under* per-user config.
* provision.build_plan — the steps that set up the group + shared state dir.
* `pibackup admin enable-group` — dry-run plan output and root gating.

Real provisioning mutates groups and /etc, so the command's side effects are
either dry-run (no subprocess) or asserted via the plan builder, never executed.
"""

import os

from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.common import config as config_mod
from pibackup.server import provision

runner = CliRunner()


# ----- config layering -----
def test_system_config_layers_under_user_config(tmp_path, monkeypatch):
    """System config supplies data_dir; per-user config overrides per key."""
    sys_cfg = tmp_path / "etc" / "config.toml"
    sys_cfg.parent.mkdir(parents=True)
    sys_cfg.write_text('data_dir = "/var/lib/pibackup"\nrepo_target = "sys@host:/repo"\n')
    monkeypatch.setenv("PIBACKUP_SYSTEM_CONFIG", str(sys_cfg))

    user_cfg = config_mod.config_dir()
    user_cfg.mkdir(parents=True, exist_ok=True)
    # User overrides repo_target but inherits data_dir from the system file.
    (user_cfg / "config.toml").write_text('repo_target = "me@host:/myrepo"\n')

    cfg = config_mod.load_config()
    assert str(cfg.data_dir) == "/var/lib/pibackup"   # from system config
    assert cfg.repo_target == "me@host:/myrepo"         # user wins


def test_no_system_config_is_unchanged_behaviour(tmp_path, monkeypatch):
    """Absent system file → original single-file behaviour (data_dir from XDG)."""
    monkeypatch.setenv("PIBACKUP_SYSTEM_CONFIG", str(tmp_path / "etc" / "absent.toml"))
    cfg = config_mod.load_config()
    # Falls back to XDG_DATA_HOME/pibackup (set by the conftest fixture).
    assert "pibackup" in str(cfg.data_dir)
    assert str(cfg.data_dir).startswith(str(tmp_path))


# ----- provision plan builder -----
def test_build_plan_creates_group_and_setgid_state_dir():
    plan = provision.build_plan("alice", "pibackup")
    cmds = plan.commands
    assert ["groupadd", "-f", "pibackup"] in cmds
    assert ["mkdir", "-p", "/var/lib/pibackup"] in cmds
    assert ["chown", "pibackup:pibackup", "/var/lib/pibackup"] in cmds
    # setgid (leading 2) so new files inherit the group.
    assert ["chmod", "2775", "/var/lib/pibackup"] in cmds
    assert ["usermod", "-aG", "pibackup", "alice"] in cmds


def test_build_plan_without_operator_skips_usermod():
    plan = provision.build_plan(None, "pibackup")
    assert not any(c[0] == "usermod" for c in plan.commands)
    assert any("usermod -aG pibackup <user>" in n for n in plan.notes)


def test_build_plan_config_points_at_shared_state_dir():
    plan = provision.build_plan("alice", "pibackup")
    assert str(plan.config_path) == "/etc/pibackup/config.toml"
    assert 'data_dir = "/var/lib/pibackup"' in plan.config_body


def test_build_plan_custom_service_user_owns_dir():
    plan = provision.build_plan("alice", "backupsvc")
    assert ["chown", "backupsvc:pibackup", "/var/lib/pibackup"] in plan.commands


def test_plan_as_shell_is_copy_pasteable():
    shell = provision.build_plan("alice", "pibackup").as_shell()
    assert "groupadd -f pibackup" in shell
    assert "usermod -aG pibackup alice" in shell
    assert "/etc/pibackup/config.toml" in shell


# ----- the enable-group command -----
def test_enable_group_dry_run_runs_nothing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("subprocess must not run on --dry-run")

    monkeypatch.setattr(subprocess, "run", boom)
    result = runner.invoke(app, ["admin", "enable-group", "alice", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would run" in result.output
    assert "groupadd -f pibackup" in result.output
    assert "newgrp pibackup" in result.output


def test_enable_group_defaults_operator_to_sudo_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "bob")
    result = runner.invoke(app, ["admin", "enable-group", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "usermod -aG pibackup bob" in result.output


def test_enable_group_requires_root_when_not_dry_run(monkeypatch):
    # Force a non-root euid so the command refuses before mutating anything.
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    result = runner.invoke(app, ["admin", "enable-group", "alice"])
    assert result.exit_code == 1
    assert "root" in result.output.lower()
