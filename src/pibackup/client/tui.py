"""Textual TUI over the client engine: browse jobs/snapshots/runs and trigger
backups, watching progress live.

A thin view layer — all the work goes through :mod:`pibackup.client.data` and
:mod:`pibackup.client.runner`, the same modules the CLI uses.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from pibackup.client import data, runner


class PibackupApp(App):
    TITLE = "pibackup"
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        ("r", "run_all", "Run all"),
        ("g", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
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
        self.refresh_data()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def refresh_data(self) -> None:
        try:
            ov = data.overview()
        except Exception as exc:  # network / store errors shouldn't crash the UI
            self._status(f"error: {exc}")
            return

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
        self._status(
            f"{len(ov['jobs'])} jobs · {len(ov['snapshots'])} snapshots · source: {source}"
        )

    def action_refresh(self) -> None:
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
