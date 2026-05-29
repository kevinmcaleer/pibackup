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


def overview(client_name: str | None = None) -> dict:
    """One snapshot of state (jobs/runs/snapshots) with a single reachability check.

    ``client_name`` selects whose jobs to show when server-backed (defaults to
    this host's ``client_name``); it is ignored in local/standalone mode where
    only this Pi's own config jobs exist.
    """
    cfg = load_config()
    srv = server()
    if srv:
        target = client_name or cfg.client_name
        return {
            "server": True,
            "client": target,
            "jobs": srv.get_jobs(target) or [],
            "runs": srv.list_runs() or [],
            "snapshots": srv.list_snapshots() or [],
        }
    store = _store()
    return {
        "server": False,
        "client": cfg.client_name,
        "jobs": _local_jobs(),
        "runs": store.list_runs(),
        "snapshots": store.list_snapshots(),
    }


def client_names() -> list[str]:
    """Enrolled client names from the server, or just this host when standalone."""
    cfg = load_config()
    srv = server()
    if srv:
        try:
            names = [c["name"] for c in (srv.list_clients() or [])]
        except Exception:
            names = []
        if cfg.client_name not in names:
            names.append(cfg.client_name)
        return sorted(names)
    return [cfg.client_name]
