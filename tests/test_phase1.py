"""Phase 1: rsync push, snapshot rotation, and run/snapshot recording."""

import time

from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.client.engine import BackupEngine
from pibackup.common.config import Config, JobSpec, config_file
from pibackup.common.store import Store
from pibackup.common.transfer import (
    Destination,
    build_rsync_command,
    classify_exit,
    parse_rsync_stats,
)

runner = CliRunner()

RSYNC_STATS = """
Number of files: 5 (reg: 4, dir: 1)
Number of created files: 4
Number of regular files transferred: 4
Total file size: 1,234 bytes
Total transferred file size: 1,234 bytes
Total bytes sent: 1,500
Total bytes received: 96
"""


def test_parse_rsync_stats():
    assert parse_rsync_stats(RSYNC_STATS) == (1500, 4)


def test_classify_exit():
    assert classify_exit(0) is True
    assert classify_exit(24) is True  # vanished file = benign
    assert classify_exit(23) is False
    assert classify_exit(255) is False


def test_destination_local():
    d = Destination("/srv/repo")
    assert d.is_remote is False
    assert d.rsync_target("pi/home") == "/srv/repo/pi/home"
    assert d.abspath("pi/home") == "/srv/repo/pi/home"


def test_destination_remote():
    d = Destination("pi@server:/srv/repo")
    assert d.is_remote is True
    assert d.host == "pi@server"
    assert d.base_path == "/srv/repo"
    assert d.rsync_target("pi/home") == "pi@server:/srv/repo/pi/home"
    assert d.abspath("pi/home") == "/srv/repo/pi/home"


def test_build_rsync_command_flags():
    cmd = build_rsync_command(["/a", "/b"], "host:/dest/", relative=True, dry_run=True)
    assert "-R" in cmd and "-n" in cmd
    assert cmd[-3:] == ["/a", "/b", "host:/dest/"]


def test_store_roundtrip(tmp_path):
    store = Store(tmp_path / "x.db")
    cid = store.ensure_client("testpi", "testpi.local")
    jid = store.ensure_job(cid, JobSpec(name="home", sources=["/home/pi"]))
    rid = store.start_run(jid)
    store.finish_run(rid, "success", 4096, "ok")
    store.add_snapshot(jid, rid, "/repo/testpi/home/2026", 4096, False)

    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["job_name"] == "home"
    assert store.list_snapshots()[0]["job_name"] == "home"


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


def test_engine_end_to_end(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello")
    (src / "sub" / "b.txt").write_text("world")

    cfg = _config(tmp_path)
    engine = BackupEngine(cfg)
    res = engine.run_job(JobSpec(name="home", sources=[str(src)]))

    assert res.ok, res.message
    assert res.snapshot and res.snapshot_path
    assert res.started_at and res.finished_at
    rel = str(src).lstrip("/")  # -R preserves the absolute source path
    job_dir = cfg.repo_dir / "testpi" / "home"
    snaps = [p for p in job_dir.iterdir() if p.name != "latest"]
    assert len(snaps) == 1
    assert (snaps[0] / rel / "a.txt").read_text() == "hello"
    assert (job_dir / "latest").is_symlink()


def test_engine_incremental_hardlinks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")

    cfg = _config(tmp_path)
    engine = BackupEngine(cfg)
    rel = str(src).lstrip("/")

    assert engine.run_job(JobSpec(name="home", sources=[str(src)])).ok
    time.sleep(1.1)  # snapshots are timestamped to the second
    assert engine.run_job(JobSpec(name="home", sources=[str(src)])).ok

    job_dir = cfg.repo_dir / "testpi" / "home"
    snaps = sorted(p for p in job_dir.iterdir() if p.name != "latest")
    assert len(snaps) == 2
    # Unchanged file is hardlinked across snapshots (--link-dest) => same inode.
    ino1 = (snaps[0] / rel / "a.txt").stat().st_ino
    ino2 = (snaps[1] / rel / "a.txt").stat().st_ino
    assert ino1 == ino2


def test_cli_run_integration(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("data")
    repo = tmp_path / "repo"

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'repo_target = "{repo}"\n'
        'client_name = "testpi"\n'
        'server_url = "http://127.0.0.1:9"\n'  # unreachable => deterministic local mode
        "\n"
        "[[job]]\n"
        'name = "home"\n'
        f'sources = ["{src}"]\n'
    )

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert (repo / "testpi" / "home" / "latest").is_symlink()

    # Standalone mode records the run locally (reporter wrote to the SQLite db).
    from pibackup.common.config import load_config

    runs = Store(load_config().db_path).list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "success"
