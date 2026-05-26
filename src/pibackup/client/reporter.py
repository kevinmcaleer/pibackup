"""Reporters record a completed backup run and provide the job list.

Two implementations share one interface so the CLI's ``run`` flow doesn't care
where state lives:

- :class:`ApiReporter` — talks to the server (Phase 2 target model).
- :class:`LocalReporter` — writes to a local SQLite db and reads jobs from
  config.toml (standalone mode, also used when no server is reachable).
"""

from __future__ import annotations

import socket

from pibackup.client.api import ServerApi
from pibackup.client.engine import JobResult
from pibackup.common.config import Config, JobSpec, load_jobs
from pibackup.common.store import Store


def _status(result: JobResult) -> str:
    return "success" if result.ok else "failure"


class LocalReporter:
    server_backed = False

    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db_path)
        self.client_id = self.store.ensure_client(config.client_name, socket.gethostname())

    def jobs(self) -> list[JobSpec]:
        return load_jobs()

    def record(self, spec: JobSpec, result: JobResult) -> None:
        job_id = self.store.ensure_job(self.client_id, spec)
        run_id = self.store.record_run(
            job_id, _status(result), result.bytes_transferred, result.message,
            result.started_at, result.finished_at,
        )
        if result.ok and result.snapshot_path:
            self.store.add_snapshot(
                job_id, run_id, result.snapshot_path, result.bytes_transferred, spec.encrypted,
            )
        # Standalone mode prunes locally for parity with the server.
        from pibackup.server import retention

        retention.prune_job(self.store, job_id, str(self.config.repo_dir))


class ApiReporter:
    server_backed = True

    def __init__(self, config: Config, api: ServerApi):
        self.config = config
        self.api = api
        self.api.register_client(config.client_name, socket.gethostname())
        self._jobs = self.api.get_jobs(config.client_name) or []

    def jobs(self) -> list[JobSpec]:
        return [
            JobSpec(
                name=j["name"],
                sources=j["sources"],
                retention_days=j["retention_days"],
                bwlimit_kbps=j["bwlimit_kbps"] or 0,
                encrypted=j["encrypted"],
            )
            for j in self._jobs
        ]

    def _job_id(self, name: str) -> int:
        return next(j["id"] for j in self._jobs if j["name"] == name)

    def record(self, spec: JobSpec, result: JobResult) -> None:
        self.api.report_run(
            self._job_id(spec.name),
            {
                "status": _status(result),
                "bytes_transferred": result.bytes_transferred,
                "message": result.message,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "snapshot_path": result.snapshot_path,
                "snapshot_size": result.bytes_transferred,
                "encrypted": spec.encrypted,
            },
        )
