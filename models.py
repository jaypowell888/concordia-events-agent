"""Shared Pydantic data models for Concordia Events Agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from config import MONTREAL_TZ


def _normalize_handle(value: Any, *, allow_empty: bool = False) -> str:
    """Normalize an Instagram handle: strip whitespace, strip leading '@', lowercase."""
    if not isinstance(value, str):
        raise ValueError("Instagram handle must be a string")
    normalized = value.strip().lstrip("@").strip().lower()
    if not normalized and not allow_empty:
        raise ValueError("Instagram handle must not be empty")
    return normalized


class Club(BaseModel):
    """A Concordia or JMSB student club tracked by its Instagram handle."""

    name: str
    handle: str
    faculty: str = "General"
    source: str = "seed"

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("handle", mode="before")
    @classmethod
    def _clean_handle(cls, value: Any) -> str:
        return _normalize_handle(value, allow_empty=False)


class InstagramPost(BaseModel):
    """Normalized Instagram post scraped from a club profile."""

    post_id: str
    shortcode: str = ""
    club_handle: str
    club_name: str = ""
    caption: str = ""
    url: str
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    timestamp: datetime

    @field_validator("club_handle", mode="before")
    @classmethod
    def _clean_club_handle(cls, value: Any) -> str:
        return _normalize_handle(value, allow_empty=False)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(MONTREAL_TZ)


class ExtractedEvent(BaseModel):
    """Structured campus event extracted from an Instagram post."""

    is_event: bool = True
    title: str
    start_time: datetime
    end_time: datetime | None = None
    location: str = "Concordia University, Montreal, QC"
    room: str | None = None
    description: str = ""
    registration_url: str | None = None
    club_name: str = ""
    club_handle: str = ""
    source_post_id: str = ""
    source_post_url: str = ""

    @field_validator("club_handle", mode="before")
    @classmethod
    def _clean_club_handle(cls, value: Any) -> str:
        if value is None:
            return ""
        return _normalize_handle(value, allow_empty=True)

    @field_validator("start_time", mode="after")
    @classmethod
    def _normalize_start_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=MONTREAL_TZ)
        return value.astimezone(MONTREAL_TZ)

    @field_validator("end_time", mode="after")
    @classmethod
    def _normalize_end_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=MONTREAL_TZ)
        return value.astimezone(MONTREAL_TZ)

    @model_validator(mode="after")
    def _ensure_valid_end_time(self) -> ExtractedEvent:
        start = self.start_time.astimezone(MONTREAL_TZ)
        self.start_time = start
        if self.end_time is None:
            self.end_time = start + timedelta(hours=2)
        else:
            end = self.end_time.astimezone(MONTREAL_TZ)
            if end <= start:
                end = start + timedelta(hours=2)
            self.end_time = end
        return self

    def content_hash(self) -> str:
        """Return a SHA-256 hex digest representing the event's normalized content."""
        payload = {
            "title": " ".join(self.title.strip().split()),
            "start_time": self.start_time.astimezone(MONTREAL_TZ).isoformat(),
            "end_time": (
                self.end_time.astimezone(MONTREAL_TZ).isoformat()
                if self.end_time is not None
                else ""
            ),
            "location": " ".join(self.location.strip().split()),
            "room": " ".join(self.room.strip().split()) if self.room else "",
            "description": self.description.strip(),
            "registration_url": (self.registration_url or "").strip(),
            "club_name": self.club_name.strip(),
            "club_handle": self.club_handle.strip().lstrip("@").lower(),
            "source_post_url": self.source_post_url.strip(),
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def fingerprint(self) -> str:
        """Return a stable SHA-256 hex digest identifying this event for deduplication."""
        if self.source_post_id and self.source_post_id.strip():
            key = self.source_post_id.strip()
        else:
            handle = self.club_handle.strip().lstrip("@").lower()
            title = " ".join(self.title.strip().lower().split())
            start_date = self.start_time.astimezone(MONTREAL_TZ).date().isoformat()
            key = f"{handle}|{title}|{start_date}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def format_summary_line(self, action: str) -> str:
        """Format a single-line human-readable summary for CLI output."""
        action_tag = action.strip().upper()
        local_start = self.start_time.astimezone(MONTREAL_TZ)
        time_str = local_start.strftime("%Y-%m-%d %H:%M %Z")
        place = (
            self.room.strip()
            if self.room and self.room.strip()
            else self.location.strip()
        )
        if self.club_handle and self.club_handle.strip():
            handle_str = f"@{self.club_handle.strip().lstrip('@').lower()}"
        elif self.club_name and self.club_name.strip():
            handle_str = self.club_name.strip()
        else:
            handle_str = "@unknown"
        return f"[{action_tag}] {self.title} | {time_str} | {place} | {handle_str}"
