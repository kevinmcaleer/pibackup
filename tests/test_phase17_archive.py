"""Issue #41: opt-in ``archive`` jobs pack each run into a single gzip'd tar
(``.tar.gz``) instead of an rsync ``--link-dest`` directory snapshot.

Covers the archive primitive (round-trip, cancellation cleanup, path-traversal
safe extract), the engine archive path (local repo), restore of an archive
snapshot, config/TOML parsing, store/db persistence of the ``archive`` flag,
and the dashboard form/checkbox + jobs-table mode column.
"""

import pytest
from fastapi.testclient import TestClient

from pibackup.client.engine import BackupEngine
from pibackup.client.restore import restore_snapshot
from pibackup.common.archive import (
    ARCHIVE_GZ_SUFFIX,
    ArchiveCancelled,
    extract_tar_gz,
    make_tar_gz,
)
from pibackup.common.auth import hash_password
from pibackup.common.config import Config, JobSpec, config_file
from pibackup.common.store import Store
from pibackup.common.transfer import CANCELLED_EXIT_CODE
from pibackup.server.app import create_app
from pibackup.server.dashboard import render_dashboard


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


# ---- archive primitive ----
def test_make_and_extract_tar_gz_roundtrip(tmp_path):
    src = tmp_path / "d"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.txt").write_text("bravo")

    out = tmp_path / "out.tar.gz"
    size = make_tar_gz([str(src)], out)
    assert out.exists() and size > 0

    dest = tmp_path / "restore"
    extract_tar_gz(out, dest)
    rel = str(src).lstrip("/")
    assert (dest / rel / "a.txt").read_text() == "alpha"
    assert (dest / rel / "sub" / "b.txt").read_text() == "bravo"


def test_make_tar_gz_compresses_repetitive_data(tmp_path):
    src = tmp_path / "d"
    src.mkdir()
    # Highly compressible payload: the gzip'd tar must be much smaller than raw.
    (src / "big.txt").write_text("x" * 100_000)
    out = tmp_path / "out.tar.gz"
    size = make_tar_gz([str(src)], out)
    assert size < 100_000  # gzip wins big on repetitive bytes


def test_make_tar_gz_cancel_cleans_up(tmp_path):
    src = tmp_path / "d"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    out = tmp_path / "out.tar.gz"
    with pytest.raises(ArchiveCancelled):
        make_tar_gz([str(src)], out, should_cancel=lambda: True)
    assert not out.exists()  # no partial artifact left behind


def test_extract_tar_gz_blocks_path_traversal(tmp_path):
    # A handcrafted tar with a '../' member must not escape the dest dir.
    import tarfile

    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("pwned")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")

    dest = tmp_path / "out"
    # filter="data" raises rather than writing outside dest.
    with pytest.raises(Exception):
        extract_tar_gz(evil, dest)
    assert not (tmp_path / "escape.txt").exists()


