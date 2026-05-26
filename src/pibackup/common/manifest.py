"""System manifest capture for restore.

Snapshots the state needed to understand (and later rebuild) a Pi: hostname, OS
release, manually-installed apt packages, ``pip freeze``, enabled systemd
services, crontab, fstab, and the Pi boot config. Every probe is best-effort —
missing tools or failures yield empty/None rather than raising.

Phase 7 will replay this onto a fresh SD card; Phase 6 just captures it.
"""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _run(cmd: list[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _lines(text: Optional[str]) -> list[str]:
    return text.splitlines() if text else []


def capture() -> dict:
    """Gather a best-effort snapshot of system state as a JSON-able dict."""
    manifest: dict = {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": socket.gethostname(),
        "os_release": _read("/etc/os-release"),
        "fstab": _read("/etc/fstab"),
    }

    if shutil.which("apt-mark"):
        manifest["apt_manual"] = _lines(_run(["apt-mark", "showmanual"]))

    pip = shutil.which("pip3") or shutil.which("pip")
    if pip:
        manifest["pip_freeze"] = _lines(_run([pip, "freeze"]))

    if shutil.which("systemctl"):
        enabled = _run(
            ["systemctl", "list-unit-files", "--state=enabled", "--type=service", "--no-legend"]
        )
        manifest["systemd_enabled"] = [line.split()[0] for line in _lines(enabled) if line.split()]

    manifest["crontab"] = _lines(_run(["crontab", "-l"]))

    for candidate in ("/boot/firmware/config.txt", "/boot/config.txt"):
        content = _read(candidate)
        if content is not None:
            manifest["boot_config_path"] = candidate
            manifest["boot_config"] = content
            break

    return manifest


def to_json(manifest: Optional[dict] = None) -> str:
    return json.dumps(manifest if manifest is not None else capture(), indent=2)


def write(path: str | Path) -> Path:
    p = Path(path)
    p.write_text(to_json())
    return p


def render_restore_script(manifest: dict) -> str:
    """Generate a shell script that replays a manifest onto a fresh system:
    hostname, apt packages, pip packages, enabled systemd services.

    The file restore (snapshot contents) is handled separately by
    ``pibackup restore``; this rebuilds the surrounding system state.
    """
    out = [
        "#!/bin/sh",
        f"# pibackup system restore — from manifest captured {manifest.get('captured_at', '?')}",
        "# Review before running. Intended to run as root on a fresh install.",
        "set -e",
        "",
    ]

    host = manifest.get("hostname")
    if host:
        out += ["# hostname", f"hostnamectl set-hostname {shlex.quote(host)}", ""]

    apt = manifest.get("apt_manual") or []
    if apt:
        pkgs = " ".join(shlex.quote(p) for p in apt)
        out += ["# apt packages", "apt-get update", f"apt-get install -y {pkgs}", ""]

    pip = manifest.get("pip_freeze") or []
    if pip:
        pkgs = " ".join(shlex.quote(p) for p in pip)
        out += ["# pip packages", f"pip install {pkgs}", ""]

    services = manifest.get("systemd_enabled") or []
    if services:
        svc = " ".join(shlex.quote(s) for s in services)
        out += ["# systemd services", f"systemctl enable {svc}", ""]

    return "\n".join(out) + "\n"
