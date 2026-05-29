"""Docker-style shared-state provisioning for server admin access (issue #33).

The goal: an operator added to the ``pibackup`` group runs ``pibackup ...``
admin commands *as themselves* — no ``sudo -u pibackup -H /full/path``. This is
the same trade-off Docker's ``docker`` group makes: group membership ≈ admin
over backups.

Shared-state model (chosen over an admin socket for simplicity): one canonical
state dir at ``/var/lib/pibackup`` that the service user *and* every grouped
operator resolve to. Two pieces make every identity land on the same DB:

* ``/etc/pibackup/config.toml`` — a system config (layered *under* per-user
  config by ``common.config``) that sets ``data_dir`` to the shared dir. So no
  matter whose ``$HOME`` you run as, ``load_config()`` points at the same DB.
* the dir is group-owned by ``pibackup`` with the **setgid** bit, so files
  created there inherit the group and stay readable/writable by all operators.

This module is a pure *plan builder*: :func:`build_plan` returns the shell
commands and file writes needed, without executing anything. The CLI executes
the plan (or prints it for ``--dry-run``); tests assert on the plan. That keeps
the root-only side effects out of the unit tests.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

GROUP = "pibackup"
SERVICE_USER = "pibackup"
STATE_DIR = Path("/var/lib/pibackup")
SYSTEM_CONFIG = Path("/etc/pibackup/config.toml")
UNIT_PATH = Path("/etc/systemd/system/pibackup-server.service")
DEFAULT_BINARY = "/usr/local/bin/pibackup"


@dataclass(frozen=True)
class Plan:
    """A provisioning plan: ordered shell commands + files to write.

    ``commands`` are argv lists (run in order). ``files`` is an ordered list of
    ``(path, body)`` pairs to write after the commands run. ``config_path`` /
    ``config_body`` are a convenience for the single-file ``enable-group`` case
    and, when ``config_body`` is set, are appended to ``files``. ``notes`` are
    human-facing lines summarising what happens and any one-time caveats.
    """

    commands: list[list[str]] = field(default_factory=list)
    config_path: Path = SYSTEM_CONFIG
    config_body: str = ""
    operator: str | None = None
    notes: list[str] = field(default_factory=list)
    files: list[tuple[Path, str]] = field(default_factory=list)

    def all_files(self) -> list[tuple[Path, str]]:
        """Every file the plan writes: explicit ``files`` plus the legacy
        ``config_path``/``config_body`` convenience pair (if set)."""
        out = list(self.files)
        if self.config_body:
            out.append((self.config_path, self.config_body))
        return out

    def as_shell(self) -> str:
        """Render the plan as copy-pasteable shell (for --dry-run)."""
        lines = [" ".join(shlex.quote(part) for part in cmd) for cmd in self.commands]
        for path, body in self.all_files():
            lines.append(
                f"write {shlex.quote(str(path))}:\n"
                + "\n".join(f"    {ln}" for ln in body.splitlines())
            )
        return "\n".join(lines)


def _config_body(state_dir: Path) -> str:
    """The system config TOML pointing every identity at the shared state dir."""
    return (
        "# Managed by `pibackup admin enable-group` (issue #33).\n"
        "# Layered *under* per-user ~/.config/pibackup/config.toml, so this only\n"
        "# supplies defaults — anything you set per-user still wins.\n"
        f'data_dir = "{state_dir}"\n'
    )


def build_plan(
    operator: str | None,
    service_user: str,
    *,
    state_dir: Path = STATE_DIR,
    system_config: Path = SYSTEM_CONFIG,
    group: str = GROUP,
) -> Plan:
    """Build the steps that grant ``operator`` group-based admin access.

    ``service_user`` owns the state dir (the daemon's identity); the dir is
    group-owned by ``group`` with setgid so grouped operators share it.
    ``operator`` (the human to enrol, e.g. ``$SUDO_USER``) is added to the
    group; pass ``None`` to set up the group/state dir without adding anyone.
    """
    commands: list[list[str]] = [
        # -f: succeed if the group already exists (idempotent re-runs).
        ["groupadd", "-f", group],
        ["mkdir", "-p", str(state_dir)],
        # Service user owns it; pibackup group shares it.
        ["chown", f"{service_user}:{group}", str(state_dir)],
        # 2775 = rwx for owner+group, setgid (2) so new files inherit the group.
        ["chmod", "2775", str(state_dir)],
        ["mkdir", "-p", str(system_config.parent)],
    ]

    notes = [
        f"Group '{group}' owns {state_dir} (setgid) — shared backup state.",
        f"System config {system_config} points all operators at {state_dir}.",
    ]

    if operator:
        commands.append(["usermod", "-aG", group, operator])
        notes.append(
            f"Added '{operator}' to '{group}'. They must re-login (or run "
            f"`newgrp {group}`) once for the new group to take effect — same "
            "one-time step as Docker."
        )
        notes.append(
            f"After that, '{operator}' runs admin commands directly: "
            "`pibackup client ls`, `pibackup enroll <pi>`, etc. — no `sudo -u`."
        )
    else:
        notes.append(
            f"No operator added. Grant access later with: usermod -aG {group} <user>"
        )

    return Plan(
        commands=commands,
        config_path=system_config,
        config_body=_config_body(state_dir),
        operator=operator,
        notes=notes,
    )


def _unit_body(binary: str, service_user: str, group: str, state_dir: Path) -> str:
    """The systemd *system* unit for the server.

    Runs as a dedicated service user with ``StateDirectory`` owning the shared
    state dir — no login, no linger, no session bus. ``PIBACKUP_DATA_DIR`` pins
    state to ``state_dir`` regardless of the service user's ``$HOME``.
    """
    return (
        "# Managed by `pibackup admin install-service` (issue #36).\n"
        "[Unit]\n"
        "Description=pibackup server (API + dashboard)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={service_user}\n"
        f"Group={group}\n"
        f"StateDirectory={state_dir.name}\n"
        f"Environment=PIBACKUP_DATA_DIR={state_dir}\n"
        f"ExecStart={binary} serve --host 0.0.0.0\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def build_service_plan(
    *,
    binary: str = DEFAULT_BINARY,
    service_user: str = SERVICE_USER,
    group: str = GROUP,
    state_dir: Path = STATE_DIR,
    unit_path: Path = UNIT_PATH,
    migrate_from: Path | None = None,
    create_user: bool = True,
) -> Plan:
    """Build the steps to run the server as a robust *system* service (issue #36).

    Creates the service user/group, provisions the shared state dir, writes the
    systemd system unit, then enables + starts it. No linger, no session bus —
    plain ``sudo systemctl`` management.

    ``binary`` is the launcher the unit calls (default ``/usr/local/bin/pibackup``
    from install.sh's symlink). ``create_user`` controls whether the ``useradd``
    step is emitted — the CLI sets it ``False`` when the service user already
    exists, so re-runs are idempotent. ``migrate_from``, if given, is an existing
    DB dir (e.g. a prior ``--user`` install's ``~/.local/share/pibackup``) whose
    contents are copied into ``state_dir`` before the service starts, so an
    upgraded host keeps its clients, jobs, and admin.
    """
    commands: list[list[str]] = [["groupadd", "-f", group]]
    if create_user:
        # System user, no home/login shell, primary group = the shared group.
        commands.append([
            "useradd", "--system", "--no-create-home",
            "--shell", "/usr/sbin/nologin", "--gid", group, service_user,
        ])
    commands += [
        ["mkdir", "-p", str(state_dir)],
        ["chown", f"{service_user}:{group}", str(state_dir)],
        ["chmod", "2775", str(state_dir)],
    ]

    notes = [
        f"Service user '{service_user}' (system, no login) runs the daemon.",
        f"State dir {state_dir} (group '{group}', setgid) is shared with operators.",
        f"Unit {unit_path} starts at boot — no linger, no session bus.",
        f"Manage it with: sudo systemctl restart {unit_path.stem}",
    ]

    if migrate_from is not None:
        # Copy an existing user-service DB/repo into the shared state dir, then
        # fix ownership. `cp -an` won't clobber anything already in state_dir.
        commands.append(["cp", "-an", f"{migrate_from}/.", str(state_dir)])
        commands.append(["chown", "-R", f"{service_user}:{group}", str(state_dir)])
        notes.insert(
            0, f"Migrated existing state from {migrate_from} into {state_dir}."
        )

    # Write the unit, then load + enable + (re)start it.
    commands.append(["systemctl", "daemon-reload"])
    commands.append(["systemctl", "enable", "--now", unit_path.stem])

    files = [(unit_path, _unit_body(binary, service_user, group, state_dir))]

    return Plan(
        commands=commands,
        config_path=SYSTEM_CONFIG,
        config_body=_config_body(state_dir),
        notes=notes,
        files=files,
    )
