"""Server-rendered web dashboard.

Satisfies "the server shows the backup jobs, last run, status, retention". A
single auto-refreshing page summarising every Pi, its jobs (with last-run
status), and recent runs. Templates are autoescaped to keep job names / paths
from injecting markup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from jinja2 import Environment, DictLoader, select_autoescape

from pibackup import __version__
from pibackup.common.store import Store

# A running job whose progress hasn't updated in this long is treated as stalled
# (e.g. the client was killed mid-run), so it doesn't sit "running" forever.
STALL_AFTER_SECONDS = 120

_DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pibackup</title>
<style>
  :root { --ok:#1a7f37; --fail:#cf222e; --muted:#57606a; --bg:#f6f8fa; --card:#fff; --border:#d0d7de; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:#1f2328; }
  header { background:#24292f; color:#fff; padding:16px 24px; display:flex; align-items:baseline; gap:12px; }
  header h1 { font-size:18px; margin:0; }
  header .ver { color:#9da7b1; font-size:13px; }
  header .spacer { flex:1; }
  header form { margin:0; }
  header .logout { background:none; border:1px solid #444c56; color:#c9d1d9; font-size:13px;
                   padding:4px 10px; border-radius:6px; cursor:pointer; }
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
  .running-card { background:var(--card); border:1px solid var(--border); border-left:4px solid var(--ok); border-radius:8px; padding:14px 18px; margin-bottom:10px; }
  .running-card.stalled { border-left-color:var(--fail); }
  .run-head { display:flex; justify-content:space-between; align-items:baseline; font-size:14px; margin-bottom:8px; }
  .run-head .who { font-weight:600; }
  .run-head .meta { color:var(--muted); font-size:13px; }
  .bar { height:10px; background:#eaeef2; border-radius:6px; overflow:hidden; }
  .bar .fill { height:100%; background:var(--ok); transition:width .5s; }
  .stalled .bar .fill { background:var(--fail); }
  .act { display:inline; margin:0; }
  .btn { font:inherit; font-size:13px; font-weight:600; padding:4px 12px; border:1px solid var(--border); border-radius:6px; cursor:pointer; }
  .btn.start { background:var(--ok); color:#fff; border-color:var(--ok); }
  .btn.stop { background:var(--fail); color:#fff; border-color:var(--fail); }
  .btn.del { background:#fff; color:var(--fail); border-color:var(--border); }
  /* New-job form */
  details.newjob { margin:8px 0 18px; }
  details.newjob > summary { cursor:pointer; font-size:14px; font-weight:600; color:#0969da; list-style:none; }
  details.newjob > summary::-webkit-details-marker { display:none; }
  .jobform { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px 20px; margin-top:10px;
             display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px 16px; align-items:end; }
  .jobform .field { display:flex; flex-direction:column; gap:4px; }
  .jobform label { font-size:13px; color:var(--muted); }
  .jobform input, .jobform select { padding:7px 9px; border:1px solid var(--border); border-radius:6px; font:inherit; font-size:14px; }
  .jobform .checkbox { flex-direction:row; align-items:center; gap:6px; }
  .jobform .checkbox input { width:auto; }
  .jobform .submit { grid-column:1 / -1; }
  .btn.create { background:var(--ok); color:#fff; border-color:var(--ok); }
  .jobform .err { grid-column:1 / -1; color:var(--fail); font-size:13px; margin:0; }
</style>
</head>
<body>
<header><h1>&#128451;&#65039; pibackup</h1><span class="ver">v{{ version }}</span>
  <span class="spacer"></span>
  <form method="post" action="/logout"><button class="logout" type="submit">Sign out</button></form>
</header>
<main>
  <section id="running">
  {% if running %}
  <h2>Running now</h2>
  {% for r in running %}
    <div class="running-card {{ 'stalled' if r.stalled }}">
      <div class="run-head">
        <span class="who">{{ r.client }} / {{ r.job }}</span>
        <span class="meta">
          {% if r.stalled %}<span class="badge failure">stalled</span>{% endif %}
          {{ r.percent }}%{% if r.rate %} · {{ r.rate }}{% endif %}{% if r.eta and not r.stalled %} · ETA {{ r.eta }}{% endif %}
        </span>
      </div>
      <div class="bar"><div class="fill" style="width: {{ r.percent }}%"></div></div>
    </div>
  {% endfor %}
  {% endif %}
  </section>

  <div class="cards">
    <div class="card"><div class="n">{{ totals.clients }}</div><div class="l">Pis</div></div>
    <div class="card"><div class="n">{{ totals.jobs }}</div><div class="l">Jobs</div></div>
    <div class="card"><div class="n">{{ totals.snapshots }}</div><div class="l">Snapshots</div></div>
    <div class="card"><div class="n">{{ totals.bytes_h }}</div><div class="l">Transferred</div></div>
  </div>

  <h2>Backup jobs</h2>
  <details class="newjob"{% if newjob_error %} open{% endif %}>
    <summary>+ New job</summary>
    <form class="jobform" method="post" action="/jobs">
      {% if newjob_error %}<p class="err">{{ newjob_error }}</p>{% endif %}
      <div class="field">
        <label for="nj-client">Pi</label>
        <select id="nj-client" name="client" required>
          {% if not clients %}<option value="">No enrolled clients</option>{% endif %}
          {% for c in clients %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
      </div>
      <div class="field">
        <label for="nj-name">Job name</label>
        <input id="nj-name" name="name" placeholder="documents" required>
      </div>
      <div class="field">
        <label for="nj-sources">Sources (comma-separated)</label>
        <input id="nj-sources" name="sources" placeholder="/home, /etc" required>
      </div>
      <div class="field">
        <label for="nj-retention">Retention (days)</label>
        <input id="nj-retention" name="retention_days" type="number" min="1" value="30">
      </div>
      <div class="field">
        <label for="nj-bwlimit">Bandwidth limit (KB/s, 0 = none)</label>
        <input id="nj-bwlimit" name="bwlimit_kbps" type="number" min="0" value="0">
      </div>
      <div class="field checkbox">
        <input id="nj-encrypted" name="encrypted" type="checkbox" value="1">
        <label for="nj-encrypted">Encrypted</label>
      </div>
      <div class="submit"><button class="btn create" type="submit">Create job</button></div>
    </form>
  </details>
  {% if jobs %}
  <table>
    <thead><tr><th>Pi</th><th>Job</th><th>Sources</th><th>Retention</th><th>Encrypted</th><th>Last run</th><th>Status</th><th>Snapshots</th><th>Actions</th></tr></thead>
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
        <td>
          {% if j.running %}
          <form class="act" method="post" action="/jobs/{{ j.id }}/stop"><button class="btn stop" type="submit">Stop</button></form>
          {% else %}
          <form class="act" method="post" action="/jobs/{{ j.id }}/start"><button class="btn start" type="submit">Start</button></form>
          <form class="act" method="post" action="/jobs/{{ j.id }}/delete" onsubmit="return confirm('Delete job {{ j.name }} on {{ j.client }}?');"><button class="btn del" type="submit">Delete</button></form>
          {% endif %}
        </td>
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
        <td>{{ r.bytes_h }}</td>
        <td class="muted">{{ r.message }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No runs yet.</div>
  {% endif %}
</main>
<script>
// Update only the "Running now" section so an open New-job form is never wiped.
(function () {
  function card(r) {
    var div = document.createElement("div");
    div.className = "running-card" + (r.stalled ? " stalled" : "");
    var head = document.createElement("div");
    head.className = "run-head";
    var who = document.createElement("span");
    who.className = "who";
    who.textContent = (r.client || "") + " / " + (r.job || "");
    var meta = document.createElement("span");
    meta.className = "meta";
    if (r.stalled) {
      var badge = document.createElement("span");
      badge.className = "badge failure";
      badge.textContent = "stalled";
      meta.appendChild(badge);
      meta.appendChild(document.createTextNode(" "));
    }
    var txt = r.percent + "%";
    if (r.rate) { txt += " \\u00b7 " + r.rate; }
    if (r.eta && !r.stalled) { txt += " \\u00b7 ETA " + r.eta; }
    meta.appendChild(document.createTextNode(txt));
    head.appendChild(who);
    head.appendChild(meta);
    var bar = document.createElement("div");
    bar.className = "bar";
    var fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = r.percent + "%";
    bar.appendChild(fill);
    div.appendChild(head);
    div.appendChild(bar);
    return div;
  }
  function render(rows) {
    var sec = document.getElementById("running");
    if (!sec) { return; }
    sec.textContent = "";
    if (!rows || rows.length === 0) { return; }
    var h2 = document.createElement("h2");
    h2.textContent = "Running now";
    sec.appendChild(h2);
    rows.forEach(function (r) { sec.appendChild(card(r)); });
  }
  function poll() {
    fetch("/running", { headers: { "Accept": "application/json" } })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) { if (data) { render(data.running); } })
      .catch(function () {});
  }
  setInterval(poll, 3000);
})();
</script>
</body>
</html>
"""

