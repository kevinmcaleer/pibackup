"""pibackup CLI — Docker-style command tree.

Grammar: ``pibackup <resource> <verb> [args]`` with top-level shortcuts for the
everyday actions (``run``, ``ps``, ``logs``, ``restore``, ``status``). One binary
serves both client operations and management; ``pibackup serve`` runs the daemon.

Reads and reporting go through the server API when one is reachable, and fall
back to local state (config.toml + a local SQLite db) for standalone use.
"""

from __future__ import annotations

import json
import socket
from enum import Enum
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from pibackup import __version__

console = Console()

app = typer.Typer(
    name="pibackup",
    help="Self-contained backup for Raspberry Pi.",
    no_args_is_help=True,
    add_completion=False,
)

# ----- resource sub-apps (the nouns) -----
job_app = typer.Typer(help="Manage backup jobs.", no_args_is_help=True)
snapshot_app = typer.Typer(help="Manage stored snapshots.", no_args_is_help=True)
client_app = typer.Typer(help="Manage registered Raspberry Pis (server view).", no_args_is_help=True)
key_app = typer.Typer(help="Manage age encryption keys.", no_args_is_help=True)
admin_app = typer.Typer(help="Manage the dashboard administrator (server).", no_args_is_help=True)

app.add_typer(job_app, name="job")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(client_app, name="client")
app.add_typer(key_app, name="key")
app.add_typer(admin_app, name="admin")


class OutputFormat(str, Enum):
    table = "table"
    json = "json"


# ----- shared helpers -----
def _planned(feature: str, phase: int) -> None:
    console.print(
        f"[yellow]'{feature}'[/] is planned for [bold]Phase {phase}[/] — not implemented yet."
    )
    raise typer.Exit()


def _config_hint() -> None:
    from pibackup.common.config import config_file

    console.print(
        f"[dim]Configure {config_file()} — for example:[/]\n"
        '[dim]  repo_target = "pi@server:/srv/pibackup/repo"\n'
        "\n"
        "  [[job]]\n"
        '  name = "home"\n'
        '  sources = ["/home/pi"]\n'
        "  retention_days = 30[/]"
    )


def _server():
    """Return a reachable ServerApi, or None to fall back to local state."""
    from pibackup.client.api import ServerApi
    from pibackup.common.config import load_config

    api = ServerApi(load_config().server_url)
    return api if api.reachable() else None


def _local_store():
    from pibackup.common.config import load_config
    from pibackup.common.store import Store

    return Store(load_config().db_path)


def _resolve_client(server, client: Optional[str]) -> str:
    """Resolve the target client for a job command.

    With no ``--client`` the local client (``cfg.client_name``) is used, exactly
    as before. When a client is named, it must already be enrolled on the server
    — otherwise we exit with a friendly hint rather than letting a raw 404 from
    ``/clients/<name>/jobs`` leak out. This is the shared spine for cross-client
    job management (issue #32).
    """
    from pibackup.client.api import ApiError
    from pibackup.common.config import load_config

    if not client:
        return load_config().client_name
    try:
        clients = server.list_clients() or []
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    if not any(c.get("name") == client for c in clients):
        console.print(
            f"[red]No such enrolled client:[/] {client}\n"
            "[dim]Run 'pibackup client ls' to see enrolled Pis.[/]"
        )
        raise typer.Exit(1)
    return client


def _jobs_data(client: Optional[str] = None) -> tuple[list[dict], bool]:
    """Return (jobs-as-dicts, server_backed).

    When ``client`` is given (and a server is reachable) the listing is for that
    enrolled client instead of the local one.
    """
    from pibackup.common.config import load_config, load_jobs

    server = _server()
    if server:
        target = _resolve_client(server, client)
        return server.get_jobs(target) or [], True
    return (
        [
            {
                "name": j.name,
                "sources": j.sources,
                "retention_days": j.retention_days,
                "bwlimit_kbps": j.bwlimit_kbps,
                "encrypted": j.encrypted,
            }
            for j in load_jobs()
        ],
        False,
    )


