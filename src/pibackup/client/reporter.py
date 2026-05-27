"""Reporters record a completed backup run and provide the job list.

Two implementations share one interface so the CLI's ``run`` flow doesn't care
where state lives:

- :class:`ApiReporter` — talks to the server (Phase 2 target model).
- :class:`LocalReporter` — writes to a local SQLite db and reads jobs from
  config.toml (standalone mode, also used when no server is reachable).
"""

from __future__ import annotations

import socket
from typing import Optional

from pibackup.client.api import ApiError, ServerApi
from pibackup.client.engine import JobResult
from pibackup.common.config import Config, JobSpec, load_jobs
from pibackup.common.store import Store
from pibackup.common.transfer import Progress


def _status(result: JobResult) -> str:
    return "success" if result.ok else "failure"


class LocalReporter:
    server_backed = False

    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db_path)
        self.client_id = self.store.ensure_client(config.client_name, socket.gethostname())
        self._job_ids: dict[str, int] = {}

    def jobs(self) -> list[JobSpec]:
        return load_jobs()

    def start(self, spec: JobSpec) -> Optional[int]:
        job_id = self.store.ensure_job(self.client_id, spec)
        self._job_ids[spec.name] = job_id
        return self.store.start_run(job_id)

    def progress(self, run_id: Optional[int], p: Progress) -> None:
        if run_id is None:
            return
        self.store.update_progress(run_id, p.percent, p.transferred, p.rate, p.eta)

    def finish(self, run_id: Optional[int], spec: JobSpec, result: JobResult) -> None:
        job_id = self._job_ids.get(spec.name) or self.store.ensure_job(self.client_id, spec)
        if run_id is None:  # start failed — fall back to a one-shot record
            self.record(spec, result)
            return
        self.store.finish_run(run_id, _status(result), result.bytes_transferred, result.message)
        if result.ok and result.snapshot_path:
            self.store.add_snapshot(
                job_id, run_id, result.snapshot_path, result.bytes_transferred, spec.encrypted,
            )
        from pibackup.server import retention

        retention.prune_job(self.store, job_id, str(self.config.repo_dir))

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

    def start(self, spec: JobSpec) -> Optional[int]:
        try:
            resp = self.api.start_run(self._job_id(spec.name))
            return (resp or {}).get("run_id")
        except ApiError:
            return None  # fall back to a one-shot record at finish

    def progress(self, run_id: Optional[int], p: Progress) -> None:
        if run_id is None:
            return
        try:
            self.api.update_run(
                run_id,
                {"percent": p.percent, "transferred": p.transferred, "rate": p.rate, "eta": p.eta},
            )
        except ApiError:
            pass  # progress is best-effort; never fail the backup over it

    def finish(self, run_id: Optional[int], spec: JobSpec, result: JobResult) -> None:
        if run_id is None:
            self.record(spec, result)
            return
        try:
            self.api.update_run(
                run_id,
                {
                    "status": _status(result),
                    "bytes_transferred": result.bytes_transferred,
                    "message": result.message,
                    "snapshot_path": result.snapshot_path,
                    "snapshot_size": result.bytes_transferred,
                    "encrypted": spec.encrypted,
                },
            )
        except ApiError:
            self.record(spec, result)

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
