"""Phase 4: client-side age encryption + key management."""

import pytest
from typer.testing import CliRunner

pytest.importorskip("pyrage")
pytest.importorskip("zstandard")

from pibackup.client.cli import app  # noqa: E402
from pibackup.client.engine import BackupEngine  # noqa: E402
from pibackup.common.config import Config, JobSpec, config_file  # noqa: E402
from pibackup.common.crypto import (  # noqa: E402
    ArchiveCancelled,
    decrypt_archive,
    encrypt_archive,
    generate_keypair,
    recipient_from_secret,
)
from pibackup.common.transfer import CANCELLED_EXIT_CODE  # noqa: E402

runner = CliRunner()


def _config(tmp_path) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
    )


# ---- crypto round-trip ----
def test_encrypt_decrypt_roundtrip(tmp_path):
    src = tmp_path / "d"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.txt").write_text("bravo")

    secret, recipient = generate_keypair()
    archive = tmp_path / f"out{tmp_path.name}.tar.zst.age"
    size = encrypt_archive([str(src)], archive, recipient)
    assert archive.exists() and size > 0

    out = tmp_path / "restore"
    decrypt_archive(archive, out, [secret])
    rel = str(src).lstrip("/")
    assert (out / rel / "a.txt").read_text() == "alpha"
    assert (out / rel / "sub" / "b.txt").read_text() == "bravo"


def test_decrypt_with_wrong_key_fails(tmp_path):
    src = tmp_path / "d"
    src.mkdir()
    (src / "x").write_text("secret")
    _, recipient = generate_keypair()
    archive = tmp_path / "a.tar.zst.age"
    encrypt_archive([str(src)], archive, recipient)

    other_secret, _ = generate_keypair()
    with pytest.raises(Exception):
        decrypt_archive(archive, tmp_path / "nope", [other_secret])


def test_recipient_from_secret_matches():
    secret, recipient = generate_keypair()
    assert recipient_from_secret(secret) == recipient


# ---- key store ----
def test_key_store(tmp_path):  # XDG isolated by conftest
    from pibackup.client import keys

    recipient, path = keys.create_key("main")
    assert path.exists() and recipient.startswith("age1")
    assert oct(path.stat().st_mode)[-3:] == "600"

    listed = keys.list_keys()
    assert listed[0]["name"] == "main"
    assert listed[0]["recipient"] == recipient
    assert keys.export_key("main") == recipient
    assert keys.default_recipient() == recipient  # single key => default
    assert recipient_from_secret(keys.load_identities()[0]) == recipient

    assert keys.remove_key("main") is True
    assert keys.list_keys() == []
    assert keys.default_recipient() is None


# ---- engine encrypted job ----
def test_engine_encrypted_job(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.txt").write_text("classified")

    cfg = _config(tmp_path)
    secret, recipient = generate_keypair()
    res = BackupEngine(cfg).run_job(
        JobSpec(name="vault", sources=[str(src)], encrypted=True), recipient=recipient
    )
    assert res.ok, res.message

    job_dir = cfg.repo_dir / "testpi" / "vault"
    archives = [p for p in job_dir.iterdir() if p.name.endswith(".tar.zst.age")]
    assert len(archives) == 1
    assert (job_dir / "latest").is_symlink()

    out = tmp_path / "restore"
    decrypt_archive(archives[0], out, [secret])
    assert (out / str(src).lstrip("/") / "secret.txt").read_text() == "classified"


# ---- cancellation during archiving (issue #26) ----
def test_encrypt_archive_cancels_and_cleans_up(tmp_path):
    # A cancel requested before the first member aborts the build, removes the
    # partial archive, and surfaces ArchiveCancelled.
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha")

    _, recipient = generate_keypair()
    archive = tmp_path / "out.tar.zst.age"
    with pytest.raises(ArchiveCancelled):
        encrypt_archive([str(src)], archive, recipient, should_cancel=lambda: True)

    assert not archive.exists()  # no partial artifact left behind


def test_engine_encrypted_cancel_during_archive(tmp_path):
    # A stop set before archiving begins makes an encrypted job report the same
    # cancelled-failure outcome (exit 130) as the rsync path, with no archive.
    from pibackup.client import cancel

    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.txt").write_text("classified")

    cfg = _config(tmp_path)
    _, recipient = generate_keypair()
    cancel.request_cancel("vault")  # pre-set flag => abort at first check

    res = BackupEngine(cfg).run_job(
        JobSpec(name="vault", sources=[str(src)], encrypted=True),
        recipient=recipient,
        should_cancel=cancel.cancel_checker("vault"),
    )
    cancel.clear_cancel("vault")

    assert res.ok is False
    assert str(CANCELLED_EXIT_CODE) in res.message
    assert "cancelled on request" in res.message
    job_dir = cfg.repo_dir / "testpi" / "vault"
    assert not list(job_dir.glob("*.tar.zst.age"))  # nothing pushed


def test_engine_encrypted_without_recipient_fails(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x").write_text("y")
    res = BackupEngine(_config(tmp_path)).run_job(
        JobSpec(name="vault", sources=[str(src)], encrypted=True), recipient=None
    )
    assert res.ok is False
    assert "recipient" in res.message


# ---- CLI ----
def test_cli_key_create_and_ls(tmp_path):
    assert runner.invoke(app, ["key", "create", "mykey"]).exit_code == 0
    out = runner.invoke(app, ["key", "ls"]).output
    assert "mykey" in out and "age1" in out


def test_cli_encrypted_run_standalone(tmp_path):
    # Default key is auto-selected as the recipient.
    assert runner.invoke(app, ["key", "create"]).exit_code == 0

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("payload")
    repo = tmp_path / "repo"

    cfg_path = config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'repo_target = "{repo}"\n'
        'client_name = "testpi"\n'
        'server_url = "http://127.0.0.1:9"\n'  # unreachable => standalone
        "\n"
        "[[job]]\n"
        'name = "vault"\n'
        f'sources = ["{src}"]\n'
        "encrypted = true\n"
    )

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    archives = list((repo / "testpi" / "vault").glob("*.tar.zst.age"))
    assert len(archives) == 1