def _render(title: str, columns: list[str], rows: list[tuple], fmt: OutputFormat, quiet: bool) -> None:
    """Render a listing in table/json form, with a quiet (IDs-only) mode."""
    if quiet:
        for row in rows:
            typer.echo(str(row[0]))
        return
    if fmt is OutputFormat.json:
        typer.echo(json.dumps([dict(zip(columns, row)) for row in rows], default=str))
        return
    table = Table(title=title, title_justify="left", header_style="bold cyan")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(c) for c in row])
    console.print(table)
    if not rows:
        console.print(f"[dim]No {title.lower()} yet.[/]")


def _detail(title: str, data: dict) -> None:
    table = Table(title=title, show_header=False, title_justify="left")
    table.add_column("field", style="cyan")
    table.add_column("value")
    for key, value in data.items():
        if isinstance(value, list):
            value = ", ".join(str(x) for x in value)
        table.add_row(str(key), str(value))
    console.print(table)


# ===== job =====
@job_app.command("ls")
def job_ls(
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show names."),
):
    """List backup jobs."""
    jobs, server_backed = _jobs_data(client)
    rows = [
        (
            j["name"],
            ", ".join(j["sources"]),
            f"{j['retention_days']}d",
            j["bwlimit_kbps"] or "unlimited",
            "yes" if j["encrypted"] else "no",
        )
        for j in jobs
    ]
    _render("Jobs", ["NAME", "SOURCES", "RETENTION", "BWLIMIT", "ENCRYPTED"], rows, fmt, quiet)
    if not jobs and not quiet and fmt is OutputFormat.table:
        if server_backed:
            console.print("[dim]No jobs for this client. Create one: pibackup job create NAME -s /path[/]")
        else:
            _config_hint()


@job_app.command("create")
def job_create(
    name: str = typer.Argument(..., help="Job name."),
    sources: list[str] = typer.Option(..., "--source", "-s", help="Source path (repeatable)."),
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
    retention_days: int = typer.Option(30, "--retention", help="Days to keep (0 = forever)."),
    bwlimit_kbps: int = typer.Option(0, "--bwlimit", help="Bandwidth cap KB/s (0 = unlimited)."),
    encrypt: bool = typer.Option(False, "--encrypt", help="Encrypt this job (Phase 4)."),
):
    """Create a backup job on the server.

    With ``--client`` the job is created for another enrolled Pi (managed from
    the server); without it, for the local host.
    """
    from pibackup.client.api import ApiError
    from pibackup.common.config import load_config

    server = _server()
    if not server:
        console.print("[red]No server reachable.[/] Start one with [bold]pibackup serve[/] or set server_url.")
        raise typer.Exit(1)
    cfg = load_config()
    local = cfg.client_name
    try:
        # Targeting another host requires it to be enrolled already; only the
        # local client is auto-registered (don't silently create a remote Pi).
        target = _resolve_client(server, client)
        if target == local:
            server.register_client(local, socket.gethostname())
        job = server.create_job(
            target,
            {
                "name": name,
                "sources": list(sources),
                "retention_days": retention_days,
                "bwlimit_kbps": bwlimit_kbps,
                "encrypted": encrypt,
            },
        )
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Created job[/] [bold]{name}[/] (id {job['id']}) for {target}.")


@job_app.command("inspect")
def job_inspect(
    job: str = typer.Argument(..., help="Job id or name."),
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
):
    """Show a job's full configuration."""
    jobs, _ = _jobs_data(client)
    match = next((j for j in jobs if j["name"] == job or str(j.get("id")) == job), None)
    if not match:
        console.print(f"[red]No such job:[/] {job}")
        raise typer.Exit(1)
    _detail("Job", match)


