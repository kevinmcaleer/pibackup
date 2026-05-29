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
    message           TEXT,
    percent           REAL NOT NULL DEFAULT 0,       -- live progress, 0-100
    transferred       INTEGER NOT NULL DEFAULT 0,    -- bytes moved so far
    rate              TEXT,                          -- e.g. "1.23MB/s"
    eta               TEXT,                          -- e.g. "0:01:23"
    updated_at        TEXT                           -- last progress tick (stall check)
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

CREATE TABLE IF NOT EXISTS enroll_tokens (
    token       TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    used        INTEGER NOT NULL DEFAULT 0
);

-- Dashboard administrator. Single-row table (id is always 1); the password is
-- stored as a PBKDF2 hash + salt, never in plaintext. session_secret signs the
-- login cookie and is rotated on every password change.
CREATE TABLE IF NOT EXISTS admin (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    username       TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    salt           TEXT NOT NULL,
    iterations     INTEGER NOT NULL,
    session_secret TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Commands the server queues for a client's job. Push-based clients have no
-- daemon, so the server can't reach out directly: instead it records an intent
-- ('start' or 'stop') that the client picks up on its next poll and acts on,
-- updating the status as it goes (pending -> running -> done / failed).
CREATE TABLE IF NOT EXISTS commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,                     -- start|stop
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    message    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_client    ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_runs_job       ON runs(job_id);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_job  ON snapshots(job_id);
CREATE INDEX IF NOT EXISTS idx_commands_job   ON commands(job_id);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys on and row access by name."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# Columns added after the initial release; brought in on existing databases by
# _migrate() since CREATE TABLE IF NOT EXISTS won't alter an existing table.
_RUN_COLUMNS = {
    "percent": "REAL NOT NULL DEFAULT 0",
    "transferred": "INTEGER NOT NULL DEFAULT 0",
    "rate": "TEXT",
    "eta": "TEXT",
    "updated_at": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from an older `runs` table (idempotent)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    for col, decl in _RUN_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")


def init_db(db_path: Path | str) -> None:
    """Create the schema if it does not yet exist, and migrate older ones."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
