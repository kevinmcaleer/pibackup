"""Phase 7: enrollment (onboarding) + bare-metal restore script."""

import shutil
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pibackup.client.api import ServerApi
from pibackup.client.cli import app
from pibackup.common import manifest as manifest_mod
from pibackup.common.config import Config, config_file
from pibackup.common.store import Store
from pibackup.server.app import create_app

runner = CliRunner()


def _config(tmp_path, **extra) -> Config:
    return Config(
        data_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        db_path=tmp_path / "pibackup.db",
        repo_target=str(tmp_path / "repo"),
        client_name="testpi",
        **extra,
    )


# ---- token store ----
def test_enroll_token_one_time_use(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    token = store.create_enroll_token("pi1")
    assert store.consume_enroll_token("pi1", token) is True
    assert store.consume_enroll_token("pi1", token) is False  # already used
    assert store.consume_enroll_token("pi1", "wrong") is False


def test_record_enrollment_stores_key(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.record_enrollment("pi1", "pi1.local", "ssh-ed25519 AAAA...")
    client = store.get_client_by_name("pi1")
    assert client["public_key"] == "ssh-ed25519 AAAA..."
    assert client["hostname"] == "pi1.local"


# ---- /enroll endpoint ----
def test_enroll_endpoint(tmp_path):
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))
    store = Store(cfg.db_path)
    token = store.create_enroll_token("newpi")

    resp = client.post(
        "/enroll",
        json={"name": "newpi", "token": token, "hostname": "h", "ssh_public_key": "ssh-ed25519 KEY"},
    )
    assert resp.status_code == 200
    assert resp.json()["repo_target"] == str(cfg.repo_dir)
    assert store.get_client_by_name("newpi")["public_key"] == "ssh-ed25519 KEY"

    # token can't be reused
    assert client.post("/enroll", json={"name": "newpi", "token": token}).status_code == 403


def test_enroll_endpoint_bad_token(tmp_path):
    client = TestClient(create_app(_config(tmp_path)))
    assert client.post("/enroll", json={"name": "x", "token": "nope"}).status_code == 403


def test_enroll_appends_authorized_key(tmp_path):
    ak = tmp_path / "authorized_keys"
    cfg = _config(tmp_path, authorized_keys=str(ak))
    client = TestClient(create_app(cfg))
    token = Store(cfg.db_path).create_enroll_token("pi1")
    client.post("/enroll", json={"name": "pi1", "token": token, "ssh_public_key": "ssh-ed25519 KEYXYZ"})
    assert "ssh-ed25519 KEYXYZ" in ak.read_text()
    assert "pibackup:pi1" in ak.read_text()


# ---- ssh key generation ----
@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen not available")
def test_ensure_ssh_key(tmp_path):  # XDG isolated by conftest
    from pibackup.client import enroll

    priv, public = enroll.ensure_ssh_key()
    assert priv.exists()
    assert public.startswith("ssh-ed25519")
    # idempotent: second call returns the same key
    _, public2 = enroll.ensure_ssh_key()
    assert public2 == public


# ---- restore script ----
def test_render_restore_script():
    script = manifest_mod.render_restore_script(
        {
            "captured_at": "2026-05-26 00:00:00",
            "hostname": "kitchen-pi",
            "apt_manual": ["vim", "git"],
            "pip_freeze": ["requests==2.0"],
            "systemd_enabled": ["ssh.service"],
        }
    )
    assert script.startswith("#!/bin/sh")
    assert "hostnamectl set-hostname kitchen-pi" in script
    assert "apt-get install -y vim git" in script
    assert "pip install requests==2.0" in script
    assert "systemctl enable ssh.service" in script


def test_render_restore_script_skips_missing_sections():
    script = manifest_mod.render_restore_script({"hostname": "h"})
    assert "hostnamectl" in script
    assert "apt-get" not in script and "pip install" not in script


# ---- CLI ----
def test_cli_enroll_prints_bootstrap(tmp_path):  # XDG isolated => own db
    result = runner.invoke(app, ["enroll", "kitchen-pi"])
    assert result.exit_code == 0, result.output
    assert "pibackup connect" in result.output
    assert "--name kitchen-pi" in result.output


def test_cli_recover_generates_script(tmp_path):
    manifest_path = tmp_path / "m.json"
    manifest_mod.write(manifest_path)  # real capture
    out = tmp_path / "restore.sh"
    result = runner.invoke(app, ["recover", str(manifest_path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("#!/bin/sh")
    assert oct(out.stat().st_mode)[-3:] == "755"


# ---- end-to-end enrollment over a real socket ----
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    import uvicorn

    port = _free_port()
    cfg = _config(tmp_path)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    api = ServerApi(f"http://127.0.0.1:{port}")
    for _ in range(100):
        if api.reachable():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    yield cfg, port
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen not available")
def test_connect_end_to_end(tmp_path, live_server):
    from pibackup.client import enroll

    cfg, port = live_server
    url = f"http://127.0.0.1:{port}"
    token = Store(cfg.db_path).create_enroll_token("kitchen-pi")

    resp = enroll.connect_to_server(url, "kitchen-pi", token)
    assert resp["repo_target"] == str(cfg.repo_dir)

    # config.toml written and the server recorded our SSH key
    written = config_file().read_text()
    assert 'client_name = "kitchen-pi"' in written
    assert f'server_url = "{url}"' in written
    assert Store(cfg.db_path).get_client_by_name("kitchen-pi")["public_key"].startswith("ssh-ed25519")
