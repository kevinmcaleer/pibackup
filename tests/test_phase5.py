"""Phase 5: shared run orchestration + the Textual TUI."""

import asyncio

import pytest

from pibackup.client import runner
from pibackup.common.config import config_file


def _write_config(tmp_path, *, encrypted=False):
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
        + ("encrypted = true\n" if encrypted else "")
    )
    return src, repo


# ---- runner ----
def test_runner_runs_and_records(tmp_path):
    _write_config(tmp_path)
    results = runner.run_jobs()
    assert len(results) == 1 and results[0].ok

    from pibackup.common.config import load_config
    from pibackup.common.store import Store

    assert Store(load_config().db_path).list_runs()[0]["status"] == "success"


def test_runner_no_jobs_raises(tmp_path):
    cfg = config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('repo_target = "/tmp/x"\nserver_url = "http://127.0.0.1:9"\n')
    with pytest.raises(runner.RunError) as exc:
        runner.run_jobs()
    assert "no jobs" in str(exc.value)


def test_runner_unknown_job_raises(tmp_path):
    _write_config(tmp_path)
    with pytest.raises(runner.RunError):
        runner.run_jobs("nope")


# ---- TUI ----
def test_tui_mounts_with_columns(tmp_path):
    _write_config(tmp_path)
    from pibackup.client.tui import PibackupApp

    async def go():
        from textual.widgets import DataTable

        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.query_one("#jobs", DataTable).columns) == 4
            assert len(app.query_one("#snaps", DataTable).columns) == 5
            assert len(app.query_one("#runs", DataTable).columns) == 5
            # config has one job, no runs yet
            assert app.query_one("#jobs", DataTable).row_count == 1
            assert app.query_one("#runs", DataTable).row_count == 0
            await pilot.press("g")  # refresh action works
            await pilot.pause()

    asyncio.run(go())


def test_tui_shows_runs_after_backup(tmp_path):
    _write_config(tmp_path)
    runner.run_jobs()  # produce a run + snapshot in the local store
    from pibackup.client.tui import PibackupApp

    async def go():
        app = PibackupApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#runs").row_count == 1
            assert app.query_one("#snaps").row_count == 1

    asyncio.run(go())