@job_app.command("rm")
def job_rm(
    job: str = typer.Argument(..., help="Job id or name."),
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
):
    """Remove a backup job (server)."""
    from pibackup.client.api import ApiError

    server = _server()
    if not server:
        console.print("[yellow]No server reachable.[/] In standalone mode, remove the [[job]] entry from config.toml.")
        raise typer.Exit(1)
    try:
        target = _resolve_client(server, client)
        jobs = server.get_jobs(target) or []
        match = next((j for j in jobs if j["name"] == job or str(j["id"]) == job), None)
        if not match:
            console.print(f"[red]No such job:[/] {job}")
            raise typer.Exit(1)
        server.delete_job(match["id"])
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Removed job[/] {job}.")


@job_app.command("start")
def job_start(
    job: str = typer.Argument(..., help="Job id or name."),
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
):
    """Queue a backup run for a job (the client picks it up and runs it)."""
    from pibackup.client.api import ApiError

    server = _server()
    if not server:
        console.print(
            "[red]No server reachable.[/] Starting jobs is a server action — "
            "run a backup directly with [bold]pibackup run[/] in standalone mode."
        )
        raise typer.Exit(1)
    try:
        target = _resolve_client(server, client)
        jobs = server.get_jobs(target) or []
        match = next((j for j in jobs if j["name"] == job or str(j["id"]) == job), None)
        if not match:
            console.print(f"[red]No such job:[/] {job}")
            raise typer.Exit(1)
        cmd = server.start_job(match["id"])
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Queued start[/] for [bold]{match['name']}[/] (command {cmd['id']}).")


@job_app.command("stop")
def job_stop(
    job: str = typer.Argument(..., help="Job id or name."),
    client: Optional[str] = typer.Option(None, "--client", "-c", help="Target enrolled client (default: this host)."),
):
    """Queue a stop for a running backup (cancels it on the client)."""
    from pibackup.client.api import ApiError

    server = _server()
    if not server:
        console.print("[red]No server reachable.[/] Stopping jobs is a server action.")
        raise typer.Exit(1)
    try:
        target = _resolve_client(server, client)
        jobs = server.get_jobs(target) or []
        match = next((j for j in jobs if j["name"] == job or str(j["id"]) == job), None)
        if not match:
            console.print(f"[red]No such job:[/] {job}")
            raise typer.Exit(1)
        cmd = server.stop_job(match["id"])
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[yellow]Queued stop[/] for [bold]{match['name']}[/] (command {cmd['id']}).")


# ===== snapshot =====
@snapshot_app.command("ls")
def snapshot_ls(
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show IDs."),
):
    """List stored snapshots."""
    server = _server()
    snaps = (server.list_snapshots() or []) if server else _local_store().list_snapshots()
    rows = [
        (s["id"], s["job_name"], s["created_at"], s["size_bytes"], "yes" if s["encrypted"] else "no")
        for s in snaps
    ]
    _render("Snapshots", ["SNAPSHOT ID", "JOB", "CREATED", "SIZE", "ENCRYPTED"], rows, fmt, quiet)


@snapshot_app.command("inspect")
def snapshot_inspect(snapshot: int = typer.Argument(..., help="Snapshot id.")):
    """Show snapshot details."""
    server = _server()
    snaps = (server.list_snapshots() or []) if server else _local_store().list_snapshots()
    match = next((s for s in snaps if s["id"] == snapshot), None)
    if not match:
        console.print(f"[red]No such snapshot:[/] {snapshot}")
        raise typer.Exit(1)
    _detail("Snapshot", match)


@snapshot_app.command("rm")
def snapshot_rm(snapshot: int = typer.Argument(..., help="Snapshot id.")):
    """Remove a snapshot (deletes its files too)."""
    from pibackup.client.api import ApiError

    server = _server()
    if server:
        try:
            server.delete_snapshot(snapshot)
        except ApiError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
    else:
        from pibackup.common.config import load_config
        from pibackup.server import retention

        cfg = load_config()
        if not retention.delete_snapshot(_local_store(), snapshot, str(cfg.repo_dir)):
            console.print(f"[red]No such snapshot:[/] {snapshot}")
            raise typer.Exit(1)
    console.print(f"[green]Removed snapshot[/] {snapshot}.")


