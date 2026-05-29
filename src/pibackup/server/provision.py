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
STATE_DIR = Path("/var/lib/pibackup")
SYSTEM_CONFIG = Path("/etc/pibackup/config.toml")


@dataclass(frozen=True)
class Plan:
    """A provisioning plan: ordered shell commands + a file to write.

    ``commands`` are argv lists (run in order). ``config_path`` / ``config_body``
    describe the system config TOML to write. ``notes`` are human-facing lines
    summarising what happens and the one-time re-login caveat.
    """

    commands: list[list[str]] = field(default_factory=list)
    config_path: Path = SYSTEM_CONFIG
    config_body: str = ""
    operator: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_shell(self) -> str:
        """Render the plan as copy-pasteable shell (for --dry-run)."""
        lines = [" ".join(shlex.quote(part) for part in cmd) for cmd in self.commands]
        lines.append(
            f"write {shlex.quote(str(self.config_path))}:\n"
            + "\n".join(f"    {ln}" for ln in self.config_body.splitlines())
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
