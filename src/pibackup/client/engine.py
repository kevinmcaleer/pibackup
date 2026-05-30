"""Backup engine: turn a job spec into a snapshot on the destination.

Two modes:

- **Plaintext** — rsync directory snapshots rotated with ``--link-dest`` so
  unchanged files are hardlinked. Layout::

      <repo>/<client>/<job>/<UTC timestamp>/   # this run's snapshot
      <repo>/<client>/<job>/latest             # symlink to newest

- **Encrypted** — a single ``tar | zstd | age`` archive per run, rsync'd as an
  opaque blob the server can't read. Layout::

      <repo>/<client>/<job>/<UTC timestamp>.tar.zst.age
      <repo>/<client>/<job>/latest             # symlink to newest archive

- **Archive** — a single gzip'd tar (``.tar.gz``) per run (issue #41), the
  smallest-on-disk option for a one-off backup but with no cross-snapshot
  hardlink dedup. Layout::

      <repo>/<client>/<job>/<UTC timestamp>.tar.gz
      <repo>/<client>/<job>/latest             # symlink to newest archive

The engine only performs the transfer; recording the run is the reporter's job.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pibackup.common.config import Config, JobSpec, ssh_key_path
from pibackup.common.crypto import ARCHIVE_SUFFIX
from pibackup.common.transfer import (
    CANCELLED_EXIT_CODE,
    Destination,
    background_prefix,
    build_rsync_command,
    run_rsync,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class JobResult:
    job: str
    ok: bool
    snapshot: Optional[str]  # timestamp/archive name, or None
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
        # Use the enrolled SSH key for remote pushes so no manual ~/.ssh/config
        # is needed; falls back to default SSH when unenrolled or local.
        key = ssh_key_path()
        self.dest = Destination(config.repo_target, ssh_key=str(key) if key.exists() else None)

    def _rsync(self, cmd: list[str], on_progress=None, should_cancel=None):
        """Run rsync, wrapped in nice/ionice when background mode is on."""
        if self.config.background:
            cmd = background_prefix() + cmd
        return run_rsync(cmd, on_progress, should_cancel=should_cancel)

    def run_job(
        self,
        spec: JobSpec,
        *,
        dry_run: bool = False,
        recipient: Optional[str] = None,
        on_progress=None,
        should_cancel=None,
    ) -> JobResult:
        base_sub = f"{self.config.client_name}/{spec.name}"
        self.dest.mkdirs(base_sub)
        if spec.encrypted:
            return self._run_encrypted(spec, base_sub, recipient, dry_run, on_progress, should_cancel)
        if spec.archive:
            return self._run_archive(spec, base_sub, dry_run, on_progress, should_cancel)
        return self._run_plaintext(spec, base_sub, dry_run, on_progress, should_cancel)

    # ----- plaintext: rsync --link-dest snapshots -----
    def _run_plaintext(self, spec: JobSpec, base_sub: str, dry_run: bool, on_progress=None, should_cancel=None) -> JobResult:
        existing = sorted(n for n in self.dest.list_dir(base_sub) if n != "latest")
        prev = existing[-1] if existing else None
        link_dest = self.dest.abspath(f"{base_sub}/{prev}") if prev else None

        stamp = _timestamp()
        snap_sub = f"{base_sub}/{stamp}"
        if not dry_run:
            self.dest.mkdirs(snap_sub)
        target = self.dest.rsync_target(snap_sub) + "/"

        cmd = build_rsync_command(
            spec.sources, target,
            link_dest=link_dest, bwlimit_kbps=spec.bwlimit_kbps or None,
            relative=True, dry_run=dry_run, rsh=self.dest.rsh, progress=not dry_run,
        )
        started = _now_iso()
        result = self._rsync(cmd, None if dry_run else on_progress, should_cancel)
        finished = _now_iso()

        snapshot = snapshot_path = None
        if result.ok and not dry_run:
            self.dest.update_latest(base_sub, stamp)
            snapshot = stamp
            snapshot_path = self.dest.abspath(snap_sub)

        return JobResult(
            spec.name, result.ok, snapshot, snapshot_path,
            result.bytes_transferred, result.files_transferred, result.message,
            started, finished,
        )

    # ----- encrypted: tar | zstd | age archive -----
    def _run_encrypted(
        self, spec: JobSpec, base_sub: str, recipient: Optional[str], dry_run: bool,
        on_progress=None, should_cancel=None,
    ) -> JobResult:
        started = _now_iso()
        archive_name = f"{_timestamp()}{ARCHIVE_SUFFIX}"

        if not recipient:
            return JobResult(
                spec.name, False, None, None, 0, 0,
                "no encryption recipient (run `pibackup key create` or set recipient in config.toml)",
                started, _now_iso(),
            )
        if dry_run:
            return JobResult(
                spec.name, True, None, None, 0, 0,
                f"would encrypt {len(spec.sources)} source(s) -> {archive_name}",
                started, _now_iso(),
            )

        from pibackup.common.crypto import ArchiveCancelled, encrypt_archive

        snap_sub = f"{base_sub}/{archive_name}"
        with tempfile.TemporaryDirectory() as tmp:
            local_archive = Path(tmp) / archive_name
            try:
                size = encrypt_archive(
                    spec.sources, local_archive, recipient, should_cancel=should_cancel
                )
            except ArchiveCancelled:
                # Report the same cancelled-failure outcome as the rsync path
                # (exit 130, a "cancelled on request" message) so a stop during
                # archiving reads identically to one during the push.
                # encrypt_archive has already removed the partial archive; the
                # tempdir takes care of the rest.
                return JobResult(
                    spec.name, False, None, None, 0, 0,
                    f"encrypt exit {CANCELLED_EXIT_CODE}: cancelled on request",
                    started, _now_iso(),
                )
            cmd = build_rsync_command(
                str(local_archive), self.dest.rsync_target(snap_sub),
                compress=False, bwlimit_kbps=spec.bwlimit_kbps or None,
                rsh=self.dest.rsh, progress=True,
            )
            result = self._rsync(cmd, on_progress, should_cancel)
        finished = _now_iso()

        if not result.ok:
            return JobResult(
                spec.name, False, None, None, result.bytes_transferred, 0,
                result.message, started, finished,
            )
        self.dest.update_latest(base_sub, archive_name)
        return JobResult(
            spec.name, True, archive_name, self.dest.abspath(snap_sub),
            result.bytes_transferred or size, 1,
            f"encrypted {size} bytes -> {archive_name}", started, finished,
        )

    # ----- archive: plain tar.gz (issue #41) -----
    def _run_archive(
        self, spec: JobSpec, base_sub: str, dry_run: bool,
        on_progress=None, should_cancel=None,
    ) -> JobResult:
        from pibackup.common.archive import (
            ARCHIVE_GZ_SUFFIX,
            ArchiveCancelled,
            make_tar_gz,
        )

        started = _now_iso()
        archive_name = f"{_timestamp()}{ARCHIVE_GZ_SUFFIX}"

        if dry_run:
            return JobResult(
                spec.name, True, None, None, 0, 0,
                f"would archive {len(spec.sources)} source(s) -> {archive_name}",
                started, _now_iso(),
            )

        snap_sub = f"{base_sub}/{archive_name}"
        with tempfile.TemporaryDirectory() as tmp:
            local_archive = Path(tmp) / archive_name
            try:
                size = make_tar_gz(spec.sources, local_archive, should_cancel=should_cancel)
            except ArchiveCancelled:
                # Report the same cancelled-failure outcome as the rsync path
                # (exit 130). make_tar_gz already removed the partial archive.
                return JobResult(
                    spec.name, False, None, None, 0, 0,
                    f"archive exit {CANCELLED_EXIT_CODE}: cancelled on request",
                    started, _now_iso(),
                )
            cmd = build_rsync_command(
                str(local_archive), self.dest.rsync_target(snap_sub),
                compress=False, bwlimit_kbps=spec.bwlimit_kbps or None,
                rsh=self.dest.rsh, progress=True,
            )
            result = self._rsync(cmd, on_progress, should_cancel)
        finished = _now_iso()

        if not result.ok:
            return JobResult(
                spec.name, False, None, None, result.bytes_transferred, 0,
                result.message, started, finished,
            )
        self.dest.update_latest(base_sub, archive_name)
        return JobResult(
            spec.name, True, archive_name, self.dest.abspath(snap_sub),
            result.bytes_transferred or size, 1,
            f"archived {size} bytes -> {archive_name}", started, finished,
        )