@snapshot_app.command("prune")
def snapshot_prune():
    """Prune snapshots past their retention window."""
    server = _server()
    if server:
        pruned = server.prune().get("pruned", 0)
    else:
        from pibackup.common.config import load_config
        from pibackup.server import retention

        pruned = len(retention.prune_all(_local_store(), str(load_config().repo_dir)))
    console.print(f"[green]Pruned[/] {pruned} snapshot(s) past retention.")


# ===== client =====
@client_app.command("ls")
def client_ls(
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show IDs."),
):
    """List registered Raspberry Pis."""
    server = _server()
    clients = server.list_clients() if server else _local_store().list_clients()
    rows = [
        (c["id"], c["name"], c.get("hostname") or "", c.get("last_seen") or "")
        for c in clients
    ]
    _render("Clients", ["CLIENT ID", "NAME", "HOSTNAME", "LAST SEEN"], rows, fmt, quiet)


@client_app.command("inspect")
def client_inspect(client: str = typer.Argument(..., help="Client id or name.")):
    """Show a registered Pi's details."""
    _planned("client inspect", 7)


@client_app.command("rm")
def client_rm(client: str = typer.Argument(..., help="Client id or name.")):
    """Remove a registered Pi."""
    _planned("client rm", 7)


# ===== key =====
def _require_crypto() -> None:
    from pibackup.common.crypto import crypto_available

    if not crypto_available():
        console.print(
            "[red]Encryption libraries missing.[/] Install with: "
            "[bold]pip install 'pibackup[crypto]'[/]"
        )
        raise typer.Exit(1)


@key_app.command("ls")
def key_ls(
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show names."),
):
    """List age encryption keys."""
    from pibackup.client import keys

    rows = [(k["name"], k["recipient"], k["created"]) for k in keys.list_keys()]
    _render("Keys", ["NAME", "RECIPIENT", "CREATED"], rows, fmt, quiet)


@key_app.command("create")
def key_create(name: str = typer.Argument("default", help="Key name.")):
    """Generate a new age key pair."""
    from pibackup.client import keys

    _require_crypto()
    try:
        recipient, path = keys.create_key(name)
    except FileExistsError:
        console.print(f"[red]Key already exists:[/] {name}")
        raise typer.Exit(1)
    console.print(f"[green]Created age key[/] [bold]{name}[/] → {path}")
    console.print(f"  public recipient: [cyan]{recipient}[/]")
    console.print(
        "[dim]It's used automatically if it's your only key, or set "
        f'recipient = "{recipient}" in config.toml.[/]'
    )


@key_app.command("export")
def key_export(name: str = typer.Argument("default", help="Key name.")):
    """Print an age public key (recipient)."""
    from pibackup.client import keys

    _require_crypto()
    try:
        typer.echo(keys.export_key(name))
    except FileNotFoundError:
        console.print(f"[red]No such key:[/] {name}")
        raise typer.Exit(1)


@key_app.command("rm")
def key_rm(key: str = typer.Argument(..., help="Key name.")):
    """Remove an age key."""
    from pibackup.client import keys

    if not keys.remove_key(key):
        console.print(f"[red]No such key:[/] {key}")
        raise typer.Exit(1)
    console.print(f"[green]Removed key[/] {key}.")


# ===== admin (dashboard login) =====
def _admin_store():
    """Open the server's local store for admin-credential management."""
    from pibackup.common.config import load_config
    from pibackup.common.store import Store

    return Store(load_config().db_path)


@admin_app.command("set-password")
def admin_set_password(
    username: str = typer.Option("admin", "--username", "-u", help="Administrator username."),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", help="New password (prompted securely if omitted)."
    ),
):
    """Set or reset the dashboard administrator's username and password."""
    import secrets

    from pibackup.common.auth import hash_password

    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if not password:
        console.print("[red]Password must not be empty.[/]")
        raise typer.Exit(1)

    ph = hash_password(password)
    store = _admin_store()
    existed = store.has_admin()
    # A fresh signing secret invalidates any existing dashboard sessions.
    store.set_admin(username, ph.hash, ph.salt, ph.iterations, secrets.token_urlsafe(32))
    verb = "Reset" if existed else "Created"
    console.print(f"[green]{verb} administrator[/] [bold]{username}[/] for the dashboard.")
    console.print("[dim]Existing dashboard sessions have been signed out.[/]")


