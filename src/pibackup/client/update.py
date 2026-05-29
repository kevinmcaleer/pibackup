"""Self-upgrade logic for `pibackup update`.

pibackup ships from a git spec and lands in the wild in a couple of shapes:

* **pipx** — the common case. The binary is a symlink into a pipx venv
  (``.../pipx/venvs/pibackup/``). pipx records the install spec (including any
  extras like ``[server]`` / ``[crypto]``) and reuses it on ``pipx upgrade``.
* **venv fallback** — ``deploy/install.sh`` uses this when pipx is unavailable:
  a self-contained venv at ``$HOME/.local/share/pibackup/venv`` symlinked onto
  PATH. There's no recorded spec, so we rebuild the git spec ourselves and
  ``pip install --upgrade`` it, preserving extras read from the installed
  distribution's metadata.

The nuance worth spelling out: the *running* process is the OLD code. So this
module only builds and runs the upgrade command; the migration step is run by
re-exec'ing the freshly installed binary with a hidden flag (see
``cli.update``), so the NEW code's ``init_db`` does the migrating.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The canonical source. Extras get spliced in as pibackup[extra] @ git+REPO@ref.
REPO_URL = "https://github.com/kevinmcaleer/pibackup"


@dataclass(frozen=True)
class InstallInfo:
    """How this pibackup was installed, and how to upgrade it.

    ``method`` is one of "pipx", "venv", or "unknown". ``command`` is the
    argv list that performs the upgrade (empty for "unknown"). ``new_binary``
    is the path to the pibackup binary that will exist after the upgrade —
    used to re-exec into the new code for migrations.
    """

    method: str
    command: list[str]
    new_binary: Path
    extras: tuple[str, ...] = ()


def _detect_extras(dist_name: str = "pibackup") -> tuple[str, ...]:
    """Best-effort: which optional-dependency extras are currently installed.

    Used only for the venv path (pipx remembers the spec itself). We can't know
    for certain which extras a spec was installed *with*, so we infer it from
    whether each extra's marker dependencies import — e.g. fastapi → [server],
    pyrage → [crypto], textual → [tui]. Conservative: only claim an extra when
    its key package is importable.
    """
    import importlib.util

    probes = {
        "server": "fastapi",
        "crypto": "pyrage",
        "tui": "textual",
    }
    found = [
        extra
        for extra, module in probes.items()
        if importlib.util.find_spec(module) is not None
    ]
    return tuple(found)


def _git_spec(extras: tuple[str, ...], ref: str) -> str:
    """Build a pip/pipx-installable git spec, preserving extras and pinning ref.

    e.g. ``pibackup[server,crypto] @ git+https://github.com/.../pibackup@main``
    """
    name = "pibackup"
    if extras:
        name = f"pibackup[{','.join(extras)}]"
    return f"{name} @ git+{REPO_URL}@{ref}"


def _venv_root(executable: Path) -> Optional[Path]:
    """Return the venv root if ``executable`` lives in a venv's bin/, else None.

    A venv has a ``pyvenv.cfg`` one level up from its ``bin/`` directory.
    """
    bindir = executable.parent
    root = bindir.parent
    if (root / "pyvenv.cfg").exists():
        return root
    return None


def detect_install(
    executable: Optional[str] = None,
    ref: str = "main",
) -> InstallInfo:
    """Figure out how pibackup is installed and how to upgrade it in place.

    ``executable`` defaults to ``sys.executable`` (the interpreter running the
    current process). For a pipx/venv install that's the venv's python, whose
    layout tells us which mechanism to use.
    """
    # NB: resolve the *directory* but NOT the final python symlink. In a real
    # pipx/venv, ``bin/python`` is a symlink to the base interpreter (e.g.
    # ``/usr/bin/python3.13``); a naive ``.resolve()`` would follow it straight
    # out of the venv, ``pyvenv.cfg`` would be missing, and detection would
    # wrongly report "unknown" (breaking ``pibackup update`` on every pipx box).
    raw = Path(executable or sys.executable)
    exe = raw.parent.resolve() / raw.name
    root = _venv_root(exe)

    # pipx lays venvs out under ``<pipx-home>/venvs/<package>/`` — detect that
    # path component and let pipx upgrade itself (it reused the recorded spec).
    if root is not None and root.parent.name == "venvs" and root.name == "pibackup":
        return InstallInfo(
            method="pipx",
            command=["pipx", "upgrade", "pibackup"],
            new_binary=root / "bin" / "pibackup",
        )

    # Self-contained venv fallback from install.sh. Rebuild the git spec
    # (preserving extras) and pip-upgrade in place.
    if root is not None:
        extras = _detect_extras()
        return InstallInfo(
            method="venv",
            command=[
                str(root / "bin" / "pip"),
                "install",
                "--upgrade",
                _git_spec(extras, ref),
            ],
            new_binary=root / "bin" / "pibackup",
            extras=extras,
        )

    # Not a recognised layout (e.g. running from a checkout / system python).
    return InstallInfo(method="unknown", command=[], new_binary=exe)
