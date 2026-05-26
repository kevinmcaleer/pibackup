"""Phase 0 smoke tests: the command tree is wired up and the schema builds."""

from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.common.db import connect, init_db
from pibackup.common.transfer import build_rsync_command

runner = CliRunner()


def test_root_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "pibackup" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "pibackup" in result.output


def test_resource_subapps_exist():
    for resource in ["job", "snapshot", "client", "key"]:
        result = runner.invoke(app, [resource, "--help"])
        assert result.exit_code == 0, resource


def test_ls_commands_run():
    for args in (["job", "ls"], ["snapshot", "ls"], ["client", "ls"], ["key", "ls"], ["ps"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args


def test_ls_json_is_empty_array():
    result = runner.invoke(app, ["job", "ls", "--format", "json"])
    assert result.exit_code == 0
    assert result.output.strip() == "[]"


def test_db_init_creates_tables(tmp_path):
    db = tmp_path / "pibackup.db"
    init_db(db)
    conn = connect(db)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"clients", "jobs", "runs", "snapshots"} <= tables


def test_rsync_command_builder():
    cmd = build_rsync_command("/data", "pi@server:/repo", bwlimit_kbps=500, link_dest="/repo/prev")
    assert cmd[0] == "rsync"
    assert "-z" in cmd
    assert "--bwlimit=500" in cmd
    assert "--link-dest=/repo/prev" in cmd
    assert cmd[-2:] == ["/data", "pi@server:/repo"]
