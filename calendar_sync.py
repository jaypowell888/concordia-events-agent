"""Google Calendar API v3 synchronization using a Service Account."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import MONTREAL_TZ
from models import ExtractedEvent

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_TIMEZONE = "America/Toronto"


def _load_service_account_credentials(service_account_json: str) -> Credentials:
    """Load Google Service Account credentials from either an inline JSON string or a file path."""
    raw = (service_account_json or "").strip()
    if not raw:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON string or path to a JSON file.")

    if raw.startswith("{"):
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid inline GOOGLE_SERVICE_ACCOUNT_JSON: {exc}") from exc
        if not isinstance(info, dict):
            raise ValueError("Inline GOOGLE_SERVICE_ACCOUNT_JSON must decode to a JSON object.")
        return Credentials.from_service_account_info(info, scopes=CALENDAR_SCOPES)

    cred_path = Path(raw).expanduser()
    try:
        return Credentials.from_service_account_file(str(cred_path), scopes=CALENDAR_SCOPES)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Google service account key file not found: {cred_path}") from exc


def _format_location(location: str, room: str | None) -> str:
    """Combine room and location cleanly without duplicating substrings."""
    clean_location = (location or "").strip()
    clean_room = (room or "").strip()

    if clean_room and clean_location:
        if clean_room.lower() in clean_location.lower():
            return clean_location
        if clean_location.lower() in clean_room.lower():
            return clean_room
        return f"{clean_room}, {clean_location}"
    return clean_room or clean_location


def _to_montreal_datetime(value: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in America/Toronto."""
    if value.tzinfo is None:
        return value.replace(tzinfo=MONTREAL_TZ)
    return value.astimezone(MONTREAL_TZ)


class GoogleCalendarSync:
    """Client wrapper for creating and updating events on a Google Calendar."""

    def __init__(
        self,
        calendar_id: str,
        service_account_json: str,
        service: Any | None = None,
    ) -> None:
        if not calendar_id or not calendar_id.strip():
            raise ValueError("GOOGLE_CALENDAR_ID must not be empty.")
        self.calendar_id = calendar_id.strip()
        self.service_account_json = service_account_json

        if service is not None:
            self._service = service
        else:
            credentials = _load_service_account_credentials(service_account_json)
            self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @property
    def service(self) -> Any:
        """Return the underlying Google Calendar API service client."""
        return self._service

    def build_event_body(self, event: ExtractedEvent) -> dict[str, Any]:
        """Convert an ExtractedEvent into a Google Calendar API v3 event payload."""
        start_dt = _to_montreal_datetime(event.start_time)
        if event.end_time is not None:
            end_dt = _to_montreal_datetime(event.end_time)
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(hours=2)
        else:
            end_dt = start_dt + timedelta(hours=2)

        description_parts: list[str] = []
        if event.description and event.description.strip():
            description_parts.append(event.description.strip())

        metadata_lines: list[str] = []
        club_name = (event.club_name or "").strip()
        club_handle = (event.club_handle or "").strip().lstrip("@")
        if club_name and club_handle:
            metadata_lines.append(f"Hosted by: {club_name} (@{club_handle})")
        elif club_name:
            metadata_lines.append(f"Hosted by: {club_name}")
        elif club_handle:
            metadata_lines.append(f"Hosted by: @{club_handle}")

        if event.registration_url and event.registration_url.strip():
            metadata_lines.append(f"Registration: {event.registration_url.strip()}")

        if event.source_post_url and event.source_post_url.strip():
            metadata_lines.append(f"Source Instagram Post: {event.source_post_url.strip()}")

        if metadata_lines:
            description_parts.append("\n".join(metadata_lines))

        body: dict[str, Any] = {
            "summary": event.title.strip(),
            "location": _format_location(event.location, event.room),
            "description": "\n\n".join(description_parts),
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": CALENDAR_TIMEZONE,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": CALENDAR_TIMEZONE,
            },
        }
        if event.color_id:
            body["colorId"] = event.color_id
        return body

    def create_event(self, event: ExtractedEvent) -> str:
        """Insert a new event into Google Calendar and return its created Google event ID."""
        body = self.build_event_body(event)
        created = (
            self._service.events()
            .insert(calendarId=self.calendar_id, body=body)
            .execute()
        )
        event_id = created.get("id") if isinstance(created, dict) else None
        if not event_id:
            raise RuntimeError("Google Calendar API insert response did not include an event 'id'.")
        return str(event_id)

    def update_event(self, google_event_id: str, event: ExtractedEvent) -> str:
        """Update an existing Google Calendar event and return its Google event ID."""
        body = self.build_event_body(event)
        updated = (
            self._service.events()
            .update(calendarId=self.calendar_id, eventId=google_event_id, body=body)
            .execute()
        )
        if isinstance(updated, dict) and updated.get("id"):
            return str(updated["id"])
        return google_event_id

    def patch_event_color(self, google_event_id: str, color_id: str) -> str:
        """Patch only the colorId of an existing Google Calendar event."""
        updated = (
            self._service.events()
            .patch(
                calendarId=self.calendar_id,
                eventId=google_event_id,
                body={"colorId": color_id},
            )
            .execute()
        )
        if isinstance(updated, dict) and updated.get("id"):
            return str(updated["id"])
        return google_event_id
