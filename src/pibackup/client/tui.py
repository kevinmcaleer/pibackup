"""Textual TUI over the client engine: browse jobs/snapshots/runs and trigger
backups, watching progress live.

A thin view layer — all the work goes through :mod:`pibackup.client.data` and
:mod:`pibackup.client.runner`, the same modules the CLI uses. With a reachable
server you can also switch between enrolled clients and create/delete their jobs
(issue #32); standalone mode stays read-only over this Pi's own config.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from pibackup.client import data, runner


class NewJobScreen(ModalScreen):
    """A small form for creating a job on the selected client (server mode)."""

    CSS = """
    NewJobScreen { align: center middle; }
    #dialog { grid-size: 2; grid-rows: auto auto auto auto auto auto auto;
              padding: 1 2; width: 64; height: auto; border: thick $primary;
              background: $surface; }
    #dialog Label { width: 100%; padding: 1 0 0 0; }
    #dialog Input { width: 100%; column-span: 2; }
    #title { column-span: 2; text-style: bold; padding: 0 0 1 0; }
    #buttons { column-span: 2; align: right middle; height: auto; padding: 1 0 0 0; }
    #nj-encrypted { column-span: 2; }
    """

    def __init__(self, client_name: str) -> None:
        super().__init__()
        self._client = client_name

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Static(f"New job for {self._client}", id="title")
            yield Label("Name")
            yield Input(placeholder="documents", id="nj-name")
            yield Label("Sources (comma-separated)")
            yield Input(placeholder="/home, /etc", id="nj-sources")
            yield Label("Retention (days)")
            yield Input(value="30", id="nj-retention")
            yield Label("Bandwidth limit (KB/s)")
            yield Input(value="0", id="nj-bwlimit")
            yield Checkbox("Encrypted", id="nj-encrypted")
            with Grid(id="buttons"):
                yield Button("Create", variant="success", id="nj-create")
                yield Button("Cancel", id="nj-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nj-cancel":
            self.dismiss(None)
            return
        name = self.query_one("#nj-name", Input).value.strip()
        sources = [
            s.strip()
            for s in self.query_one("#nj-sources", Input).value.split(",")
            if s.strip()
        ]
        if not name or not sources:
            self.query_one("#title", Static).update(
                "Name and at least one source are required"
            )
            return

        def _int(widget_id: str, default: int) -> int:
            try:
                return int(self.query_one(widget_id, Input).value)
            except (ValueError, TypeError):
                return default

        self.dismiss(
            {
                "name": name,
                "sources": sources,
                "retention_days": _int("#nj-retention", 30),
                "bwlimit_kbps": _int("#nj-bwlimit", 0),
                "encrypted": self.query_one("#nj-encrypted", Checkbox).value,
            }
        )


class PibackupApp(App):
    TITLE = "pibackup"
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    #clientbar { height: 3; padding: 0 1; }
    #clientbar Label { padding: 1 1 0 0; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        ("r", "run_all", "Run all"),
        ("s", "start_job", "Start job"),
        ("x", "stop_job", "Stop job"),
        ("n", "new_job", "New job"),
        ("d", "delete_job", "Delete job"),
        ("g", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._client: str | None = None  # selected client (server mode)

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid(id="clientbar"):
            yield Label("Client:")
            yield Select([], id="client-select", allow_blank=True)
        with TabbedContent(initial="jobs-tab"):
            with TabPane("Jobs", id="jobs-tab"):
                yield DataTable(id="jobs", cursor_type="row")
            with TabPane("Snapshots", id="snaps-tab"):
                yield DataTable(id="snaps", cursor_type="row")
            with TabPane("Runs", id="runs-tab"):
                yield DataTable(id="runs", cursor_type="row")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jobs", DataTable).add_columns("Name", "Sources", "Retention", "Encrypted")
        self.query_one("#snaps", DataTable).add_columns("ID", "Job", "Created", "Size", "Encrypted")
        self.query_one("#runs", DataTable).add_columns("ID", "Job", "Started", "Status", "Bytes")
        self._populate_clients()
        self.refresh_data()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _populate_clients(self) -> None:
        """Fill the client dropdown; hide it entirely in standalone mode."""
        select = self.query_one("#client-select", Select)
        try:
            names = data.client_names()
        except Exception:
            names = []
        bar = self.query_one("#clientbar", Grid)
        # Only meaningful with a server + more than the local client to choose from.
        if len(names) <= 1:
            bar.display = False
            self._client = names[0] if names else None
            return
        bar.display = True
        select.set_options((n, n) for n in names)
        if self._client not in names:
            self._client = names[0]
        select.value = self._client

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "client-select" or event.value is Select.BLANK:
            return
        if event.value != self._client:
            self._client = str(event.value)
            self.refresh_data()

    def refresh_data(self) -> None:
        try:
            ov = data.overview(self._client)
        except Exception as exc:  # network / store errors shouldn't crash the UI
            self._status(f"error: {exc}")
            return

        # Keep the last overview so start/stop can map the selected row to a job.
        self._overview = ov
        jobs = self.query_one("#jobs", DataTable)
        jobs.clear()
        for j in ov["jobs"]:
            jobs.add_row(
                j["name"], ", ".join(j["sources"]), f"{j['retention_days']}d",
                "yes" if j["encrypted"] else "no",
            )

        snaps = self.query_one("#snaps", DataTable)
        snaps.clear()
        for s in ov["snapshots"]:
            snaps.add_row(
                str(s["id"]), s["job_name"], s["created_at"], str(s["size_bytes"]),
                "yes" if s["encrypted"] else "no",
            )

        runs = self.query_one("#runs", DataTable)
        runs.clear()
        for r in ov["runs"]:
            runs.add_row(str(r["id"]), r["job_name"], r["started_at"], r["status"], str(r["bytes_transferred"]))

        source = "server" if ov["server"] else "local"
        who = ov.get("client") or "—"
        self._status(
            f"{who} · {len(ov['jobs'])} jobs · {len(ov['snapshots'])} snapshots · source: {source}"
        )

    def action_refresh(self) -> None:
        self._populate_clients()
        self.refresh_data()

    def _selected_job(self) -> dict | None:
        """The job under the cursor in the Jobs table, or None."""
        ov = getattr(self, "_overview", None)
        if not ov:
            return None
        table = self.query_one("#jobs", DataTable)
        row = table.cursor_row
        jobs = ov["jobs"]
        if row is None or row < 0 or row >= len(jobs):
            return None
        return jobs[row]

    def _api(self):
        """A ServerApi for the current server, or None when standalone."""
        ov = getattr(self, "_overview", None)
        if not ov or not ov.get("server"):
            return None
        from pibackup.client.api import ServerApi
        from pibackup.common.config import load_config

        return ServerApi(load_config().server_url)

    def _queue(self, action: str) -> None:
        """Queue a start/stop command for the selected job (server only)."""
        api = self._api()
        if api is None:
            self._status("start/stop needs a reachable server")
            return
        job = self._selected_job()
        if not job:
            self._status("select a job first")
            return
        from pibackup.client.api import ApiError

        try:
            if action == "start":
                api.start_job(job["id"])
            else:
                api.stop_job(job["id"])
        except ApiError as exc:
            self._status(f"{action} failed: {exc}")
            return
        self._status(f"queued {action} for {job['name']}")

    def action_start_job(self) -> None:
        self._queue("start")

    def action_stop_job(self) -> None:
        self._queue("stop")

    def action_new_job(self) -> None:
        """Open the new-job modal for the selected client (server mode only)."""
        api = self._api()
        if api is None:
            self._status("creating jobs needs a reachable server")
            return
        target = self._client or (self._overview or {}).get("client")
        if not target:
            self._status("no client selected")
            return

        def _on_close(spec: dict | None) -> None:
            if not spec:
                return
            self._create_job(target, spec)

        self.push_screen(NewJobScreen(target), _on_close)

    def _create_job(self, client_name: str, spec: dict) -> None:
        api = self._api()
        if api is None:
            self._status("creating jobs needs a reachable server")
            return
        from pibackup.client.api import ApiError

        try:
            api.create_job(client_name, spec)
        except ApiError as exc:
            self._status(f"create failed: {exc}")
            return
        self._status(f"created {spec['name']} for {client_name}")
        self.refresh_data()

    def action_delete_job(self) -> None:
        """Delete the selected job from the server."""
        api = self._api()
        if api is None:
            self._status("deleting jobs needs a reachable server")
            return
        job = self._selected_job()
        if not job:
            self._status("select a job first")
            return
        if "id" not in job:
            self._status("cannot delete a local-only job")
            return
        from pibackup.client.api import ApiError

        try:
            api.delete_job(job["id"])
        except ApiError as exc:
            self._status(f"delete failed: {exc}")
            return
        self._status(f"deleted {job['name']}")
        self.refresh_data()

    def action_run_all(self) -> None:
        self._status("running backup …")
        self._run_worker()

    @work(thread=True, exclusive=True)
    def _run_worker(self) -> None:
        def on_result(name: str, res) -> None:
            mark = "✓" if res.ok else "✗"
            self.call_from_thread(self._status, f"{mark} {name}: {res.message}")

        try:
            results = runner.run_jobs(on_result=on_result)
        except Exception as exc:
            self.call_from_thread(self._status, f"run failed: {exc}")
            return

        ok = sum(1 for r in results if r.ok)
        self.call_from_thread(self._status, f"done: {ok}/{len(results)} ok")
        self.call_from_thread(self.refresh_data)


def main() -> None:
    PibackupApp().run()
