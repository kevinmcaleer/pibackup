"""rsync transfer layer.

Builds and runs the rsync push, parses ``--stats`` output, classifies exit
codes, and manages the snapshot destination (local path or ``host:/path`` over
SSH). Snapshots are timestamped directories rotated with ``--link-dest`` so
unchanged files are hardlinked against the previous snapshot.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# rsync exit codes worth treating as success: 0 = OK, 24 = a source file vanished
# mid-transfer (benign on a live system).
_OK_EXIT_CODES = {0, 24}

_EXIT_MEANINGS = {
    23: "partial transfer (some files/attrs could not be transferred)",
    24: "some source files vanished during transfer",
    255: "SSH/connection error",
}


def build_rsync_command(
    sources: str | Path | Sequence[str | Path],
    dest: str,
    *,
    link_dest: Optional[str | Path] = None,
    bwlimit_kbps: Optional[int] = None,
    compress: bool = True,
    relative: bool = False,
    dry_run: bool = False,
    extra: Optional[Sequence[str]] = None,
) -> list[str]:
    """Assemble the rsync argv for a backup push.

    - ``-a``            archive mode (perms, times, symlinks, recursion)
    - ``--partial``     keep partially transferred files so runs can resume
    - ``--stats``       emit a machine-parseable transfer summary
    - ``-z``            wire compression (cheap win on slow links)
    - ``-R``            preserve absolute source paths inside the snapshot
    - ``--bwlimit``     background-friendly throttle (our stand-in for BITS)
    - ``--link-dest``   hardlink unchanged files against the previous snapshot
    """
    cmd: list[str] = ["rsync", "-a", "--partial", "--stats"]
    if compress:
        cmd.append("-z")
    if relative:
        cmd.append("-R")
    if dry_run:
        cmd.append("-n")
    if bwlimit_kbps:
        cmd.append(f"--bwlimit={bwlimit_kbps}")
    if link_dest:
        cmd.append(f"--link-dest={link_dest}")
    if extra:
        cmd.extend(extra)
    if isinstance(sources, (str, Path)):
        sources = [sources]
    cmd.extend(str(s) for s in sources)
    cmd.append(str(dest))
    return cmd


@dataclass
class RsyncResult:
    ok: bool
    exit_code: int
    bytes_transferred: int
    files_transferred: int
    message: str
    output: str


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def parse_rsync_stats(output: str) -> tuple[int, int]:
    """Return ``(bytes_transferred, files_transferred)`` from --stats output."""
    bytes_sent = 0
    files = 0

    m = re.search(r"Total bytes sent:\s*([\d,]+)", output)
    if m:
        bytes_sent = _to_int(m.group(1))
    else:
        m = re.search(r"\bsent\s+([\d,]+)\s+bytes", output)
        if m:
            bytes_sent = _to_int(m.group(1))

    m = re.search(r"Number of regular files transferred:\s*([\d,]+)", output)
    if not m:
        m = re.search(r"Number of files transferred:\s*([\d,]+)", output)
    if m:
        files = _to_int(m.group(1))

    return bytes_sent, files


def classify_exit(code: int) -> bool:
    return code in _OK_EXIT_CODES


def run_rsync(cmd: Sequence[str]) -> RsyncResult:
    """Execute rsync, capturing output and classifying the outcome."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    ok = classify_exit(proc.returncode)
    bytes_sent, files = parse_rsync_stats(output)
    if ok:
        message = f"transferred {files} file(s), {bytes_sent} bytes"
        if proc.returncode != 0:
            message += f" (rsync code {proc.returncode}: {_EXIT_MEANINGS.get(proc.returncode, 'warning')})"
    else:
        meaning = _EXIT_MEANINGS.get(proc.returncode, "rsync failure")
        first_err = (proc.stderr.strip().splitlines() or ["(no stderr)"])[0]
        message = f"rsync exit {proc.returncode}: {meaning} — {first_err}"
    return RsyncResult(
        ok=ok,
        exit_code=proc.returncode,
        bytes_transferred=bytes_sent,
        files_transferred=files,
        message=message,
        output=output,
    )


# ---------------------------------------------------------------------------
# Destination: a backup repository base, either local or remote (over SSH).
# ---------------------------------------------------------------------------


def _split_target(raw: str) -> tuple[Optional[str], str]:
    """Split an rsync target into ``(host, path)``.

    A target is remote when it contains a colon whose left side has no slash,
    e.g. ``pi@server:/srv/repo`` -> ``("pi@server", "/srv/repo")``. Otherwise
    it's a local path and host is ``None``.
    """
    if ":" in raw:
        head, _, tail = raw.partition(":")
        if "/" not in head:
            return head, tail
    return None, raw


@dataclass
class Destination:
    """A backup repository base. Knows how to inspect and prepare itself
    whether it lives on the local filesystem or on a remote host over SSH."""

    raw: str

    def __post_init__(self) -> None:
        self.host, self.base_path = _split_target(self.raw)

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    def _abs(self, subpath: str) -> str:
        return f"{self.base_path.rstrip('/')}/{subpath}" if subpath else self.base_path

    def rsync_target(self, subpath: str) -> str:
        """The rsync destination string for a subpath under the repo base."""
        path = self._abs(subpath)
        return f"{self.host}:{path}" if self.is_remote else path

    def abspath(self, subpath: str) -> str:
        """Absolute path on the destination side (for ``--link-dest``)."""
        return self._abs(subpath)

    def _ssh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", self.host, *args], capture_output=True, text=True)

    def mkdirs(self, subpath: str) -> None:
        path = self._abs(subpath)
        if self.is_remote:
            self._ssh("mkdir", "-p", shlex.quote(path))
        else:
            Path(path).mkdir(parents=True, exist_ok=True)

    def list_dir(self, subpath: str) -> list[str]:
        path = self._abs(subpath)
        if self.is_remote:
            proc = self._ssh(f"ls -1 {shlex.quote(path)} 2>/dev/null")
            return [line for line in proc.stdout.splitlines() if line]
        p = Path(path)
        return sorted(child.name for child in p.iterdir()) if p.is_dir() else []

    def update_latest(self, base_sub: str, snapshot_name: str) -> None:
        """Point ``<base_sub>/latest`` at the freshly written snapshot."""
        link = self._abs(f"{base_sub}/latest")
        if self.is_remote:
            self._ssh("ln", "-sfn", shlex.quote(snapshot_name), shlex.quote(link))
        else:
            link_path = Path(link)
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(snapshot_name)
