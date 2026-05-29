"""Test fixtures: isolate config/data dirs so tests never touch real state."""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("PIBACKUP_DATA_DIR", raising=False)
    # Default to an unreachable server so tests run in deterministic local mode
    # even on a box where a real pibackup server is listening on :8765. Tests
    # that need a server still set server_url in their own config.toml.
    monkeypatch.setenv("PIBACKUP_SERVER_URL", "http://127.0.0.1:9")
    yield
