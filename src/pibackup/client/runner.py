"""Shared backup-run orchestration used by both the CLI and the TUI.

Resolves jobs (from the server when reachable, else local config), runs each one
through the engine, and reports the result. Raises :class:`RunError` with a
human-readable message for the caller to present.
"""

from __future__ import annotations

from typing import Callable, Optional

from pibackup.client.engine import BackupEngine, JobResult
from pibackup.common.config import load_config


class RunError(Exception):
    """A run could not proceed; ``str(exc)`` is safe to show the user."""

    def __init__(self, message: str, *, server_backed: bool = False):
        super().__init__(message)
        self.server_backed = server_backed


def server():
    """A reachable ServerApi, or None to fall back to local state."""
    from pibackup.client.api import ServerApi

    api = ServerApi(load_config().server_url)
    return api if api.reachable() else None


def build_reporter(cfg):
    from pibackup.client.reporter import ApiReporter, LocalReporter

    srv = server()
    return ApiReporter(cfg, srv) if srv else LocalReporter(cfg)


def _resolve_recipient(cfg, specs) -> Optional[str]:
    if not any(s.encrypted for s in specs):
        return None
    from pibackup.client import keys
    from pibackup.common.crypto import crypto_available

    if not crypto_available():
        raise RunError("encryption libraries missing — install with: pip install 'pibackup[crypto]'")
    recipient = cfg.recipient or keys.default_recipient()
    if not recipient:
        raise RunError("an encrypted job is selected but no recipient is set (run `pibackup key create`)")
    return recipient


def run_jobs(
    job_name: Optional[str] = None,
    *,
    dry_run: bool = False,
    on_result: Optional[Callable[[str, JobResult], None]] = None,
) -> list[JobResult]:
    """Run all jobs (or one named job). Calls ``on_result(name, result)`` after
    each. Raises :class:`RunError` if nothing can run."""
    cfg = load_config()
    reporter = build_reporter(cfg)  # may raise ApiError

    specs = reporter.jobs()
    if not specs:
        raise RunError(
            "no jobs on the server for this client" if reporter.server_backed else "no jobs configured",
            server_backed=reporter.server_backed,
        )
    if job_name:
        specs = [s for s in specs if s.name == job_name]
        if not specs:
            raise RunError(f"no such job: {job_name}")

    try:
        engine = BackupEngine(cfg)
    except ValueError as exc:
        raise RunError(str(exc))

    recipient = _resolve_recipient(cfg, specs)

    results: list[JobResult] = []
    for spec in specs:
        run_id = None
        on_progress = None
        if not dry_run:
            run_id = reporter.start(spec)  # opens a 'running' run for live progress
            if run_id is not None:
                on_progress = lambda p, rid=run_id: reporter.progress(rid, p)

        res = engine.run_job(
            spec, dry_run=dry_run, recipient=recipient, on_progress=on_progress
        )
        if not dry_run:
            reporter.finish(run_id, spec, res)
        results.append(res)
        if on_result:
            on_result(spec.name, res)
    return results
