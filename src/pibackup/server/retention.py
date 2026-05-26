"""Retention: prune snapshots past their job's retention window.

The server owns storage, so pruning deletes both the snapshot directory on disk
and its database row. Directory deletion is refused for any path outside the
configured repo root, as a guard against a bad ``repo_target`` wiping the wrong
tree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from pibackup.common.store import Store


def _within(path: str, root: Optional[str]) -> bool:
    if root is None:
        return True
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:  # different drives / relative vs absolute
        return False


def _prune_one(store: Store, snap: dict, repo_root: Optional[str]) -> bool:
    path = snap["path"]
    p = Path(path)
    if p.exists():
        if not _within(path, repo_root):
            return False  # refuse to delete outside the repo root
        shutil.rmtree(p, ignore_errors=True)
    store.delete_snapshot_row(snap["id"])
    return True


def prune_all(store: Store, repo_root: Optional[str] = None) -> list[dict]:
    """Prune every expired snapshot. Returns those actually removed."""
    return [snap for snap in store.list_expired_snapshots() if _prune_one(store, snap, repo_root)]


def prune_job(store: Store, job_id: int, repo_root: Optional[str] = None) -> list[dict]:
    """Prune expired snapshots for a single job."""
    return [
        snap
        for snap in store.list_expired_snapshots()
        if snap["job_id"] == job_id and _prune_one(store, snap, repo_root)
    ]


def delete_snapshot(store: Store, snap_id: int, repo_root: Optional[str] = None) -> bool:
    """Delete a specific snapshot (directory + row), regardless of age."""
    snap = store.get_snapshot(snap_id)
    if snap is None:
        return False
    return _prune_one(store, snap, repo_root)
