"""Typed views over the database rows.

Plain dataclasses kept dependency-free so the client never needs pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Client:
    id: int
    name: str
    hostname: Optional[str] = None
    public_key: Optional[str] = None
    enrolled_at: Optional[str] = None
    last_seen: Optional[str] = None


@dataclass
class Job:
    id: int
    client_id: int
    name: str
    source_paths: list[str]
    schedule: Optional[str] = None
    retention_days: int = 30
    encrypted: bool = False
    bwlimit_kbps: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class Run:
    id: int
    job_id: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "running"
    bytes_transferred: int = 0
    message: Optional[str] = None


@dataclass
class Snapshot:
    id: int
    job_id: int
    run_id: Optional[int]
    path: str
    created_at: Optional[str] = None
    size_bytes: int = 0
    encrypted: bool = False
