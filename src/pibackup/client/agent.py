"""Client-side command agent: act on start/stop requests from the server.

Push-based clients have no always-on connection, so the server can't reach in
to start or stop a backup directly. Instead it queues commands (see
:mod:`pibackup.server` ``/jobs/{id}/start`` and ``/stop``) and the agent polls
for them, runs or cancels the matching job, and reports the outcome back.

``poll_once`` drains the queue a single time (handy for a cron/timer tick or a
test); ``run_agent`` loops it on an interval for a long-lived ``pibackup agent``.
"""

from __future__ import annotations

import time
from typing import Optional

from pibackup.client import cancel
from pibackup.client.api import ApiError, ServerApi
from pibackup.common.config import load_config


def _act(api: ServerApi, command: dict) -> str:
    """Carry out one queued command, returning a short status line."""
    action = command["action"]
    job_name = command["job_name"]
    command_id = command["id"]

    if action == "stop":
        # Signal any in-flight run of this job to tear its rsync down. Backups
        # run in another process, so this is a cross-process filesystem flag.
        cancel.request_cancel(job_name)
        _report(api, command_id, "done", f"stop requested for {job_name}")
        return f"stop {job_name}"

    if action == "start":
        from pibackup.client import runner

        _report(api, command_id, "running", f"starting {job_name}")
        try:
            results = runner.run_jobs(job_name)
        except runner.RunError as exc:
            _report(api, command_id, "failed", str(exc))
            return f"start {job_name}: failed ({exc})"
        res = results[0] if results else None
        status = "done" if (res and res.ok) else "failed"
        message = res.message if res else "no result"
        _report(api, command_id, status, message)
        return f"start {job_name}: {status}"

    _report(api, command_id, "failed", f"unknown action: {action}")
    return f"unknown action: {action}"


def _report(api: ServerApi, command_id: int, status: str, message: str,
            run_id: Optional[int] = None) -> None:
    """Best-effort status report; never let a reporting hiccup stall the agent."""
    try:
        api.update_command(command_id, {"status": status, "message": message, "run_id": run_id})
    except ApiError:
        pass


def poll_once(api: Optional[ServerApi] = None, cfg=None) -> list[str]:
    """Fetch and act on every pending command once. Returns a line per command."""
    cfg = cfg or load_config()
    api = api or ServerApi(cfg.server_url)
    try:
        pending = api.pending_commands(cfg.client_name) or []
    except ApiError:
        return []
    return [_act(api, command) for command in pending]


def run_agent(interval: float = 5.0, on_action=None) -> None:
    """Poll the server forever, acting on queued start/stop commands."""
    cfg = load_config()
    api = ServerApi(cfg.server_url)
    while True:
        for line in poll_once(api, cfg):
            if on_action:
                on_action(line)
        time.sleep(interval)
