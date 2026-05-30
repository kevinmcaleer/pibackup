"""Plain gzip'd-tar archives for opt-in ``archive`` jobs (issue #41).

An *archive* job packs its sources into a single ``.tar.gz`` per run instead of
an rsync ``--link-dest`` directory snapshot. This is the smallest-on-disk option
for a *single* backup (gzip-compressed, no per-file rsync overhead), at the cost
of the cross-snapshot hardlink dedup the directory model gives — every archive
run is a full, standalone tarball. It is the unencrypted sibling of the
``tar | zstd | age`` pipeline in :mod:`pibackup.common.crypto`, using only the
stdlib so a plaintext-only client needs no extra libraries.

``tar`` + ``gzip`` are what the issue asked for and are universally available;
we use Python's :mod:`tarfile`/:mod:`gzip` rather than shelling out so the build
is cancellable per-member and trivially unit-testable.
"""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

# The snapshot filename suffix for an archive-mode run. Restore keys off this to
# tell an archive blob apart from a directory snapshot (and from the encrypted
# ``.tar.zst.age`` blob) without needing a new database column.
ARCHIVE_GZ_SUFFIX = ".tar.gz"


class ArchiveCancelled(Exception):
    """Raised when a tar.gz build is cancelled on request.

    Mirrors :class:`pibackup.common.crypto.ArchiveCancelled` and
    :data:`pibackup.common.transfer.CANCELLED_EXIT_CODE`: the caller turns this
    into the same cancelled-failure outcome the rsync path reports.
    """


def _arcname(path: Path) -> str:
    # Mirror rsync -R (and the encrypted path): preserve the absolute source
    # path inside the archive, minus the leading slash.
    return str(path).lstrip("/")


def make_tar_gz(
    sources: Sequence[str | Path],
    out_path: str | Path,
    *,
    level: int = 6,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> int:
    """Pack ``sources`` into a gzip'd tar at ``out_path``; return its size.

    ``level`` is the gzip compression level (1–9; 6 is gzip's default balance of
    ratio vs. CPU, sensible on a Pi). ``should_cancel`` (if given) is polled
    before each source and as every member is added; the first truthy result
    aborts the build, removes the partial archive, and raises
    :class:`ArchiveCancelled` — matching how :func:`run_rsync` and
    :func:`pibackup.common.crypto.encrypt_archive` tear down on a cancel.
    """
    out_path = Path(out_path)

    def _filter(info: "tarfile.TarInfo") -> "tarfile.TarInfo":
        # tarfile calls this for every member as it walks the tree, so it's our
        # per-file cancellation point; raise to unwind out of tar.add().
        if should_cancel and should_cancel():
            raise ArchiveCancelled("archive cancelled on request")
        return info

    try:
        with tarfile.open(out_path, mode="w:gz", compresslevel=level) as tar:
            for source in sources:
                if should_cancel and should_cancel():
                    raise ArchiveCancelled("archive cancelled on request")
                sp = Path(source)
                tar.add(sp, arcname=_arcname(sp), recursive=True, filter=_filter)
        return out_path.stat().st_size
    except ArchiveCancelled:
        out_path.unlink(missing_ok=True)  # drop any partial archive artifact
        raise


def extract_tar_gz(archive_path: str | Path, dest_dir: str | Path) -> None:
    """Reverse :func:`make_tar_gz`, extracting into ``dest_dir``."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # filter guards path traversal