# ---- engine archive job ----
def test_engine_archive_job_local(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.txt").write_text("payload")

    cfg = _config(tmp_path)
    res = BackupEngine(cfg).run_job(
        JobSpec(name="docs", sources=[str(src)], archive=True)
    )
    assert res.ok, res.message

    job_dir = cfg.repo_dir / "testpi" / "docs"
    archives = [p for p in job_dir.iterdir() if p.name.endswith(ARCHIVE_GZ_SUFFIX)]
    assert len(archives) == 1
    assert (job_dir / "latest").is_symlink()


def test_engine_archive_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.txt").write_text("payload")

    cfg = _config(tmp_path)
    res = BackupEngine(cfg).run_job(
        JobSpec(name="docs", sources=[str(src)], archive=True), dry_run=True
    )
    assert res.ok
    assert "would archive" in res.message
    job_dir = cfg.repo_dir / "testpi" / "docs"
    assert not job_dir.exists() or not list(job_dir.glob("*.tar.gz"))


def test_engine_archive_cancel_during_build(tmp_path):
    from pibackup.client import cancel

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.txt").write_text("payload")

    cfg = _config(tmp_path)
    cancel.request_cancel("docs")
    res = BackupEngine(cfg).run_job(
        JobSpec(name="docs", sources=[str(src)], archive=True),
        should_cancel=cancel.cancel_checker("docs"),
    )
    cancel.clear_cancel("docs")

    assert res.ok is False
    assert str(CANCELLED_EXIT_CODE) in res.message
    job_dir = cfg.repo_dir / "testpi" / "docs"
    assert not list(job_dir.glob("*.tar.gz"))  # nothing pushed


# ---- restore an archive snapshot ----
def test_restore_archive_snapshot_local(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.txt").write_text("bravo")

    cfg = _config(tmp_path)
    res = BackupEngine(cfg).run_job(
        JobSpec(name="docs", sources=[str(src)], archive=True)
    )
    assert res.ok, res.message

    job_dir = cfg.repo_dir / "testpi" / "docs"
    archive = next(p for p in job_dir.iterdir() if p.name.endswith(ARCHIVE_GZ_SUFFIX))

    out = tmp_path / "restored"
    rr = restore_snapshot(cfg, {"path": str(archive), "encrypted": False}, str(out))
    assert rr.ok, rr.message
    rel = str(src).lstrip("/")
    assert (out / rel / "a.txt").read_text() == "alpha"
    assert (out / rel / "sub" / "b.txt").read_text() == "bravo"


# ---- config / TOML ----
def test_config_parses_archive_flag(tmp_path, monkeypatch):
    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        'repo_target = "/tmp/repo"\n'
        'client_name = "testpi"\n'
        "\n"
        "[[job]]\n"
        'name = "docs"\n'
        'sources = ["/home/pi/docs"]\n'
        "archive = true\n"
    )
    from pibackup.common.config import load_jobs

    jobs = load_jobs()
    assert len(jobs) == 1
    assert jobs[0].archive is True
    assert jobs[0].encrypted is False


def test_jobspec_archive_defaults_false():
    assert JobSpec(name="x", sources=["/a"]).archive is False


# ---- store / db persistence ----
def _store(tmp_path) -> Store:
    store = Store(tmp_path / "pibackup.db")
    return store


def test_store_persists_archive_flag(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("testpi", "key-fingerprint")
    jid = store.ensure_job(
        cid, JobSpec(name="docs", sources=["/home/pi/docs"], archive=True)
    )
    row = store.get_job(jid)
    assert bool(row["archive"]) is True

    # Round-trips back through the API shape too.
    listed = [j for j in store.list_jobs() if j["id"] == jid][0]
    assert bool(listed["archive"]) is True


def test_store_archive_defaults_zero(tmp_path):
    store = _store(tmp_path)
    cid = store.ensure_client("testpi", "fp")
    jid = store.ensure_job(cid, JobSpec(name="plain", sources=["/a"]))
    assert bool(store.get_job(jid)["archive"]) is False


# ---- dashboard form + table ----
def _admin_store(cfg, username="admin", password="secret") -> Store:
    store = Store(cfg.db_path)
    ph = hash_password(password)
    store.set_admin(username, ph.hash, ph.salt, ph.iterations, "sign-secret")
    return store


def test_dashboard_form_has_archive_checkbox(tmp_path):
    cfg = _config(tmp_path)
    store = _admin_store(cfg)
    html = render_dashboard(store)
    assert 'name="archive"' in html
    assert "Archive" in html


def test_dashboard_shows_archive_mode(tmp_path):
    cfg = _config(tmp_path)
    store = _admin_store(cfg)
    cid = store.ensure_client("testpi", "fp")
    store.ensure_job(cid, JobSpec(name="docs", sources=["/a"], archive=True))
    html = render_dashboard(store)
    assert "archive" in html  # mode column renders the archive label
