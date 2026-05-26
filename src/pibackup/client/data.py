"""Read helpers shared by the CLI and TUI: jobs, runs, snapshots.

Pulls from the server when reachable, else the local SQLite store / config.
"""

from __future__ import annotations

from pibackup.client.runner import server
from pibackup.common.config import load_config, load_jobs


def _store():
    from pibackup.common.store import Store

    return Store(load_config().db_path)


def _local_jobs() -> list[dict]:
    return [
        {
            "name": j.name,
            "sources": j.sources,
            "retention_days": j.retention_days,
            "bwlimit_kbps": j.bwlimit_kbps,
            "encrypted": j.encrypted,
        }
        for j in load_jobs()
    ]


def overview() -> dict:
    """One snapshot of state (jobs/runs/snapshots) with a single reachability check."""
    cfg = load_config()
    srv = server()
    if srv:
        return {
            "server": True,
            "jobs": srv.get_jobs(cfg.client_name) or [],
            "runs": srv.list_runs() or [],
            "snapshots": srv.list_snapshots() or [],
        }
    store = _store()
    return {
        "server": False,
        "jobs": _local_jobs(),
        "runs": store.list_runs(),
        "snapshots": store.list_snapshots(),
    }
