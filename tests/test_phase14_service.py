"""Phase 14 (issue #36): run the server as a robust systemd *system* service.

Covers:
* provision.build_service_plan — user/group/state-dir setup, the system unit
  body, enable+start, idempotent user creation, and DB migration.
* `pibackup admin install-service` — dry-run plan output and root gating.

Real provisioning creates users and touches /etc + systemd, so the command is
only exercised via --dry-run (no subprocess) or asserted through the plan
builder, never executed.
"""

import os

from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.server import provision

runner = CliRunner()


# ----- service plan builder -----
def test_service_plan_creates_user_group_and_state_dir():
    plan = provision.build_service_plan()
    cmds = plan.commands
    assert ["groupadd", "-f", "pibackup"] in cmds
    assert [
        "useradd", "--system", "--no-create-home",
        "--shell", "/usr/sbin/nologin", "--gid", "pibackup", "pibackup",
    ] in cmds
    assert ["mkdir", "-p", "/var/lib/pibackup"] in cmds
    assert ["chown", "pibackup:pibackup", "/var/lib/pibackup"] in cmds
    assert ["chmod", "2775", "/var/lib/pibackup"] in cmds


def test_service_plan_enables_and_starts_unit():
    cmds = provision.build_service_plan().commands
    assert ["systemctl", "daemon-reload"] in cmds
    assert ["systemctl", "enable", "--now", "pibackup-server"] in cmds


def test_service_plan_writes_system_unit():
    plan = provision.build_service_plan()
    paths = {str(p): body for p, body in plan.all_files()}
    unit = paths["/etc/systemd/system/pibackup-server.service"]
    # The robustness payload: system user, no linger/session-bus dependency.
    assert "User=pibackup" in unit
    assert "Group=pibackup" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "Environment=PIBACKUP_DATA_DIR=/var/lib/pibackup" in unit
    assert "ExecStart=/usr/local/bin/pibackup serve --host 0.0.0.0" in unit


def test_service_plan_also_writes_system_config():
    """The unit and the shared-state system config are both written, so a fresh
    host lands on /var/lib/pibackup for both the daemon and group operators."""
    plan = provision.build_service_plan()
    paths = {str(p): body for p, body in plan.all_files()}
    assert 'data_dir = "/var/lib/pibackup"' in paths["/etc/pibackup/config.toml"]


def test_service_plan_skips_useradd_when_user_exists():
    plan = provision.build_service_plan(create_user=False)
    assert not any(c[0] == "useradd" for c in plan.commands)
    # group + state dir still set up.
    assert ["groupadd", "-f", "pibackup"] in plan.commands


def test_service_plan_custom_binary_in_unit():
    plan = provision.build_service_plan(binary="/opt/pibackup/bin/pibackup")
    unit = dict((str(p), b) for p, b in plan.all_files())[
        "/etc/systemd/system/pibackup-server.service"
    ]
    assert "ExecStart=/opt/pibackup/bin/pibackup serve --host 0.0.0.0" in unit


def test_service_plan_migration_copies_old_state():
    from pathlib import Path

    plan = provision.build_service_plan(
        migrate_from=Path("/home/pibackup/.local/share/pibackup")
    )
    assert [
        "cp", "-an", "/home/pibackup/.local/share/pibackup/.", "/var/lib/pibackup"
    ] in plan.commands
    assert ["chown", "-R", "pibackup:pibackup", "/var/lib/pibackup"] in plan.commands
    assert any("Migrated existing state" in n for n in plan.notes)


def test_service_plan_no_migration_has_no_cp():
    plan = provision.build_service_plan()
    assert not any(c[0] == "cp" for c in plan.commands)


def test_plan_as_shell_renders_unit_write():
    shell = provision.build_service_plan().as_shell()
    assert "systemctl enable --now pibackup-server" in shell
    assert "/etc/systemd/system/pibackup-server.service" in shell
    assert "User=pibackup" in shell


# ----- the install-service command -----
def test_install_service_dry_run_runs_nothing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("subprocess must not run on --dry-run")

    monkeypatch.setattr(subprocess, "run", boom)
    result = runner.invoke(app, ["admin", "install-service", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would run" in result.output
    assert "systemctl enable --now pibackup-server" in result.output
    assert "User=pibackup" in result.output


def test_install_service_requires_root_when_not_dry_run(monkeypatch):
    # Force a non-root euid so the command refuses before mutating anything.
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    result = runner.invoke(app, ["admin", "install-service"])
    assert result.exit_code == 1
    assert "root" in result.output.lower()
