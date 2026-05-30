"""Restore files from a snapshot.

- Plaintext snapshots: reverse rsync from the snapshot directory into the target
  (the snapshot preserves absolute paths via ``-R``, so ``--target /`` restores
  in place, while the default target is a safe local directory).
- Encrypted snapshots: fetch the ``.tar.zst.age`` blob (if remote) and decrypt +
  extract it with the local age keys.

Bare-metal restore (replaying the system manifest onto a fresh SD card) is
Phase 7; this is the file-level half.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pibackup.common.config import Config, ssh_key_path
from pibackup.common.archive import ARCHIVE_GZ_SUFFIX
from pibackup.common.transfer import Destination, build_rsync_command, run_rsync


@dataclass
class RestoreResult:
    ok: bool
    target: str
    message: str


def restore_snapshot(config: Config, snap: dict, target_dir: str) -> RestoreResult:
    if not config.repo_target:
        return RestoreResult(False, target_dir, "no repo_target configured — can't locate the snapshot")

    key = ssh_key_path()
    dest = Destination(config.repo_target, ssh_key=str(key) if key.exists() else None)
    path = snap["path"]
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    if snap.get("encrypted"):
        return _restore_encrypted(dest, path, target)

    if path.endswith(ARCHIVE_GZ_SUFFIX):
        return _restore_archive(dest, path, target)

    src = dest.rsync_source(path).rstrip("/") + "/"
    # No default excludes on restore: the snapshot must come back verbatim, and a
    # stored path that matches a backup-time exclude (e.g. /tmp) would be dropped.
    cmd = build_rsync_command(
        src, str(target).rstrip("/") + "/", compress=True, rsh=dest.rsh, default_excludes=False
    )
    result = run_rsync(cmd)
    msg = f"restored {result.files_transferred} file(s)" if result.ok else result.message
    return RestoreResult(result.ok, str(target), msg)


def _restore_archive(dest: Destination, path: str, target: Path) -> RestoreResult:
    """Fetch (if remote) and unpack a ``.tar.gz`` archive snapshot."""
    from pibackup.common.archive import extract_tar_gz

    archive = Path(path)
    tmpdir: tempfile.TemporaryDirectory | None = None
    if dest.is_remote:
        tmpdir = tempfile.TemporaryDirectory()
        archive = Path(tmpdir.name) / Path(path).name
        result = run_rsync(build_rsync_command(f"{dest.host}:{path}", str(archive), compress=False, rsh=dest.rsh))
        if not result.ok:
            tmpdir.cleanup()
            return RestoreResult(False, str(target), result.message)

    try:
        extract_tar_gz(archive, target)
    except Exception as exc:  # truncated/corrupt blob, etc.
        return RestoreResult(False, str(target), f"extract failed: {exc}")
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    return RestoreResult(True, str(target), "extracted archive")


def _restore_encrypted(dest: Destination, path: str, target: Path) -> RestoreResult:
    from pibackup.client import keys
    from pibackup.common.crypto import decrypt_archive

    identities = keys.load_identities()
    if not identities:
        return RestoreResult(False, str(target), "no age keys available to decrypt (restore your key with `pibackup key`)")

    archive = Path(path)
    tmpdir: tempfile.TemporaryDirectory | None = None
    if dest.is_remote:
        tmpdir = tempfile.TemporaryDirectory()
        archive = Path(tmpdir.name) / Path(path).name
        result = run_rsync(build_rsync_command(f"{dest.host}:{path}", str(archive), compress=False, rsh=dest.rsh))
        if not result.ok:
            tmpdir.cleanup()
            return RestoreResult(False, str(target), result.message)

    try:
        decrypt_archive(archive, target, identities)
    except Exception as exc:  # wrong key, corrupt blob, etc.
        return RestoreResult(False, str(target), f"decrypt failed: {exc}")
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    return RestoreResult(True, str(target), "decrypted and extracted")
