"""Cross-process cancel flags for in-flight backups.

A backup runs in whichever process started it (a foreground ``pibackup run``, a
systemd timer, or one kicked off by the polling agent), so a ``stop`` request
can't simply set an in-memory flag. Instead each in-flight job drops a marker
file under the data dir; a stop request creates a *cancel* marker for that job,
and the running engine notices it on its next rsync progress tick and tears the
transfer down. Markers are best-effort and self-clean once the run ends.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from pibackup.common.config import load_config


def _flags_dir() -> Path:
    return load_config().data_dir / "cancel"


def _safe(job_name: str) -> str:
    """A filesystem-safe stem for a job name (names are admin-controlled, but
    may contain slashes/spaces)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", job_name) or "job"


def _flag_path(job_name: str) -> Path:
    return _flags_dir() / f"{_safe(job_name)}.cancel"


def request_cancel(job_name: str) -> None:
    """Ask any in-flight run of ``job_name`` to stop."""
    path = _flag_path(job_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop")


def is_cancelled(job_name: str) -> bool:
    """True if a stop has been requested for ``job_name``."""
    return _flag_path(job_name).exists()


def clear_cancel(job_name: str) -> None:
    """Remove a job's cancel marker (run finished or was torn down)."""
    try:
        _flag_path(job_name).unlink()
    except FileNotFoundError:
        pass


def cancel_checker(job_name: Optional[str]):
    """A zero-arg predicate the engine can poll, or None when not cancellable."""
    if not job_name:
        return None
    return lambda: is_cancelled(job_name)
