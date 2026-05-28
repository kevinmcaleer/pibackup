"""rsync transfer layer.

Builds and runs the rsync push, parses ``--stats`` output, classifies exit
codes, and manages the snapshot destination (local path or ``host:/path`` over
SSH). Snapshots are timestamped directories rotated with ``--link-dest`` so
unchanged files are hardlinked against the previous snapshot.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# rsync exit codes worth treating as success: 0 = OK, 24 = a source file vanished
# mid-transfer (benign on a live system).
_OK_EXIT_CODES = {0, 24}

# Directories that are always inaccessible or useless to back up.
# These are excluded by default so rsync never returns exit 23 for them.
_DEFAULT_EXCLUDES = [
    "lost+found",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/tmp",
    "/var/tmp",
]

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
    rsh: Optional[str] = None,
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
    - ``-e``            the SSH transport (``rsh``), so the enrolled key is used
    """
    cmd: list[str] = ["rsync", "-a", "--partial", "--stats"]
    if compress:
        cmd.append("-z")
    if relative:
        cmd.append("-R")
    if dry_run:
        cmd.append("-n")
    if rsh:
        cmd += ["-e", rsh]
    if bwlimit_kbps:
        cmd.append(f"--bwlimit={bwlimit_kbps}")
    if link_dest:
        cmd.append(f"--link-dest={link_dest}")
    for exc in _DEFAULT_EXCLUDES:
        cmd.append(f"--exclude={exc}")
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


def background_prefix() -> list[str]:
    """A ``nice``/``ionice`` prefix so backups stay out of the way of foreground
    work (empty if the tools aren't available). Exit codes pass through both,
    so this is transparent to :func:`run_rsync`."""
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]  # idle I/O class
    return prefix


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
# SSH transport: drive rsync/ssh with the pibackup-managed key so a client
# needs no manual ~/.ssh/config and unattended runs never stall on a prompt.
# ---------------------------------------------------------------------------


def ssh_options(ssh_key: str) -> list[str]:
    """SSH options for an unattended push with the enrolled key:

    - ``-i``/``IdentitiesOnly`` use *only* this key (ignore the agent/defaults)
    - ``StrictHostKeyChecking=accept-new`` trust a host the first time, but
      still refuse if a *known* host key later changes
    - ``BatchMode=yes`` never prompt — fail fast instead of hanging a timer
    - ``ConnectTimeout`` bound the wait on an unreachable server

    Assumes a space-free key path (true for the XDG default).
    """
    return [
        "-i", ssh_key,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
    ]


def ssh_rsh(ssh_key: Optional[str]) -> Optional[str]:
    """The rsync ``-e`` transport string for SSH with ``ssh_key``. Returns
    ``None`` to leave rsync's default SSH untouched (local dest, or a client
    that hasn't enrolled yet)."""
    if not ssh_key:
        return None
    return "ssh " + " ".join(ssh_options(ssh_key))


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
    ssh_key: Optional[str] = None  # enrolled SSH identity for remote transfers

    def __post_init__(self) -> None:
        self.host, self.base_path = _split_target(self.raw)

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    @property
    def rsh(self) -> Optional[str]:
        """rsync ``-e`` transport for a remote destination (None when local)."""
        return ssh_rsh(self.ssh_key) if self.is_remote else None

    def _ssh_argv(self) -> list[str]:
        """The ``ssh`` argv prefix, carrying the enrolled key when we have one."""
        return ["ssh", *(ssh_options(self.ssh_key) if self.ssh_key else [])]

    def _abs(self, subpath: str) -> str:
        return f"{self.base_path.rstrip('/')}/{subpath}" if subpath else self.base_path

    def rsync_target(self, subpath: str) -> str:
        """The rsync destination string for a subpath under the repo base."""
        path = self._abs(subpath)
        return f"{self.host}:{path}" if self.is_remote else path

    def abspath(self, subpath: str) -> str:
        """Absolute path on the destination side (for ``--link-dest``)."""
        return self._abs(subpath)

    def rsync_source(self, abspath: str) -> str:
        """An rsync source string for an absolute path on the destination
        (used by restore: ``host:/path`` if remote, else the local path)."""
        return f"{self.host}:{abspath}" if self.is_remote else abspath

    def _ssh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self._ssh_argv(), self.host, *args], capture_output=True, text=True)

    def mkdirs(self, subpath: str) -> None:
        path = self._abs(subpath)
        if self.is_remote:
            result = self._ssh("mkdir", "-p", shlex.quote(path))
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create remote directory {path}: {result.stderr.strip()}"
                )
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
