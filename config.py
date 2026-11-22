"""Shared configuration constants and environment loading for Concordia Events Agent."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel

MONTREAL_TZ = ZoneInfo("America/Toronto")
DEFAULT_CLUBS_PATH = Path("clubs.json")
DEFAULT_DB_PATH = Path("state/events.db")
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
DEFAULT_GEMINI_THINKING_LEVEL = "medium"
DEFAULT_GEMINI_ESCALATION_MODEL = ""
DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL = "high"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
DEFAULT_APIFY_ACTOR = "apify/instagram-scraper"


class Settings(BaseModel):
    """Runtime settings loaded from environment variables and CLI overrides."""

    gemini_api_key: str = ""
    apify_token: str = ""
    google_calendar_id: str = ""
    google_service_account_json: str = "./service-account.json"
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL
    gemini_escalation_model: str = DEFAULT_GEMINI_ESCALATION_MODEL
    gemini_escalation_thinking_level: str = DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    apify_actor: str = DEFAULT_APIFY_ACTOR
    clubs_path: Path = DEFAULT_CLUBS_PATH
    db_path: Path = DEFAULT_DB_PATH

    def validate_for_run(
        self,
        dry_run: bool = False,
        require_apify: bool = True,
        require_gemini: bool = True,
    ) -> None:
        """Validate that required credentials are present for the selected run mode.

        Raises:
            ValueError: Lists any missing required environment variables.
        """
        missing: list[str] = []
        if require_gemini and not self.gemini_api_key.strip():
            missing.append("GEMINI_API_KEY")
        if require_apify and not self.apify_token.strip():
            missing.append("APIFY_TOKEN")

        if not dry_run:
            if not self.google_calendar_id.strip():
                missing.append("GOOGLE_CALENDAR_ID")
            if not self.google_service_account_json.strip():
                missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")

        if missing:
            vars_list = ", ".join(missing)
            raise ValueError(
                f"Missing required environment variable(s): {vars_list}. "
                "Set them in your .env file or shell environment."
            )


def load_settings(
    env_file: Path | None = None,
    lookback_days: int | None = None,
    clubs_path: Path | None = None,
    db_path: Path | None = None,
) -> Settings:
    """Load settings from dotenv and ``os.environ`` with optional explicit overrides."""
    load_dotenv(dotenv_path=env_file)

    if lookback_days is not None:
        resolved_lookback = lookback_days
    else:
        raw_lookback = os.environ.get("LOOKBACK_DAYS", "").strip()
        resolved_lookback = int(raw_lookback) if raw_lookback else DEFAULT_LOOKBACK_DAYS

    if clubs_path is not None:
        resolved_clubs_path = Path(clubs_path)
    else:
        raw_clubs = os.environ.get("CLUBS_PATH", "").strip()
        resolved_clubs_path = Path(raw_clubs) if raw_clubs else DEFAULT_CLUBS_PATH

    if db_path is not None:
        resolved_db_path = Path(db_path)
    else:
        raw_db = os.environ.get("DB_PATH", "").strip()
        resolved_db_path = Path(raw_db) if raw_db else DEFAULT_DB_PATH

    raw_fallback = os.environ.get("GEMINI_FALLBACK_MODEL")
    resolved_fallback = (
        DEFAULT_GEMINI_FALLBACK_MODEL
        if raw_fallback is None
        else raw_fallback.strip()
    )

    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        apify_token=os.environ.get("APIFY_TOKEN", "").strip(),
        google_calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "").strip(),
        google_service_account_json=os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_JSON", "./service-account.json"
        ).strip(),
        lookback_days=resolved_lookback,
        gemini_model=os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL,
        gemini_thinking_level=(
            os.environ.get("GEMINI_THINKING_LEVEL", "").strip()
            or DEFAULT_GEMINI_THINKING_LEVEL
        ),
        gemini_escalation_model=(
            os.environ.get("GEMINI_ESCALATION_MODEL", "").strip()
            or DEFAULT_GEMINI_ESCALATION_MODEL
        ),
        gemini_escalation_thinking_level=(
            os.environ.get("GEMINI_ESCALATION_THINKING_LEVEL", "").strip()
            or DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL
        ),
        gemini_fallback_model=resolved_fallback,
        apify_actor=os.environ.get("APIFY_ACTOR", "").strip() or DEFAULT_APIFY_ACTOR,
        clubs_path=resolved_clubs_path,
        db_path=resolved_db_path,
    )
