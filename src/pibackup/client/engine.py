"""Backup engine: turn a job spec into a timestamped, hardlinked snapshot.

Layout on the destination::

    <repo_target>/<client>/<job>/<UTC timestamp>/   # this run's snapshot
    <repo_target>/<client>/<job>/latest             # symlink to newest

Each run uses ``--link-dest`` against the previous snapshot so unchanged files
are hardlinked rather than re-sent, and ``-R`` so absolute source paths are
preserved inside the snapshot (which makes restore straightforward later).

The engine only performs the transfer; recording the run (locally or to the
server) is the reporter's job — see :mod:`pibackup.client.reporter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pibackup.common.config import Config, JobSpec
from pibackup.common.transfer import Destination, build_rsync_command, run_rsync


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class JobResult:
    job: str
    ok: bool
    snapshot: Optional[str]  # timestamp name, or None
    snapshot_path: Optional[str]  # absolute path on the destination, or None
    bytes_transferred: int
    files_transferred: int
    message: str
    started_at: str
    finished_at: str


class BackupEngine:
    def __init__(self, config: Config):
        if not config.repo_target:
            raise ValueError(
                'No repo_target configured. Set it in config.toml, e.g. '
                'repo_target = "pi@server:/srv/pibackup/repo"'
            )
        self.config = config
        self.dest = Destination(config.repo_target)

    def run_job(self, spec: JobSpec, *, dry_run: bool = False) -> JobResult:
        base_sub = f"{self.config.client_name}/{spec.name}"
        self.dest.mkdirs(base_sub)

        # Newest existing snapshot becomes the --link-dest base.
        existing = sorted(n for n in self.dest.list_dir(base_sub) if n != "latest")
        prev = existing[-1] if existing else None
        link_dest = self.dest.abspath(f"{base_sub}/{prev}") if prev else None

        stamp = _timestamp()
        snap_sub = f"{base_sub}/{stamp}"
        if not dry_run:
            self.dest.mkdirs(snap_sub)
        target = self.dest.rsync_target(snap_sub) + "/"

        cmd = build_rsync_command(
            spec.sources,
            target,
            link_dest=link_dest,
            bwlimit_kbps=spec.bwlimit_kbps or None,
            relative=True,
            dry_run=dry_run,
        )

        started = _now_iso()
        result = run_rsync(cmd)
        finished = _now_iso()

        snapshot = snapshot_path = None
        if result.ok and not dry_run:
            self.dest.update_latest(base_sub, stamp)
            snapshot = stamp
            snapshot_path = self.dest.abspath(snap_sub)

        return JobResult(
            job=spec.name,
            ok=result.ok,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            bytes_transferred=result.bytes_transferred,
            files_transferred=result.files_transferred,
            message=result.message,
            started_at=started,
            finished_at=finished,
        )
