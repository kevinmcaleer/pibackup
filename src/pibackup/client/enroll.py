"""Client-side enrollment: SSH key generation, server handshake, config writing.

One command on a fresh Pi (`pibackup connect <url> --token …`) generates an SSH
keypair, registers with the server (sending the public key), and writes a
config.toml pointed at the server and its repo.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

from pibackup.client.api import ServerApi
from pibackup.common.config import JobSpec, config_dir, config_file, load_jobs


def ssh_key_path() -> Path:
    return config_dir() / "ssh" / "id_ed25519"


def ensure_ssh_key() -> tuple[Path, str]:
    """Generate an ed25519 keypair if absent; return (private_path, public_key)."""
    priv = ssh_key_path()
    priv.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(priv.parent, 0o700)
    pub = Path(str(priv) + ".pub")
    if not priv.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv),
             "-C", f"pibackup@{socket.gethostname()}"],
            check=True, capture_output=True, text=True,
        )
    return priv, pub.read_text().strip()


def _serialize_config(top: dict, jobs: list[JobSpec]) -> str:
    lines = [f'{k} = "{v}"' for k, v in top.items() if v is not None]
    for job in jobs:
        srcs = ", ".join(f'"{s}"' for s in job.sources)
        lines += ["", "[[job]]", f'name = "{job.name}"', f"sources = [{srcs}]",
                  f"retention_days = {job.retention_days}"]
        if job.bwlimit_kbps:
            lines.append(f"bwlimit_kbps = {job.bwlimit_kbps}")
        if job.encrypted:
            lines.append("encrypted = true")
    return "\n".join(lines) + "\n"


def write_config(*, name: str, server_url: str, repo_target: str | None) -> Path:
    existing_jobs = load_jobs()  # preserve any locally-defined jobs
    top = {"repo_target": repo_target, "client_name": name, "server_url": server_url}
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_config(top, existing_jobs))
    return path


def connect_to_server(url: str, name: str, token: str) -> dict:
    """Generate a key, enroll with the server, and write config. Returns the
    server's enrollment response (repo_target, jobs)."""
    _, public_key = ensure_ssh_key()
    resp = ServerApi(url).enroll(name, token, socket.gethostname(), public_key) or {}
    write_config(name=name, server_url=url, repo_target=resp.get("repo_target"))
    return resp
