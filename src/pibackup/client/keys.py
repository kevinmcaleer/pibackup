"""Local age key store at ``$XDG_CONFIG_HOME/pibackup/keys/<name>.key``.

Each file holds the age secret (identity); the public recipient is recomputed
from it on demand, so there's nothing to keep in sync. Files are mode 0600.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from pibackup.common import crypto
from pibackup.common.config import config_dir


def keys_dir() -> Path:
    return config_dir() / "keys"


def _ensure_dir() -> Path:
    d = keys_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _key_path(name: str) -> Path:
    return keys_dir() / f"{name}.key"


def _read_secret(path: Path) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise ValueError(f"no key material in {path}")


def create_key(name: str = "default") -> tuple[str, Path]:
    _ensure_dir()
    path = _key_path(name)
    if path.exists():
        raise FileExistsError(name)
    secret, recipient = crypto.generate_keypair()
    path.write_text(f"# pibackup age key '{name}'\n# public key: {recipient}\n{secret}\n")
    os.chmod(path, 0o600)
    return recipient, path


def list_keys() -> list[dict]:
    d = keys_dir()
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("*.key")):
        try:
            recipient = crypto.recipient_from_secret(_read_secret(path))
        except Exception:
            recipient = "(unreadable)"
        out.append(
            {
                "name": path.stem,
                "recipient": recipient,
                "path": str(path),
                "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
            }
        )
    return out


def export_key(name: str) -> str:
    path = _key_path(name)
    if not path.exists():
        raise FileNotFoundError(name)
    return crypto.recipient_from_secret(_read_secret(path))


def remove_key(name: str) -> bool:
    path = _key_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def load_identities() -> list[str]:
    """All stored secrets, for decryption (age tries each)."""
    d = keys_dir()
    if not d.exists():
        return []
    return [_read_secret(p) for p in sorted(d.glob("*.key"))]


def default_recipient() -> Optional[str]:
    """If exactly one key exists, its recipient — a convenient default."""
    keys = list_keys()
    return keys[0]["recipient"] if len(keys) == 1 else None
