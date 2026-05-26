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

app.add_typer(job_app, name="job")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(client_app, name="client")
app.add_typer(key_app, name="key")


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


def _jobs_data() -> tuple[list[dict], bool]:
    """Return (jobs-as-dicts, server_backed)."""
    from pibackup.common.config import load_config, load_jobs

    server = _server()
    if server:
        return server.get_jobs(load_config().client_name) or [], True
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
    fmt: OutputFormat = typer.Option(OutputFormat.table, "--format", "-f", help="Output format."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show names."),
):
    """List backup jobs."""
    jobs, server_backed = _jobs_data()
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
    retention_days: int = typer.Option(30, "--retention", help="Days to keep (0 = forever)."),
    bwlimit_kbps: int = typer.Option(0, "--bwlimit", help="Bandwidth cap KB/s (0 = unlimited)."),
    encrypt: bool = typer.Option(False, "--encrypt", help="Encrypt this job (Phase 4)."),
):
    """Create a backup job on the server."""
    from pibackup.client.api import ApiError
    from pibackup.common.config import load_config

    server = _server()
    if not server:
        console.print("[red]No server reachable.[/] Start one with [bold]pibackup serve[/] or set server_url.")
        raise typer.Exit(1)
    cfg = load_config()
    try:
        server.register_client(cfg.client_name, socket.gethostname())
        job = server.create_job(
            cfg.client_name,
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
    console.print(f"[green]Created job[/] [bold]{name}[/] (id {job['id']}) for {cfg.client_name}.")


@job_app.command("inspect")
def job_inspect(job: str = typer.Argument(..., help="Job id or name.")):
    """Show a job's full configuration."""
    jobs, _ = _jobs_data()
    match = next((j for j in jobs if j["name"] == job or str(j.get("id")) == job), None)
    if not match:
        console.print(f"[red]No such job:[/] {job}")
        raise typer.Exit(1)
    _detail("Job", match)


@job_app.command("rm")
def job_rm(job: str = typer.Argument(..., help="Job id or name.")):
    """Remove a backup job (server)."""
    from pibackup.client.api import ApiError
    from pibackup.common.config import load_config

    server = _server()
    if not server:
        console.print("[yellow]No server reachable.[/] In standalone mode, remove the [[job]] entry from config.toml.")
        raise typer.Exit(1)
    try:
        jobs = server.get_jobs(load_config().client_name) or []
        match = next((j for j in jobs if j["name"] == job or str(j["id"]) == job), None)
        if not match:
            console.print(f"[red]No such job:[/] {job}")
            raise typer.Exit(1)
        server.delete_job(match["id"])
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Removed job[/] {job}.")


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


# ===== top-level shortcuts =====
@app.command()
def run(
    job: Optional[str] = typer.Argument(None, help="Job to run; default = all jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would transfer without writing."),
):
    """Run a backup now."""
    from pibackup.client.api import ApiError
    from pibackup.client.engine import BackupEngine
    from pibackup.client.reporter import ApiReporter, LocalReporter
    from pibackup.common.config import config_file, load_config

    cfg = load_config()
    server = _server()
    try:
        reporter = ApiReporter(cfg, server) if server else LocalReporter(cfg)
    except ApiError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    specs = reporter.jobs()
    if not specs:
        if reporter.server_backed:
            console.print("[yellow]No jobs on the server for this client.[/] Create one: [bold]pibackup job create NAME -s /path[/]")
        else:
            console.print(f"[yellow]No jobs configured[/] in {config_file()}.")
            _config_hint()
        raise typer.Exit(1)
    if job:
        specs = [s for s in specs if s.name == job]
        if not specs:
            console.print(f"[red]No such job:[/] {job}")
            raise typer.Exit(1)

    try:
        engine = BackupEngine(cfg)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    # Resolve an encryption recipient if any selected job needs one.
    recipient = None
    if any(s.encrypted for s in specs):
        from pibackup.client import keys

        _require_crypto()
        recipient = cfg.recipient or keys.default_recipient()
        if not recipient:
            console.print(
                "[red]An encrypted job is selected but no recipient is set.[/] "
                "Run [bold]pibackup key create[/] or set recipient in config.toml."
            )
            raise typer.Exit(1)

    failures = 0
    for spec in specs:
        with console.status(f"Backing up [bold]{spec.name}[/] → {cfg.repo_target} …"):
            res = engine.run_job(spec, dry_run=dry_run, recipient=recipient)
        icon = "[green]✓[/]" if res.ok else "[red]✗[/]"
        verb = "would back up" if dry_run else ("backed up" if res.ok else "failed")
        suffix = f"  → {res.snapshot}" if res.snapshot else ""
        console.print(f"{icon} [bold]{spec.name}[/] {verb}: {res.message}{suffix}")
        if not dry_run:
            try:
                reporter.record(spec, res)
            except ApiError as exc:
                console.print(f"[yellow]  (failed to report run: {exc})[/]")
        if not res.ok:
            failures += 1
    if failures:
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
    snapshot: str = typer.Argument(..., help="Snapshot id to restore from."),
    path: Optional[str] = typer.Argument(None, help="Restore target (default: original location)."),
):
    """Restore files from a snapshot."""
    _planned("restore", 6)


@app.command()
def connect(url: str = typer.Argument(..., help="Server URL to enroll this Pi against.")):
    """Enroll this Pi against a server (using an enrollment token)."""
    _planned("connect", 7)


@app.command()
def enroll(name: str = typer.Argument(..., help="Name for the new Pi.")):
    """(Server) Mint a one-line bootstrap + token for a new Pi."""
    _planned("enroll", 7)


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