@admin_app.command("reset")
def admin_reset(
    username: str = typer.Option("admin", "--username", "-u", help="Administrator username."),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", help="New password (prompted securely if omitted)."
    ),
):
    """Alias for `admin set-password` — reset the dashboard credentials."""
    admin_set_password(username=username, password=password)


@admin_app.command("show")
def admin_show():
    """Show whether a dashboard administrator is configured."""
    store = _admin_store()
    admin = store.get_admin()
    if admin is None:
        console.print("[yellow]No administrator configured.[/] Set one: pibackup admin set-password")
        raise typer.Exit(1)
    _detail("Administrator", {"username": admin["username"], "updated_at": admin["updated_at"]})


# ===== top-level shortcuts =====
@app.command()
def run(
    job: Optional[str] = typer.Argument(None, help="Job to run; default = all jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would transfer without writing."),
):
    """Run a backup now."""
    from pibackup.client import runner
    from pibackup.client.api import ApiError

    def show(name: str, res) -> None:
        icon = "[green]✓[/]" if res.ok else "[red]✗[/]"
        verb = "would back up" if dry_run else ("backed up" if res.ok else "failed")
        suffix = f"  → {res.snapshot}" if res.snapshot else ""
        console.print(f"{icon} [bold]{name}[/] {verb}: {res.message}{suffix}")

    try:
        with console.status("Backing up …"):
            results = runner.run_jobs(job, dry_run=dry_run, on_result=show)
    except runner.RunError as exc:
        console.print(f"[red]{exc}[/]")
        if not exc.server_backed and "no jobs" in str(exc):
            _config_hint()
        raise typer.Exit(1)
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    if any(not r.ok for r in results):
        raise typer.Exit(code=1)


@app.command()
def ps(
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show IDs."),
):
    """Show running / recent backup runs."""
    server = _server()
    runs = (server.list_runs() or []) if server else _local_store().list_runs()
    rows = [
        (r["id"], r["job_name"], r["started_at"], r["status"], r["bytes_transferred"])
        for r in runs
    ]
    _render("Runs", ["RUN ID", "JOB", "STARTED", "STATUS", "BYTES"], rows, fmt, quiet)


@app.command()
def logs(run: int = typer.Argument(..., help="Run id.")):
    """Show the logs for a backup run."""
    server = _server()
    runs = (server.list_runs() or []) if server else _local_store().list_runs()
    match = next((r for r in runs if r["id"] == run), None)
    if not match:
        console.print(f"[red]No such run:[/] {run}")
        raise typer.Exit(1)
    console.print(f"[bold]{match['job_name']}[/] run {match['id']} — [cyan]{match['status']}[/]")
    console.print(match.get("message") or "[dim](no message)[/]")


@app.command()
def restore(
    snapshot: int = typer.Argument(..., help="Snapshot id (see `pibackup snapshot ls`)."),
    target: Optional[str] = typer.Option(
        None, "--target", "-t",
        help="Restore into this directory (default: ./pibackup-restore-<id>; use / for original paths).",
    ),
):
    """Restore files from a snapshot."""
    from pibackup.client.restore import restore_snapshot
    from pibackup.common.config import load_config

    cfg = load_config()
    server = _server()
    snaps = (server.list_snapshots() or []) if server else _local_store().list_snapshots()
    snap = next((s for s in snaps if s["id"] == snapshot), None)
    if not snap:
        console.print(f"[red]No such snapshot:[/] {snapshot}")
        raise typer.Exit(1)
    if not cfg.repo_target:
        console.print("[red]No repo_target configured[/] — can't locate the snapshot.")
        raise typer.Exit(1)
    if snap.get("encrypted"):
        _require_crypto()

    target_dir = target or f"./pibackup-restore-{snapshot}"
    with console.status(f"Restoring snapshot {snapshot} → {target_dir} …"):
        res = restore_snapshot(cfg, snap, target_dir)
    if res.ok:
        console.print(f"[green]Restored[/] snapshot {snapshot} → {res.target} ({res.message})")
    else:
        console.print(f"[red]Restore failed:[/] {res.message}")
        raise typer.Exit(1)


