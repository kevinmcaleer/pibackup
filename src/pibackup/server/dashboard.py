"""Server-rendered web dashboard.

Satisfies "the server shows the backup jobs, last run, status, retention". A
single auto-refreshing page summarising every Pi, its jobs (with last-run
status), and recent runs. Templates are autoescaped to keep job names / paths
from injecting markup.
"""

from __future__ import annotations

import json

from jinja2 import Environment, DictLoader, select_autoescape

from pibackup import __version__
from pibackup.common.store import Store

_DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>pibackup</title>
<style>
  :root { --ok:#1a7f37; --fail:#cf222e; --muted:#57606a; --bg:#f6f8fa; --card:#fff; --border:#d0d7de; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:#1f2328; }
  header { background:#24292f; color:#fff; padding:16px 24px; display:flex; align-items:baseline; gap:12px; }
  header h1 { font-size:18px; margin:0; }
  header .ver { color:#9da7b1; font-size:13px; }
  main { padding:24px; max-width:1100px; margin:0 auto; }
  .cards { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:8px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px 20px; min-width:120px; }
  .card .n { font-size:28px; font-weight:600; }
  .card .l { color:var(--muted); font-size:13px; }
  h2 { font-size:15px; margin:28px 0 8px; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
  th,td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--border); font-size:14px; }
  th { background:#f6f8fa; color:var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; padding:2px 8px; border-radius:12px; font-size:12px; font-weight:600; color:#fff; }
  .badge.success { background:var(--ok); }
  .badge.failure { background:var(--fail); }
  .badge.running, .badge.never { background:var(--muted); }
  .muted { color:var(--muted); }
  .empty { color:var(--muted); padding:16px; background:var(--card); border:1px solid var(--border); border-radius:8px; }
  code { background:#eaeef2; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<header><h1>&#128451;&#65039; pibackup</h1><span class="ver">v{{ version }}</span></header>
<main>
  <div class="cards">
    <div class="card"><div class="n">{{ totals.clients }}</div><div class="l">Pis</div></div>
    <div class="card"><div class="n">{{ totals.jobs }}</div><div class="l">Jobs</div></div>
    <div class="card"><div class="n">{{ totals.snapshots }}</div><div class="l">Snapshots</div></div>
    <div class="card"><div class="n">{{ totals.bytes_h }}</div><div class="l">Transferred</div></div>
  </div>

  <h2>Backup jobs</h2>
  {% if jobs %}
  <table>
    <thead><tr><th>Pi</th><th>Job</th><th>Sources</th><th>Retention</th><th>Encrypted</th><th>Last run</th><th>Status</th><th>Snapshots</th></tr></thead>
    <tbody>
    {% for j in jobs %}
      <tr>
        <td>{{ j.client }}</td>
        <td>{{ j.name }}</td>
        <td class="muted">{{ j.sources | join(', ') }}</td>
        <td>{{ j.retention_days }}d</td>
        <td>{{ 'yes' if j.encrypted else 'no' }}</td>
        <td class="muted">{{ j.last_started or '—' }}</td>
        <td><span class="badge {{ j.last_status or 'never' }}">{{ j.last_status or 'never' }}</span></td>
        <td>{{ j.snapshots }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No jobs yet. Create one with <code>pibackup job create</code>.</div>
  {% endif %}

  <h2>Recent runs</h2>
  {% if runs %}
  <table>
    <thead><tr><th>#</th><th>Job</th><th>Started</th><th>Status</th><th>Bytes</th><th>Message</th></tr></thead>
    <tbody>
    {% for r in runs %}
      <tr>
        <td>{{ r.id }}</td>
        <td>{{ r.job_name }}</td>
        <td class="muted">{{ r.started_at }}</td>
        <td><span class="badge {{ r.status }}">{{ r.status }}</span></td>
        <td>{{ r.bytes_transferred }}</td>
        <td class="muted">{{ r.message }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No runs yet.</div>
  {% endif %}
</main>
</body>
</html>
"""

_env = Environment(
    loader=DictLoader({"dashboard.html": _DASHBOARD}),
    autoescape=select_autoescape(["html"]),
)


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def render_dashboard(store: Store) -> str:
    clients = store.list_clients()
    jobs = store.list_jobs()
    runs = store.list_runs(200)
    snaps = store.list_snapshots()

    # Runs are newest-first, so the first seen per job is its latest run.
    last_run: dict[int, dict] = {}
    for run in runs:
        last_run.setdefault(run["job_id"], run)

    snap_count: dict[int, int] = {}
    for snap in snaps:
        snap_count[snap["job_id"]] = snap_count.get(snap["job_id"], 0) + 1

    job_rows = []
    for job in jobs:
        lr = last_run.get(job["id"])
        job_rows.append(
            {
                "client": job["client_name"],
                "name": job["name"],
                "sources": json.loads(job["source_paths"]),
                "retention_days": job["retention_days"],
                "encrypted": bool(job["encrypted"]),
                "last_status": lr["status"] if lr else None,
                "last_started": lr["started_at"] if lr else None,
                "snapshots": snap_count.get(job["id"], 0),
            }
        )

    totals = {
        "clients": len(clients),
        "jobs": len(jobs),
        "snapshots": len(snaps),
        "bytes_h": _human_bytes(sum(s["size_bytes"] for s in snaps)),
    }

    return _env.get_template("dashboard.html").render(
        version=__version__, jobs=job_rows, runs=runs[:20], totals=totals
    )
