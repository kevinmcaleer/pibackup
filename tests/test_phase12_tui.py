"""Issue #32 (TUI surface): cross-client job browsing + create/delete from the
Textual UI.

Standalone mode stays read-only and hides the client selector; server mode
exposes the dropdown, the new-job modal (``n``), and delete (``d``). We patch
``data``/``ServerApi`` so no live server is needed.
"""

import asyncio

import pytest

from pibackup.client import data
from pibackup.common.config import config_file


def _write_config(tmp_path):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "f.txt").write_text("data")
    repo = tmp_path / "repo"
    cfg = config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f'repo_target = "{repo}"\n'
        'client_name = "testpi"\n'
        'server_url = "http://127.0.0.1:9"\n'  # unreachable => standalone
        "\n"
        "[[job]]\n"
        'name = "home"\n'
        f'sources = ["{src}"]\n'
    )
    return src, repo


# ---- data helpers ----
def test_client_names_standalone_returns_local(tmp_path):
    _write_config(tmp_path)
    assert data.client_names() == ["testpi"]


def test_client_names_from_server(tmp_path, monkeypatch):
    _write_config(tmp_path)

    class FakeApi:
        def list_clients(self):
            return [{"name": "beta"}, {"name": "alpha"}]

    monkeypatch.setattr(data, "server", lambda: FakeApi())
    # sorted, and the local client_name is folded in if missing
    assert data.client_names() == ["alpha", "beta", "testpi"]


def test_overview_targets_selected_client(tmp_path, monkeypatch):
    _write_config(tmp_path)
    seen = {}

    class FakeApi:
        def get_jobs(self, name):
            seen["name"] = name
            return [{"name": "docs", "sources": ["/h"], "retention_days": 7,
                     "encrypted": False, "id": 5}]

        def list_runs(self):
            return []

        def list_snapshots(self):
            return []

    monkeypatch.setattr(data, "server", lambda: FakeApi())
    ov = data.overview("beta")
    assert seen["name"] == "beta"
    assert ov["server"] and ov["client"] == "beta"
    assert ov["jobs"][0]["name"] == "docs"


# ---- TUI: standalone hides selector, no server actions ----
def test_tui_standalone_hides_client_bar(tmp_path):
    _write_config(tmp_path)
    from pibackup.client.tui import PibackupApp

    async def go():
        from textual.containers import Grid

        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#clientbar", Grid).display is False
            # new/delete are no-ops without a server, surfaced in the status line
            await pilot.press("n")
            await pilot.pause()
            assert "needs a reachable server" in str(
                app.query_one("#status").render()
            )

    asyncio.run(go())


# ---- TUI: server mode exposes selector + create/delete ----
class _FakeServerApi:
    """In-memory stand-in for ServerApi used by both data and tui."""

    def __init__(self):
        self.jobs = {
            "testpi": [],
            "beta": [
                {"name": "docs", "sources": ["/home"], "retention_days": 30,
                 "encrypted": False, "id": 11}
            ],
        }
        self.created = []
        self.deleted = []

    # data.overview / client_names
    def list_clients(self):
        return [{"name": "beta"}, {"name": "testpi"}]

    def get_jobs(self, name):
        return list(self.jobs.get(name, []))

    def list_runs(self):
        return []

    def list_snapshots(self):
        return []

    # actions
    def create_job(self, client_name, spec):
        self.created.append((client_name, spec))
        self.jobs.setdefault(client_name, []).append({**spec, "id": 99})

    def delete_job(self, job_id):
        self.deleted.append(job_id)
        for jobs in self.jobs.values():
            jobs[:] = [j for j in jobs if j.get("id") != job_id]


def _patch_server(monkeypatch, fake):
    monkeypatch.setattr(data, "server", lambda: fake)
    import pibackup.client.api as api_mod

    monkeypatch.setattr(api_mod, "ServerApi", lambda *a, **k: fake)


def test_tui_server_shows_client_selector(tmp_path, monkeypatch):
    _write_config(tmp_path)
    fake = _FakeServerApi()
    _patch_server(monkeypatch, fake)
    from pibackup.client.tui import PibackupApp

    async def go():
        from textual.containers import Grid
        from textual.widgets import Select

        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#clientbar", Grid).display is True
            sel = app.query_one("#client-select", Select)
            values = [v for _, v in sel._options] if hasattr(sel, "_options") else []
            assert "beta" in values and "testpi" in values

    asyncio.run(go())


def test_tui_delete_calls_api(tmp_path, monkeypatch):
    _write_config(tmp_path)
    fake = _FakeServerApi()
    _patch_server(monkeypatch, fake)
    from pibackup.client.tui import PibackupApp

    async def go():
        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # switch to the client that actually has a job
            app._client = "beta"
            app.refresh_data()
            await pilot.pause()
            app.query_one("#jobs").move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert fake.deleted == [11]

    asyncio.run(go())


def test_tui_new_job_modal_creates(tmp_path, monkeypatch):
    _write_config(tmp_path)
    fake = _FakeServerApi()
    _patch_server(monkeypatch, fake)
    from pibackup.client.tui import NewJobScreen, PibackupApp

    async def go():
        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._client = "beta"
            app.refresh_data()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewJobScreen)
            from textual.widgets import Input

            screen = app.screen
            screen.query_one("#nj-name", Input).value = "photos"
            screen.query_one("#nj-sources", Input).value = "/pics, /media"
            screen.query_one("#nj-retention", Input).value = "10"
            await pilot.pause()
            await pilot.click("#nj-create")
            await pilot.pause()
            assert len(fake.created) == 1
            client_name, spec = fake.created[0]
            assert client_name == "beta"
            assert spec["name"] == "photos"
            assert spec["sources"] == ["/pics", "/media"]
            assert spec["retention_days"] == 10

    asyncio.run(go())
