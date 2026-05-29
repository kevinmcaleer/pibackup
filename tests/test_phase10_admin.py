"""Issue #20: dashboard administrator login + CLI credential management."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.common.auth import (
    hash_password,
    sign_session,
    verify_password,
    verify_session,
)
from pibackup.common.config import Config, config_file
from pibackup.common.store import Store
from pibackup.server.app import SESSION_COOKIE, create_app

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


def _write_config(tmp_path) -> Config:
    """Point the CLI at an isolated server DB via config.toml (server mode)."""
    cfg = _config(tmp_path)
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(
        f'data_dir = "{cfg.data_dir}"\n'
        f'db_path = "{cfg.db_path}"\n'
        f'repo_dir = "{cfg.repo_dir}"\n'
    )
    return cfg


# ---- password hashing ----
def test_hash_password_roundtrip():
    ph = hash_password("hunter2")
    assert ph.hash != "hunter2"  # never stored in plaintext
    assert verify_password("hunter2", ph)
    assert not verify_password("wrong", ph)


def test_hash_password_uses_random_salt():
    a = hash_password("same")
    b = hash_password("same")
    assert a.salt != b.salt and a.hash != b.hash


# ---- session token signing ----
def test_session_token_roundtrip():
    token = sign_session("admin", "s3cr3t")
    assert verify_session(token, "s3cr3t") == "admin"


def test_session_token_rejects_wrong_secret_or_tamper():
    token = sign_session("admin", "s3cr3t")
    assert verify_session(token, "other") is None
    assert verify_session("garbage", "s3cr3t") is None
    assert verify_session(token + "x", "s3cr3t") is None


# ---- CLI credential management ----
def test_cli_set_and_reset_password(tmp_path):
    _write_config(tmp_path)
    cfg = _config(tmp_path)

    res = runner.invoke(app, ["admin", "set-password", "-u", "boss", "-p", "letmein"])
    assert res.exit_code == 0, res.output
    assert "Created administrator" in res.output

    store = Store(cfg.db_path)
    admin = store.get_admin()
    assert admin["username"] == "boss"
    assert admin["password_hash"] not in ("letmein", "")
    first_secret = admin["session_secret"]

    # Resetting rotates the session secret (invalidating old sessions).
    res = runner.invoke(app, ["admin", "reset", "-u", "boss", "-p", "newpass"])
    assert res.exit_code == 0, res.output
    assert "Reset administrator" in res.output
    admin2 = store.get_admin()
    assert admin2["session_secret"] != first_secret


def test_cli_show_reports_status(tmp_path):
    _write_config(tmp_path)
    assert runner.invoke(app, ["admin", "show"]).exit_code == 1  # none yet
    runner.invoke(app, ["admin", "set-password", "-u", "admin", "-p", "pw"])
    res = runner.invoke(app, ["admin", "show"])
    assert res.exit_code == 0 and "admin" in res.output


# ---- dashboard auth ----
def _client_with_admin(tmp_path, username="admin", password="secret"):
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    ph = hash_password(password)
    store.set_admin(username, ph.hash, ph.salt, ph.iterations, "sign-secret")
    return TestClient(create_app(cfg))


def test_dashboard_redirects_anonymous_to_login(tmp_path):
    client = _client_with_admin(tmp_path)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_success_grants_dashboard(tmp_path):
    client = _client_with_admin(tmp_path, "admin", "secret")
    resp = client.post(
        "/login", data={"username": "admin", "password": "secret"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert SESSION_COOKIE in resp.cookies
    # The cookie now unlocks the dashboard.
    page = client.get("/")
    assert page.status_code == 200
    assert "Backup jobs" in page.text


def test_login_failure_rejected(tmp_path):
    client = _client_with_admin(tmp_path, "admin", "secret")
    resp = client.post("/login", data={"username": "admin", "password": "nope"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text
    assert SESSION_COOKIE not in resp.cookies


def test_logout_clears_session(tmp_path):
    client = _client_with_admin(tmp_path)
    client.post("/login", data={"username": "admin", "password": "secret"})
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_dashboard_locked_when_no_admin_configured(tmp_path):
    """With no admin row the dashboard stays locked and prompts for setup."""
    client = TestClient(create_app(_config(tmp_path)))
    assert client.get("/", follow_redirects=False).status_code == 303
    login = client.get("/login")
    assert login.status_code == 200
    assert "No administrator configured" in login.text


def test_health_stays_public(tmp_path):
    """The API health check is not behind the dashboard login."""
    client = _client_with_admin(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
