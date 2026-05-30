"""Configuration resolution.

Defaults follow the XDG spec and can be overridden by a TOML file at
``$XDG_CONFIG_HOME/pibackup/config.toml`` or by the ``PIBACKUP_DATA_DIR`` env var.

The same ``config.toml`` also holds the client's backup jobs (Phase 1 is
"manually configured"; Phase 2 will sync these from the server):

    repo_target = "pi@server:/srv/pibackup/repo"   # rsync destination base
    client_name = "kitchen-pi"                      # defaults to the hostname

    [[job]]
    name = "home"
    sources = ["/home/kev"]
    retention_days = 30
    bwlimit_kbps = 0          # 0 = unlimited
    encrypted = false         # Phase 4
    archive = false           # issue #41: pack the run into a single .tar.gz
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env_path(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


def data_dir() -> Path:
    base = _env_path("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return _env_path("PIBACKUP_DATA_DIR", base / "pibackup")


def config_dir() -> Path:
    base = _env_path("XDG_CONFIG_HOME", Path.home() / ".config")
    return base / "pibackup"


def config_file() -> Path:
    return config_dir() / "config.toml"


def system_config_file() -> Path:
    """A system-wide config layered *under* the per-user config.toml.

    This is how the Docker-style shared-state model (issue #33) points every
    operator at one DB: ``pibackup admin enable-group`` writes this file with a
    ``data_dir`` under ``/var/lib/pibackup`` so the service user and any member
    of the ``pibackup`` group resolve the *same* state, regardless of whose
    ``$HOME`` they run as. Overridable via ``PIBACKUP_SYSTEM_CONFIG`` for tests.
    Absent (the default on a fresh user install), behaviour is unchanged.
    """
    return _env_path("PIBACKUP_SYSTEM_CONFIG", Path("/etc/pibackup/config.toml"))


def default_server_url() -> str:
    """The server URL when config.toml doesn't set one. Overridable via
    ``PIBACKUP_SERVER_URL`` (mirrors ``PIBACKUP_DATA_DIR``) so tests and
    alternate deployments can point elsewhere without writing a config file."""
    return os.environ.get("PIBACKUP_SERVER_URL", "http://127.0.0.1:8765")


def ssh_key_path() -> Path:
    """The SSH identity generated at enrollment, used for rsync-over-SSH."""
    return config_dir() / "ssh" / "id_ed25519"


@dataclass(frozen=True)
class Config:
    data_dir: Path  # root for all server state
    repo_dir: Path  # where the server stores snapshots locally
    db_path: Path  # SQLite database
    server_url: str = "http://127.0.0.1:8765"
    repo_target: Optional[str] = None  # rsync destination base for the client
    recipient: Optional[str] = None  # age public key for encrypted jobs
    authorized_keys: Optional[str] = None  # server: append enrolled SSH keys here
    background: bool = True  # run backup transfers under nice/ionice
    client_name: str = field(default_factory=socket.gethostname)


@dataclass(frozen=True)
class JobSpec:
    """A client-side backup job definition (from config.toml)."""

    name: str
    sources: list[str]
    retention_days: int = 30
    bwlimit_kbps: int = 0  # 0 = unlimited
    encrypted: bool = False
    archive: bool = False  # pack each run into a single .tar.gz (issue #41)


def _load_toml() -> dict:
    """Merge the system config (if any) under the per-user config.

    Per-user ``config.toml`` keys win over the system file, so an operator can
    still override anything locally; the system file (written by
    ``admin enable-group``) supplies the shared ``data_dir`` both the service
    user and grouped operators resolve to. A fresh user install has no system
    file, so this collapses to the original single-file behaviour.
    """
    merged: dict = {}
    for cfg_path in (system_config_file(), config_file()):
        if not cfg_path.exists():
            continue
        with cfg_path.open("rb") as fh:
            merged.update(tomllib.load(fh))
    return merged


def load_config() -> Config:
    """Load config, layering an optional TOML file over the XDG defaults."""
    overrides = _load_toml()
    ddir = Path(overrides.get("data_dir", data_dir()))
    return Config(
        data_dir=ddir,
        repo_dir=Path(overrides.get("repo_dir", ddir / "repo")),
        db_path=Path(overrides.get("db_path", ddir / "pibackup.db")),
        server_url=overrides.get("server_url", default_server_url()),
        repo_target=overrides.get("repo_target"),
        recipient=overrides.get("recipient"),
        authorized_keys=overrides.get("authorized_keys"),
        background=bool(overrides.get("background", True)),
        client_name=overrides.get("client_name", socket.gethostname()),
    )


def load_jobs() -> list[JobSpec]:
    """Read the client's configured backup jobs from config.toml."""
    overrides = _load_toml()
    jobs: list[JobSpec] = []
    for entry in overrides.get("job", []):
        jobs.append(
            JobSpec(
                name=entry["name"],
                sources=list(entry["sources"]),
                retention_days=int(entry.get("retention_days", 30)),
                bwlimit_kbps=int(entry.get("bwlimit_kbps", 0)),
                encrypted=bool(entry.get("encrypted", False)),
                archive=bool(entry.get("archive", False)),
            )
        )
    return jobs