_LOGIN = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pibackup — sign in</title>
<style>
  body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         background:#f6f8fa; color:#1f2328; display:flex; min-height:100vh;
         align-items:center; justify-content:center; }
  .login { background:#fff; border:1px solid #d0d7de; border-radius:8px;
           padding:28px 32px; width:320px; }
  .login h1 { font-size:18px; margin:0 0 4px; }
  .login .sub { color:#57606a; font-size:13px; margin:0 0 20px; }
  label { display:block; font-size:13px; color:#57606a; margin:12px 0 4px; }
  input { width:100%; padding:8px 10px; border:1px solid #d0d7de; border-radius:6px;
          font-size:14px; }
  button { width:100%; margin-top:18px; padding:9px; border:0; border-radius:6px;
           background:#24292f; color:#fff; font-size:14px; font-weight:600; cursor:pointer; }
  .err { color:#cf222e; font-size:13px; margin-top:14px; }
  .hint { color:#57606a; font-size:12px; margin-top:16px; }
  code { background:#eaeef2; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
  <form class="login" method="post" action="/login">
    <h1>&#128451;&#65039; pibackup</h1>
    <p class="sub">Sign in to the dashboard</p>
    {% if needs_setup %}
    <p class="hint">No administrator configured yet. On the server, set one with
      <code>pibackup admin set-password</code>.</p>
    {% else %}
    <label for="username">Username</label>
    <input id="username" name="username" autofocus autocomplete="username">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    {% endif %}
  </form>
</body>
</html>
"""

_env = Environment(
    loader=DictLoader({"dashboard.html": _DASHBOARD, "login.html": _LOGIN}),
    autoescape=select_autoescape(["html"]),
)


def render_login(error: str | None = None, needs_setup: bool = False) -> str:
    """The sign-in page; ``needs_setup`` flips it to a 'no admin yet' notice."""
    return _env.get_template("login.html").render(error=error, needs_setup=needs_setup)


def _age_seconds(ts: str | None) -> float | None:
    """Seconds since a stored UTC timestamp ('YYYY-MM-DD HH:MM:SS'), or None."""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _running_rows(runs: list[dict]) -> list[dict]:
    rows = []
    for run in runs:
        if run["status"] != "running":
            continue
        age = _age_seconds(run.get("updated_at") or run.get("started_at"))
        rows.append(
            {
                "client": run.get("client_name"),
                "job": run["job_name"],
                "percent": int(run.get("percent") or 0),
                "rate": run.get("rate"),
                "eta": run.get("eta"),
                "stalled": age is not None and age > STALL_AFTER_SECONDS,
            }
        )
    return rows


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def running_rows(store: Store) -> list[dict]:
    """Public helper: the current running runs shaped for the dashboard /
    the /running poll endpoint (client name + progress)."""
    return _running_rows(store.running_runs())


def render_dashboard(store: Store, newjob_error: str | None = None) -> str:
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

    # Jobs with an in-flight run can be stopped; the rest can be started.
    running_job_ids = {r["job_id"] for r in store.running_runs()}

    job_rows = []
    for job in jobs:
        lr = last_run.get(job["id"])
        job_rows.append(
            {
                "id": job["id"],
                "client": job["client_name"],
                "name": job["name"],
                "sources": json.loads(job["source_paths"]),
                "retention_days": job["retention_days"],
                "encrypted": bool(job["encrypted"]),
                "last_status": lr["status"] if lr else None,
                "last_started": lr["started_at"] if lr else None,
                "snapshots": snap_count.get(job["id"], 0),
                "running": job["id"] in running_job_ids,
            }
        )

    totals = {
        "clients": len(clients),
        "jobs": len(jobs),
        "snapshots": len(snaps),
        "bytes_h": _human_bytes(sum(s["size_bytes"] for s in snaps)),
    }

    running = _running_rows(store.running_runs())  # carries client name + progress
    # Per-run byte counts get humanised (B/KB/MB/GB/TB) with comma grouping.
    recent_runs = []
    for run in runs[:20]:
        row = dict(run)
        row["bytes_h"] = _human_bytes(run["bytes_transferred"] or 0)
        recent_runs.append(row)

    return _env.get_template("dashboard.html").render(
        version=__version__, jobs=job_rows, runs=recent_runs, totals=totals,
        running=running,
        clients=[c["name"] for c in clients], newjob_error=newjob_error,
    )
