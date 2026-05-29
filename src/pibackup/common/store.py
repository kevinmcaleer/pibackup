"""Database access shared by client and server.

The client uses this to record its own runs/snapshots locally (Phase 1); the
server will use the same store for job config and reporting (Phase 2).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Optional

from pibackup.common.config import JobSpec
from pibackup.common.db import connect, init_db


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    # -- reads (safe before the db exists) --
    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self.db_path.exists():
            return []
        conn = connect(self.db_path)
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def list_clients(self) -> list[dict]:
        return self._query("SELECT * FROM clients ORDER BY name")

    def get_client_by_name(self, name: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM clients WHERE name = ?", (name,))
        return rows[0] if rows else None

    def get_job(self, job_id: int) -> Optional[dict]:
        rows = self._query(
            """SELECT j.*, c.name AS client_name
               FROM jobs j JOIN clients c ON c.id = j.client_id
               WHERE j.id = ?""",
            (job_id,),
        )
        return rows[0] if rows else None

    def jobs_for_client(self, client_name: str) -> list[dict]:
        return self._query(
            """SELECT j.*, c.name AS client_name
               FROM jobs j JOIN clients c ON c.id = j.client_id
               WHERE c.name = ? ORDER BY j.name""",
            (client_name,),
        )

    def get_run(self, run_id: int) -> Optional[dict]:
        rows = self._query(
            """SELECT r.*, j.name AS job_name
               FROM runs r JOIN jobs j ON j.id = r.job_id WHERE r.id = ?""",
            (run_id,),
        )
        return rows[0] if rows else None

    def get_snapshot(self, snap_id: int) -> Optional[dict]:
        rows = self._query(
            """SELECT s.*, j.name AS job_name
               FROM snapshots s JOIN jobs j ON j.id = s.job_id WHERE s.id = ?""",
            (snap_id,),
        )
        return rows[0] if rows else None

    def list_expired_snapshots(self) -> list[dict]:
        """Snapshots older than their job's retention window (0 = keep forever)."""
        return self._query(
            """SELECT s.*, j.retention_days, j.name AS job_name
               FROM snapshots s JOIN jobs j ON j.id = s.job_id
               WHERE j.retention_days > 0
                 AND datetime(s.created_at)
                     < datetime('now', '-' || j.retention_days || ' days')"""
        )

    def list_jobs(self) -> list[dict]:
        return self._query(
            """SELECT j.*, c.name AS client_name
               FROM jobs j JOIN clients c ON c.id = j.client_id
               ORDER BY c.name, j.name"""
        )

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT r.*, j.name AS job_name
               FROM runs r JOIN jobs j ON j.id = r.job_id
               ORDER BY r.started_at DESC, r.id DESC LIMIT ?""",
            (limit,),
        )

    def list_snapshots(self) -> list[dict]:
        return self._query(
            """SELECT s.*, j.name AS job_name
               FROM snapshots s JOIN jobs j ON j.id = s.job_id
               ORDER BY s.created_at DESC, s.id DESC"""
        )

    def get_command(self, command_id: int) -> Optional[dict]:
        rows = self._query(
            """SELECT cmd.*, j.name AS job_name, c.name AS client_name
               FROM commands cmd
               JOIN jobs j ON j.id = cmd.job_id
               JOIN clients c ON c.id = j.client_id
               WHERE cmd.id = ?""",
            (command_id,),
        )
        return rows[0] if rows else None

    def list_commands(self, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT cmd.*, j.name AS job_name, c.name AS client_name
               FROM commands cmd
               JOIN jobs j ON j.id = cmd.job_id
               JOIN clients c ON c.id = j.client_id
               ORDER BY cmd.created_at DESC, cmd.id DESC LIMIT ?""",
            (limit,),
        )

    def pending_commands_for_client(self, client_name: str) -> list[dict]:
        """Queued (still 'pending') commands for a client, oldest first so the
        client acts on them in the order they were issued."""
        return self._query(
            """SELECT cmd.*, j.name AS job_name, c.name AS client_name
               FROM commands cmd
               JOIN jobs j ON j.id = cmd.job_id
               JOIN clients c ON c.id = j.client_id
               WHERE c.name = ? AND cmd.status = 'pending'
               ORDER BY cmd.created_at, cmd.id""",
            (client_name,),
        )

    def running_runs(self) -> list[dict]:
        """In-flight runs with their live progress, newest first."""
        return self._query(
            """SELECT r.*, j.name AS job_name, c.name AS client_name
               FROM runs r
               JOIN jobs j ON j.id = r.job_id
               JOIN clients c ON c.id = j.client_id
               WHERE r.status = 'running'
               ORDER BY r.started_at DESC, r.id DESC"""
        )

    # -- writes --
    def ensure_schema(self) -> None:
        init_db(self.db_path)

    def ensure_client(self, name: str, hostname: Optional[str] = None) -> int:
        self.ensure_schema()
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO clients (name, hostname) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET hostname=excluded.hostname, "
                "last_seen=datetime('now')",
                (name, hostname),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
            return int(row["id"])
        finally:
            conn.close()

    # -- enrollment --
    def create_enroll_token(self, client_name: str) -> str:
        self.ensure_schema()
        token = secrets.token_urlsafe(24)
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO enroll_tokens (token, client_name) VALUES (?, ?)",
                (token, client_name),
            )
            conn.commit()
            return token
        finally:
            conn.close()

    def consume_enroll_token(self, client_name: str, token: str) -> bool:
        """Validate a one-time token for a client and mark it used."""
        if not self.db_path.exists():
            return False
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT token FROM enroll_tokens WHERE token = ? AND client_name = ? AND used = 0",
                (token, client_name),
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE enroll_tokens SET used = 1 WHERE token = ?", (token,))
            conn.commit()
            return True
        finally:
            conn.close()

    def record_enrollment(self, name: str, hostname: Optional[str], public_key: Optional[str]) -> int:
        self.ensure_schema()
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO clients (name, hostname, public_key, enrolled_at, last_seen)
                   VALUES (?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT(name) DO UPDATE SET
                     hostname=excluded.hostname, public_key=excluded.public_key,
                     enrolled_at=datetime('now'), last_seen=datetime('now')""",
                (name, hostname, public_key),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
            return int(row["id"])
        finally:
            conn.close()

    # -- admin (dashboard login) --
    def get_admin(self) -> Optional[dict]:
        """The single admin row, or None if no administrator is configured yet."""
        rows = self._query("SELECT * FROM admin WHERE id = 1")
        return rows[0] if rows else None

    def has_admin(self) -> bool:
        return self.get_admin() is not None

    def set_admin(
        self, username: str, password_hash: str, salt: str, iterations: int, session_secret: str
    ) -> None:
        """Create or replace the dashboard administrator (rotates the session secret)."""
        self.ensure_schema()
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO admin (id, username, password_hash, salt, iterations,
                                      session_secret, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                     username=excluded.username,
                     password_hash=excluded.password_hash,
                     salt=excluded.salt,
                     iterations=excluded.iterations,
                     session_secret=excluded.session_secret,
                     updated_at=datetime('now')""",
                (username, password_hash, salt, iterations, session_secret),
            )
            conn.commit()
        finally:
            conn.close()

    def ensure_job(self, client_id: int, spec: JobSpec) -> int:
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO jobs (client_id, name, source_paths, retention_days,
                                     encrypted, bwlimit_kbps)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_id, name) DO UPDATE SET
                     source_paths=excluded.source_paths,
                     retention_days=excluded.retention_days,
                     encrypted=excluded.encrypted,
                     bwlimit_kbps=excluded.bwlimit_kbps""",
                (
                    client_id,
                    spec.name,
                    json.dumps(spec.sources),
                    spec.retention_days,
                    int(spec.encrypted),
                    spec.bwlimit_kbps or None,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM jobs WHERE client_id = ? AND name = ?",
                (client_id, spec.name),
            ).fetchone()
            return int(row["id"])
        finally:
            conn.close()

    def delete_job(self, job_id: int) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()

    def record_run(
        self,
        job_id: int,
        status: str,
        bytes_transferred: int,
        message: str,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> int:
        """Insert a completed run, defaulting timestamps to now if not given."""
        self.ensure_schema()
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                """INSERT INTO runs (job_id, started_at, finished_at, status,
                                     bytes_transferred, message)
                   VALUES (?, COALESCE(?, datetime('now')),
                           COALESCE(?, datetime('now')), ?, ?, ?)""",
                (job_id, started_at, finished_at, status, bytes_transferred, message),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def delete_snapshot_row(self, snap_id: int) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("DELETE FROM snapshots WHERE id = ?", (snap_id,))
            conn.commit()
        finally:
            conn.close()

    def start_run(self, job_id: int) -> int:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO runs (job_id, status) VALUES (?, 'running')", (job_id,)
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def update_progress(
        self, run_id: int, percent: float, transferred: int, rate: Optional[str], eta: Optional[str]
    ) -> None:
        """Record a live progress tick for a running run (best-effort)."""
        conn = connect(self.db_path)
        try:
            conn.execute(
                """UPDATE runs SET percent=?, transferred=?, rate=?, eta=?,
                       bytes_transferred=?, updated_at=datetime('now')
                   WHERE id=? AND status='running'""",
                (percent, transferred, rate, eta, transferred, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def finish_run(self, run_id: int, status: str, bytes_transferred: int, message: str) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                """UPDATE runs SET finished_at=datetime('now'), status=?,
                       bytes_transferred=?, message=?, updated_at=datetime('now'),
                       percent=CASE WHEN ?='success' THEN 100 ELSE percent END
                   WHERE id=?""",
                (status, bytes_transferred, message, status, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def add_snapshot(
        self,
        job_id: int,
        run_id: int,
        path: str,
        size_bytes: int,
        encrypted: bool,
        created_at: Optional[str] = None,
    ) -> int:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                """INSERT INTO snapshots (job_id, run_id, path, size_bytes, encrypted, created_at)
                   VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
                (job_id, run_id, path, size_bytes, int(encrypted), created_at),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    # -- commands (server -> client work queue) --
    def enqueue_command(self, job_id: int, action: str) -> int:
        """Queue a 'start' or 'stop' command for a job; returns its id."""
        self.ensure_schema()
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO commands (job_id, action) VALUES (?, ?)",
                (job_id, action),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def update_command(
        self,
        command_id: int,
        status: str,
        message: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Advance a command's lifecycle (claimed/acted-on by the client)."""
        conn = connect(self.db_path)
        try:
            conn.execute(
                """UPDATE commands SET status=?,
                       message=COALESCE(?, message),
                       run_id=COALESCE(?, run_id),
                       updated_at=datetime('now')
                   WHERE id=?""",
                (status, message, run_id, command_id),
            )
            conn.commit()
        finally:
            conn.close()
