"""Thin HTTP client for the pibackup server API.

Uses the standard library only, so installing the client pulls no extra deps.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


class ApiError(Exception):
    pass


class ServerApi:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ApiError(f"{exc.code} {exc.reason}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"cannot reach server at {self.base}: {exc}") from exc

    @staticmethod
    def _seg(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def reachable(self) -> bool:
        try:
            self._request("GET", "/health")
            return True
        except ApiError:
            return False

    # clients
    def register_client(self, name: str, hostname: Optional[str] = None):
        return self._request("POST", "/clients", {"name": name, "hostname": hostname})

    def list_clients(self):
        return self._request("GET", "/clients")

    def enroll(self, name: str, token: str, hostname: Optional[str], ssh_public_key: Optional[str]):
        return self._request(
            "POST", "/enroll",
            {"name": name, "token": token, "hostname": hostname, "ssh_public_key": ssh_public_key},
        )

    # jobs
    def get_jobs(self, client_name: str):
        return self._request("GET", f"/clients/{self._seg(client_name)}/jobs")

    def create_job(self, client_name: str, spec: dict):
        return self._request("POST", f"/clients/{self._seg(client_name)}/jobs", spec)

    def list_jobs(self):
        return self._request("GET", "/jobs")

    def delete_job(self, job_id: int):
        return self._request("DELETE", f"/jobs/{job_id}")

    # commands (start/stop a job from the server side)
    def start_job(self, job_id: int):
        return self._request("POST", f"/jobs/{job_id}/start")

    def stop_job(self, job_id: int):
        return self._request("POST", f"/jobs/{job_id}/stop")

    def list_commands(self):
        return self._request("GET", "/commands")

    def pending_commands(self, client_name: str):
        return self._request("GET", f"/clients/{self._seg(client_name)}/commands")

    def update_command(self, command_id: int, payload: dict):
        return self._request("PATCH", f"/commands/{command_id}", payload)

    # runs + snapshots
    def report_run(self, job_id: int, payload: dict):
        return self._request("POST", f"/jobs/{job_id}/runs", payload)

    def start_run(self, job_id: int):
        """Open a 'running' run and return its id (for live progress)."""
        return self._request("POST", f"/jobs/{job_id}/runs", {"status": "running"})

    def update_run(self, run_id: int, payload: dict):
        """Patch a run with a progress tick or a terminal result."""
        return self._request("PATCH", f"/runs/{run_id}", payload)

    def list_runs(self):
        return self._request("GET", "/runs")

    def list_snapshots(self):
        return self._request("GET", "/snapshots")

    def delete_snapshot(self, snap_id: int):
        return self._request("DELETE", f"/snapshots/{snap_id}")

    def prune(self):
        return self._request("POST", "/maintenance/prune")
