"""SQLite schema and connection helpers.

The server owns this database: registered Pis (clients), their backup jobs,
each run's outcome, and the snapshots produced. Timestamps are stored as ISO-ish
UTC text via SQLite's ``datetime('now')`` for portability.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    hostname    TEXT,
    public_key  TEXT,
    enrolled_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    source_paths   TEXT NOT NULL,                 -- JSON array of paths
    schedule       TEXT,                          -- systemd timer / cron expression
    retention_days INTEGER NOT NULL DEFAULT 30,
    encrypted      INTEGER NOT NULL DEFAULT 0,    -- 0/1
    bwlimit_kbps   INTEGER,                        -- NULL = unlimited
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, name)
);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    started_at        TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',  -- running|success|failure
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    message           TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    size_bytes INTEGER NOT NULL DEFAULT 0,
    encrypted  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_client    ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_runs_job       ON runs(job_id);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_job  ON snapshots(job_id);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys on and row access by name."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the schema if it does not yet exist (idempotent)."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
