"""SQLite state persistence for tracking processed Instagram posts and synced calendar events."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from config import DEFAULT_DB_PATH, MONTREAL_TZ
from models import ExtractedEvent


class EventStateStore:
    """SQLite-backed deduplication and sync state store."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self._read_only = read_only
        self._closed = False

        if self._read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(f"State database not found for read-only access: {self.db_path}")
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only = ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_posts (
                    post_id TEXT PRIMARY KEY,
                    club_handle TEXT NOT NULL,
                    events_found INTEGER NOT NULL,
                    processed_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_events (
                    fingerprint TEXT PRIMARY KEY,
                    google_event_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_post_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise RuntimeError("Cannot write to EventStateStore opened in read-only mode.")

    def is_post_processed(self, post_id: str) -> bool:
        """Return True if the given Instagram post ID has already been processed."""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM processed_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self._read_only and "no such table" in str(exc).lower():
                return False
            raise
        return row is not None

    def mark_post_processed(self, post_id: str, club_handle: str, events_found: int) -> None:
        """Record an Instagram post as processed."""
        self._ensure_writable()
        processed_at = datetime.now(MONTREAL_TZ).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO processed_posts (post_id, club_handle, events_found, processed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    club_handle = excluded.club_handle,
                    events_found = excluded.events_found,
                    processed_at = excluded.processed_at
                """,
                (post_id, club_handle, int(events_found), processed_at),
            )

    def get_synced_event(self, fingerprint: str) -> dict[str, Any] | None:
        """Fetch stored sync metadata for an event fingerprint, or None if unseen."""
        try:
            row = self._conn.execute(
                """
                SELECT fingerprint, google_event_id, content_hash, source_post_id,
                       title, start_time, updated_at
                FROM synced_events
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self._read_only and "no such table" in str(exc).lower():
                return None
            raise
        return dict(row) if row is not None else None

    def upsert_synced_event(self, event: ExtractedEvent, google_event_id: str) -> None:
        """Insert or update a synced event record keyed by its deterministic fingerprint."""
        self._ensure_writable()
        updated_at = datetime.now(MONTREAL_TZ).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO synced_events (
                    fingerprint, google_event_id, content_hash,
                    source_post_id, title, start_time, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    google_event_id = excluded.google_event_id,
                    content_hash = excluded.content_hash,
                    source_post_id = excluded.source_post_id,
                    title = excluded.title,
                    start_time = excluded.start_time,
                    updated_at = excluded.updated_at
                """,
                (
                    event.fingerprint(),
                    google_event_id,
                    event.content_hash(),
                    event.source_post_id,
                    event.title,
                    event.start_time.isoformat(),
                    updated_at,
                ),
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> EventStateStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