@app.command()
def manifest(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write JSON here (default: stdout)."),
):
    """Capture a system manifest (hostname, packages, services, /etc bits)."""
    from pibackup.common import manifest as manifest_mod

    text = manifest_mod.to_json()
    if output:
        from pathlib import Path

        Path(output).write_text(text)
        console.print(f"[green]Wrote manifest[/] → {output}")
    else:
        typer.echo(text)


@app.command()
def recover(
    manifest_file: str = typer.Argument(..., help="Path to a captured manifest.json."),
    output: str = typer.Option("restore-system.sh", "--output", "-o", help="Where to write the restore script."),
    apply: bool = typer.Option(False, "--apply", help="Run the script now (needs root)."),
):
    """Generate (or run) a system-restore script from a manifest."""
    import json
    import os
    import subprocess
    from pathlib import Path

    from pibackup.common import manifest as manifest_mod

    try:
        data = json.loads(Path(manifest_file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Cannot read manifest:[/] {exc}")
        raise typer.Exit(1)

    out = Path(output)
    out.write_text(manifest_mod.render_restore_script(data))
    os.chmod(out, 0o755)
    console.print(f"[green]Wrote restore script[/] → {out}")
    console.print("[dim]Review it, then run as root — restore your files first with `pibackup restore`.[/]")

    if apply:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            console.print("[yellow]--apply needs root.[/]")
            raise typer.Exit(1)
        subprocess.run(["sh", str(out)], check=True)


@app.command()
def connect(
    url: str = typer.Argument(..., help="Server URL, e.g. http://server:8765"),
    token: str = typer.Option(..., "--token", help="Enrollment token from `pibackup enroll`."),
    name: Optional[str] = typer.Option(None, "--name", help="Client name (default: hostname)."),
):
    """Enroll this Pi against a server using an enrollment token."""
    from pibackup.client import enroll as enroll_mod
    from pibackup.client.api import ApiError

    client_name = name or socket.gethostname()
    try:
        resp = enroll_mod.connect_to_server(url, client_name, token)
    except ApiError as exc:
        console.print(f"[red]Enrollment failed:[/] {exc}")
        raise typer.Exit(1)
    except Exception as exc:  # ssh-keygen / fs errors
        console.print(f"[red]Enrollment error:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"[green]Enrolled[/] as [bold]{client_name}[/] with {url}.")
    if resp.get("repo_target"):
        console.print(f"  repo target: {resp['repo_target']}")
    jobs = resp.get("jobs") or []
    names = ", ".join(j["name"] for j in jobs)
    console.print(f"  jobs pulled: {len(jobs)}" + (f" ({names})" if names else ""))
    console.print("[dim]Your SSH key is registered; run `pibackup run` to back up.[/]")


@app.command()
def enroll(
    name: str = typer.Argument(..., help="Name for the new Pi."),
    url: Optional[str] = typer.Option(None, "--url", help="Server URL to advertise (default: server_url from config)."),
):
    """(Server) Mint a one-line bootstrap + token for a new Pi."""
    from pibackup.common.config import load_config
    from pibackup.common.store import Store

    cfg = load_config()
    store = Store(cfg.db_path)
    store.ensure_client(name, None)
    token = store.create_enroll_token(name)
    advertised = url or cfg.server_url
    install_url = "https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh"
    console.print(f"[green]Enrollment token for[/] [bold]{name}[/]: [cyan]{token}[/]")
    console.print("\nOn a fresh Pi (installs, enrolls, schedules — runs as root):")
    console.print(
        f"  [bold]curl -fsSL {install_url} | sudo sh -s -- "
        f"--server {advertised} --name {name} --token {token} --timer[/]"
    )
    console.print("\nOr, if pibackup is already installed:")
    console.print(f"  [bold]sudo pibackup connect {advertised} --name {name} --token {token}[/]")


@app.command()
def status():
    """Show the local pibackup configuration and where state lives."""
    from pibackup.common.config import load_config

    cfg = load_config()
    reachable = _server() is not None
    table = Table(title="pibackup status", show_header=False, title_justify="left")
    table.add_column("key", style="cyan")
    table.add_column("value")
    table.add_row("version", __version__)
    table.add_row("client name", cfg.client_name)
    table.add_row("data dir", str(cfg.data_dir))
    table.add_row("repo dir", str(cfg.repo_dir))
    present = "present" if cfg.db_path.exists() else "not created"
    table.add_row("database", f"{cfg.db_path} ({present})")
    table.add_row("repo target", cfg.repo_target or "[dim]unset[/]")
    table.add_row("server url", f"{cfg.server_url} ({'reachable' if reachable else 'unreachable'})")
    console.print(table)


@app.command()
def tui():
    """Launch the terminal UI (browse jobs/snapshots/runs, trigger backups)."""
    try:
        from pibackup.client.tui import PibackupApp
    except ImportError as exc:
        console.print(
            "[red]TUI needs Textual.[/] Install with: [bold]pip install 'pibackup[tui]'[/]"
        )
        raise typer.Exit(1) from exc
    PibackupApp().run()


@app.command()
def agent(
    once: bool = typer.Option(False, "--once", help="Process the queue once and exit."),
    interval: float = typer.Option(5.0, "--interval", help="Seconds between polls."),
):
    """Poll the server for queued start/stop commands and act on them."""
    from pibackup.client import agent as agent_mod
    from pibackup.client.api import ApiError

    server = _server()
    if not server:
        console.print("[red]No server reachable.[/] Set server_url or start one with [bold]pibackup serve[/].")
        raise typer.Exit(1)

    try:
        if once:
            for line in agent_mod.poll_once():
                console.print(f"  [cyan]{line}[/]")
            console.print("[green]Processed queued commands.[/]")
            return
        console.print(f"[green]Agent polling[/] every {interval:g}s — Ctrl-C to stop.")
        agent_mod.run_agent(interval, on_action=lambda line: console.print(f"  [cyan]{line}[/]"))
    except KeyboardInterrupt:
        console.print("\n[dim]Agent stopped.[/]")
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8765, help="Port for the API + dashboard."),
):
    """Run the pibackup server: REST API + web dashboard (the daemon)."""
    try:
        from pibackup.server.app import run_server
    except ImportError as exc:
        console.print(
            "[red]Server dependencies missing.[/] Install with: "
            "[bold]pip install 'pibackup[server]'[/]"
        )
        raise typer.Exit(code=1) from exc
    run_server(host=host, port=port)


@app.command()
def update(
    ref: str = typer.Option("main", "--ref", "--branch", help="Git ref/branch to upgrade to."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would happen, change nothing."),
    restart: bool = typer.Option(False, "--restart", help="Restart active pibackup systemd services afterwards."),
    run_migrations_only: bool = typer.Option(
        False, "--run-migrations-only", hidden=True,
        help="(internal) Skip the upgrade; just run database migrations. "
        "The freshly installed binary re-invokes itself with this flag.",
    ),
):
    """Upgrade pibackup to the latest version and migrate local state."""
    import subprocess

    from pibackup.client.update import detect_install
    from pibackup.common.config import load_config
    from pibackup.common.db import init_db

    # Re-exec entry point: the NEW binary runs this branch so the NEW migration
    # logic touches the database (the original process was still the OLD code).
    if run_migrations_only:
        cfg = load_config()
        init_db(cfg.db_path)
        console.print(f"[green]Migrations applied[/] to {cfg.db_path}.")
        return

    info = detect_install(ref=ref)
    if info.method == "unknown":
        console.print(
            "[yellow]Couldn't detect a pipx or venv install.[/] pibackup looks like "
            "it's running from a source checkout or system Python — upgrade it the "
            "way you installed it (e.g. [bold]git pull[/] or your package manager)."
        )
        raise typer.Exit(1)

    old_version = __version__
    extras = f" (extras: {', '.join(info.extras)})" if info.extras else ""
    console.print(f"[cyan]pibackup {old_version}[/] — upgrading via [bold]{info.method}[/]{extras} …")

    if dry_run:
        console.print("[dim]--dry-run, would run:[/]")
        console.print(f"  [bold]{' '.join(info.command)}[/]")
        console.print(f"  [bold]{info.new_binary} update --run-migrations-only[/]")
        if restart:
            console.print("  [bold]restart active pibackup systemd services[/]")
        return

    # 1. Upgrade the package (pipx reuses its recorded spec; venv re-uses ours).
    try:
        subprocess.run(info.command, check=True)
    except FileNotFoundError as exc:
        console.print(f"[red]Upgrade tool not found:[/] {exc}. Is pipx/pip on PATH?")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as exc:
        console.print(
            f"[red]Upgrade failed[/] (exit {exc.returncode}). "
            "On a permission error, re-run as the user that owns the install "
            "(e.g. with [bold]sudo[/] for a root/system install)."
        )
        raise typer.Exit(1)

    # 2. Migrate via the NEW binary so the freshly installed migration code runs.
    try:
        subprocess.run([str(info.new_binary), "update", "--run-migrations-only"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        console.print(
            f"[yellow]Upgrade succeeded but migrations didn't run automatically[/] ({exc}). "
            f"Run them by hand: [bold]{info.new_binary} update --run-migrations-only[/]"
        )

    # 3. New version (queried from the freshly installed binary).
    new_version = "unknown"
    try:
        out = subprocess.run(
            [str(info.new_binary), "--version"], check=True, capture_output=True, text=True
        )
        new_version = out.stdout.strip().split()[-1]
    except (FileNotFoundError, subprocess.CalledProcessError, IndexError):
        pass
    console.print(f"[green]Updated[/] pibackup {old_version} → {new_version}.")

    # 4. The running services are still on the old code until restarted.
    _handle_service_restart(restart)


# pibackup systemd units that, if active, are running stale code after an upgrade.
_SERVICE_UNITS = ("pibackup-server", "pibackup-agent", "pibackup-backup.timer")


def _active_services() -> list[str]:
    """Return the pibackup systemd units that are currently active (system + user)."""
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        return []
    active: list[str] = []
    for unit in _SERVICE_UNITS:
        for scope in ([], ["--user"]):
            res = subprocess.run(
                ["systemctl", *scope, "is-active", "--quiet", unit],
                capture_output=True,
            )
            if res.returncode == 0:
                active.append(unit if not scope else f"--user {unit}")
                break
    return active


def _handle_service_restart(restart: bool) -> None:
    """Restart active pibackup services, or print the commands to do so."""
    import subprocess

    active = _active_services()
    if not active:
        return
    if not restart:
        console.print(
            "[yellow]Active pibackup services are still running the old code.[/] "
            "Restart them to pick up the upgrade:"
        )
        for unit in active:
            console.print(f"  [bold]sudo systemctl restart {unit}[/]")
        console.print("[dim]Or re-run with [bold]pibackup update --restart[/].[/]")
        return
    for unit in active:
        scope = ["--user"] if unit.startswith("--user ") else []
        name = unit.replace("--user ", "")
        try:
            subprocess.run(["systemctl", *scope, "restart", name], check=True)
            console.print(f"[green]Restarted[/] {unit}.")
        except subprocess.CalledProcessError:
            console.print(f"[red]Couldn't restart[/] {unit} — try [bold]sudo systemctl restart {name}[/].")


# ----- root callback (version) -----
def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pibackup {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None, "--version", "-v", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
):
    """Self-contained backup for Raspberry Pi."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
