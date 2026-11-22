"""Extract structured Concordia campus events from Instagram posts using Gemini."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests
from dateutil import parser as dateutil_parser
from pydantic import BaseModel, Field

from config import (
    DEFAULT_GEMINI_ESCALATION_MODEL,
    DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL,
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_THINKING_LEVEL,
    MONTREAL_TZ,
)
from models import ExtractedEvent, InstagramPost

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB safety limit for poster downloads
_MAX_IMAGES_PER_POST = 5
_ESCALATION_CONFIDENCE_THRESHOLD = 0.75
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_TRANSIENT_CODE_RE = re.compile(r"\b(?:503|429)\b")
_TRANSIENT_CODES = frozenset({429, 503})
_NON_TRANSIENT_CODES = frozenset({400, 401, 403, 404})
_DEFAULT_CONCORDIA_LOCATION = "Concordia University, Montreal, QC"
_CONCORDIA_DEFAULT_LOCATIONS = frozenset(
    {
        "concordia university, montreal, qc",
        "concordia university",
        "concordia university, montreal",
    }
)
_ONLINE_SIGNAL_RE = re.compile(
    r"\b(?:online|virtual|webinar|zoom|microsoft\s+teams|ms\s+teams|google\s+meet|livestream|live\s+stream|webcast)\b",
    re.IGNORECASE,
)
_ONLINE_ROOM_RE = re.compile(
    r"^(?:online|virtual|zoom|webinar|teams|microsoft\s+teams|ms\s+teams|google\s+meet|livestream|n/a|none|tba)$",
    re.IGNORECASE,
)
_PHYSICAL_CONCORDIA_CUE_RE = re.compile(
    r"\b(?:in[\s-]person|hall\s+building|john\s+molson\s+building|ev\s+building|webster\s+library|loyola\s+campus|sgw\s+campus|h-\d{3}|mb[\s-]s?\d+\.\d+|ev[\s-]\d+\.\d+)\b",
    re.IGNORECASE,
)
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class RawExtractedEvent(BaseModel):
    """Schema for a single candidate event returned by Gemini structured output."""

    is_event: bool = Field(
        default=True,
        description=(
            "True if this item describes an actual scheduled event with a date/time; "
            "False for memes, photo recaps, hiring announcements without a scheduled session, "
            "or general club updates."
        ),
    )
    title: str = Field(
        default="",
        description="Concise, descriptive title of the event.",
    )
    start_time: str | None = Field(
        default=None,
        description=(
            "Event start date and time in ISO-8601 format in America/Toronto (Montreal) "
            "timezone, e.g. '2026-11-26T18:00:00-05:00'."
        ),
    )
    end_time: str | None = Field(
        default=None,
        description=(
            "Event end date and time in ISO-8601 format in America/Toronto (Montreal) "
            "timezone, or null if not mentioned."
        ),
    )
    location: str = Field(
        default="",
        description=(
            "Real venue, building, campus, or 'Online' (e.g., 'Hall Building, Concordia University (SGW)', "
            "'John Molson Building (MB), Concordia University', 'Hotel Bonaventure Montreal', "
            "'Online', or 'Online (Zoom)'). Do NOT default external or virtual events to Concordia University."
        ),
    )
    room: str | None = Field(
        default=None,
        description=(
            "Specific Concordia room code if stated (e.g., 'H-937', 'MB S2.330', 'EV 1.605', "
            "'JMSB 9th Floor'), or null if not specified."
        ),
    )
    description: str = Field(
        default="",
        description="Short, clean summary of the event details, speakers, food/perks, or dress code.",
    )
    registration_url: str | None = Field(
        default=None,
        description=(
            "Explicit http(s) registration, ticket, or RSVP URL if present in the caption/poster; "
            "null otherwise."
        ),
    )


class EventExtractionResponse(BaseModel):
    """Structured JSON wrapper returned by Gemini for an Instagram post."""

    events: list[RawExtractedEvent] = Field(
        default_factory=list,
        description="List of scheduled events extracted from the post. Empty if not an event.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Overall confidence (0.0 to 1.0) in the extracted event details.",
    )
    needs_deep_review: bool = Field(
        default=False,
        description=(
            "Set true if dates, times, room codes, or poster text are ambiguous, dense, or "
            "hard to read and would benefit from a deeper review pass."
        ),
    )


def _is_daily_quota_error(exc: Exception) -> bool:
    """Return True if ``exc`` is a 429/RESOURCE_EXHAUSTED daily-quota (PerDay) error."""
    is_429 = False
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if val == 429 or (isinstance(val, str) and val.strip() == "429"):
            is_429 = True
            break

    status = getattr(exc, "status", None)
    if isinstance(status, str) and status.strip().upper() == "RESOURCE_EXHAUSTED":
        is_429 = True

    message_parts = [str(exc)]
    raw_msg = getattr(exc, "message", None)
    if isinstance(raw_msg, str):
        message_parts.append(raw_msg)
    combined = " ".join(message_parts)
    upper = combined.upper()

    if not is_429 and ("RESOURCE_EXHAUSTED" in upper or re.search(r"\b429\b", combined)):
        is_429 = True

    if not is_429:
        return False

    return "GENERATEREQUESTSPERDAY" in upper or "PERDAY" in upper


def _is_transient_gemini_error(exc: Exception) -> bool:
    """Return True if ``exc`` represents a retryable Gemini 503/429 transient error."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            if val in _TRANSIENT_CODES:
                return True
            if val in _NON_TRANSIENT_CODES:
                return False
        elif isinstance(val, str) and val.isdigit():
            numeric = int(val)
            if numeric in _TRANSIENT_CODES:
                return True
            if numeric in _NON_TRANSIENT_CODES:
                return False

    status = getattr(exc, "status", None)
    if isinstance(status, str):
        status_upper = status.strip().upper()
        if status_upper in {"UNAVAILABLE", "RESOURCE_EXHAUSTED"}:
            return True

    text = str(exc)
    upper = text.upper()
    if "UNAVAILABLE" in upper or "RESOURCE_EXHAUSTED" in upper:
        return True
    return bool(_TRANSIENT_CODE_RE.search(text))


def _build_extraction_prompt(post: InstagramPost) -> str:
    """Build the Gemini instruction prompt anchored to the post's Montreal publication date."""
    published_local = post.timestamp.astimezone(MONTREAL_TZ)
    published_str = published_local.strftime("%A, %Y-%m-%d %H:%M %Z (%z)")
    club_label = post.club_name or post.club_handle

    return (
        "You are an expert student-event extraction assistant for Concordia University "
        "(SGW and Loyola campuses) and the John Molson School of Business (JMSB) in Montreal, QC.\n\n"
        "Analyze the Instagram post below (and any attached poster/carousel images) and extract "
        "all distinct scheduled student events.\n\n"
        "RULES:\n"
        "1. Ignore non-event posts: memes, event recaps/photo dumps, giveaway posts without a scheduled "
        "in-person/online gathering, or executive hiring/application deadlines that are not a scheduled "
        "event. For non-event posts, return {\"events\": []}.\n"
        "2. Resolve all relative dates (e.g. 'tonight', 'tomorrow', 'this Thursday', 'next Friday') "
        "using the Post Published Date in Montreal time (America/Toronto) provided below.\n"
        "3. Format `start_time` and `end_time` as ISO-8601 datetimes in the America/Toronto timezone "
        "(e.g., 2026-11-26T18:00:00-05:00). If no end time is given, set `end_time` to null.\n"
        "4. Save the Date / missing start time: If a post has an event date but no explicit start time "
        "('Save the Date'), default `start_time` to `18:00:00` in America/Toronto on that date and "
        "start `description` with \"Time TBA — \".\n"
        "5. Location, venues, and virtual/online events:\n"
        "   - Virtual / online events (webinars, Zoom, Microsoft Teams, Google Meet, livestreams, online "
        "info sessions): set `location` to \"Online\" (or \"Online (Zoom)\", \"Online (Microsoft Teams)\", "
        "etc.) and set `room` to null. NEVER default virtual/online events to \"Concordia University, Montreal, QC\".\n"
        "   - Off-campus / external / downtown Montreal venues (hotels, corporate offices, bars, restaurants, "
        "ski resorts, external conference centers): preserve the real venue name and address in `location` "
        "(and room/suite in `room` if given). NEVER replace an external venue with \"Concordia University, Montreal, QC\".\n"
        "   - On-campus Concordia events: ONLY use Concordia buildings/rooms or \"Concordia University, Montreal, QC\" "
        "when the event is actually held at Concordia:\n"
        "     * Henry F. Hall Building: 'H-937', 'H-110', 'Hall 7th Floor' (SGW Campus)\n"
        "     * John Molson Building / JMSB: 'MB S2.330', 'MB 9th Floor', 'MB 3.210' (SGW Campus)\n"
        "     * EV Building (Engineering/Visual Arts): 'EV 1.605', 'EV 2.260' (SGW Campus)\n"
        "     * Other SGW buildings: FB, FG, LB (Webster Library), VA, GM, CL, H\n"
        "     * Loyola Campus buildings: SP, CC, AD, CJ, VL, RF, PY\n"
        "     Put the specific room code in `room` and the building/campus/venue in `location`.\n"
        "6. Registration URLs: Set `registration_url` to null unless an explicit `http://` or `https://` "
        "URL is present in the caption or poster (do not invent URLs). If registration says link in bio, "
        "leave `registration_url` as null and mention \"Link in bio\" inside `description` instead.\n"
        "7. If a single post promotes multiple distinct event sessions/dates, return one entry per event.\n"
        "8. Set `confidence` (0.0–1.0) and `needs_deep_review` (true if dates, times, room numbers, "
        "or stylized poster text are ambiguous or hard to read).\n\n"
        f"Club Name: {club_label}\n"
        f"Instagram Handle: @{post.club_handle}\n"
        f"Post Published Date (Montreal): {published_str}\n"
        f"Post URL: {post.url}\n"
        f"Post Caption:\n{post.caption or '(no caption text)'}\n"
    )


def _resolve_location_and_room(
    raw: RawExtractedEvent,
    post: InstagramPost,
) -> tuple[str, str | None]:
    """Preserve real external/online venues and avoid erroneously defaulting virtual events to Concordia."""
    location = (raw.location or "").strip()
    room = raw.room.strip() if isinstance(raw.room, str) and raw.room.strip() else None
    has_physical_room = bool(room and not _ONLINE_ROOM_RE.match(room))

    if location and _ONLINE_SIGNAL_RE.search(location):
        if not has_physical_room:
            room = None
        return location, room

    if location and location.lower() not in _CONCORDIA_DEFAULT_LOCATIONS:
        return location, room

    event_text = " ".join(part for part in (raw.title, raw.description) if part)
    combined_text = " ".join(part for part in (event_text, post.caption) if part)

    if (
        not has_physical_room
        and _ONLINE_SIGNAL_RE.search(combined_text)
        and not _PHYSICAL_CONCORDIA_CUE_RE.search(event_text)
    ):
        return "Online", None

    if not location:
        return _DEFAULT_CONCORDIA_LOCATION, room

    return location, room


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a Montreal-timezone aware datetime."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = dateutil_parser.isoparse(text)
    except ValueError:
        try:
            dt = dateutil_parser.parse(text)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MONTREAL_TZ)
    return dt.astimezone(MONTREAL_TZ)


class GeminiEventExtractor:
    """Uses Google Gemini structured outputs to extract Concordia events from Instagram posts."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        client: Any | None = None,
        thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL,
        escalation_model: str = DEFAULT_GEMINI_ESCALATION_MODEL,
        escalation_thinking_level: str = DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL,
        fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL,
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 30.0,
        post_pacing_delay: float = 1.5,
        sleep_fn: Callable[[float], None] | None = None,
        fetch_images: bool = True,
        image_timeout: float = 12.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client
        self.thinking_level = thinking_level
        self.escalation_model = (escalation_model or "").strip()
        self.escalation_thinking_level = escalation_thinking_level
        self.fallback_model = (fallback_model or "").strip()
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = float(retry_base_delay)
        self.retry_max_delay = float(retry_max_delay)
        self.post_pacing_delay = float(post_pacing_delay)
        self._sleep: Callable[[float], None] = sleep_fn if sleep_fn is not None else time.sleep
        self.fetch_images = fetch_images
        self.image_timeout = image_timeout
        self._posts_called = 0
        self._primary_daily_quota_exhausted = False

    @property
    def client(self) -> Any:
        """Lazily construct and return the ``google.genai.Client``."""
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _fetch_image_bytes(self, image_url: str) -> tuple[bytes, str] | None:
        """Download poster image bytes gracefully, returning None on any failure."""
        try:
            response = requests.get(
                image_url,
                timeout=self.image_timeout,
                headers={"User-Agent": _BROWSER_USER_AGENT},
            )
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            data = response.content
            if not data or len(data) > _MAX_IMAGE_BYTES:
                return None
            return data, content_type
        except Exception as exc:  # noqa: BLE001
            logger.debug("Poster image fetch failed for %s (%s); using caption only.", image_url, exc)
            return None

    def _collect_image_urls(self, post: InstagramPost) -> list[str]:
        """Collect up to 5 deduplicated image URLs from post.image_urls or fallback image_url."""
        raw_urls: list[str] = []
        if post.image_urls:
            raw_urls = list(post.image_urls)
        elif post.image_url:
            raw_urls = [post.image_url]

        urls: list[str] = []
        seen: set[str] = set()
        for url in raw_urls:
            if not isinstance(url, str):
                continue
            cleaned = url.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)
                if len(urls) >= _MAX_IMAGES_PER_POST:
                    break
        return urls

    def _build_contents(self, post: InstagramPost, prompt: str) -> list[Any]:
        """Build Gemini multimodal contents list (prompt + up to 5 poster/carousel images)."""
        contents: list[Any] = [prompt]
        if not self.fetch_images:
            return contents

        image_urls = self._collect_image_urls(post)
        if not image_urls:
            return contents

        from google.genai import types

        for image_url in image_urls:
            image_payload = self._fetch_image_bytes(image_url)
            if image_payload is None:
                continue
            image_bytes, mime_type = image_payload
            try:
                contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not attach poster image part for %s: %s", image_url, exc)
        return contents

    def _build_config(
        self,
        thinking_level: str | None = None,
        model_name: str | None = None,
    ) -> Any:
        """Create GenerateContentConfig enforcing structured JSON output.

        Only attaches ``thinking_config`` for ``gemini-4-*`` models; omits it for
        older models (e.g., ``gemini-2.5-flash``) to avoid 400 API errors.
        """
        from google.genai import types

        target_model = (model_name or self.model or "").strip()
        config_kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": EventExtractionResponse,
        }

        if target_model.lower().startswith("gemini-4-"):
            resolved_level = (
                thinking_level or self.thinking_level or DEFAULT_GEMINI_THINKING_LEVEL
            ).strip()
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=resolved_level
            )

        return types.GenerateContentConfig(**config_kwargs)

    def _generate_with_retries(
        self,
        *,
        model: str,
        contents: list[Any],
        thinking_level: str | None,
    ) -> Any:
        """Call ``generate_content`` with exponential backoff + jitter on 503/429 errors."""
        config = self._build_config(thinking_level=thinking_level, model_name=model)
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if _is_daily_quota_error(exc):
                    raise
                if not _is_transient_gemini_error(exc) or attempt >= self.max_retries:
                    raise
                delay = min(
                    self.retry_max_delay,
                    self.retry_base_delay * (2**attempt),
                ) + random.uniform(0.0, 1.0)
                logger.warning(
                    "Transient Gemini error on model '%s' (attempt %d/%d): %s; "
                    "retrying in %.2fs.",
                    model,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                    delay,
                )
                self._sleep(delay)
        raise RuntimeError("Unreachable retry loop termination")  # pragma: no cover

    def _parse_response(self, response: Any) -> EventExtractionResponse:
        """Parse Gemini's structured output from either ``response.parsed`` or ``response.text``."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, EventExtractionResponse):
            return parsed
        if isinstance(parsed, dict):
            return EventExtractionResponse.model_validate(parsed)

        raw_text = getattr(response, "text", None)
        if not isinstance(raw_text, str) or not raw_text.strip():
            return EventExtractionResponse(events=[])

        cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
        return EventExtractionResponse.model_validate_json(cleaned)

    def _should_escalate(self, extraction: EventExtractionResponse) -> bool:
        """Return True when an event post warrants a second pass with the escalation model."""
        if not self.escalation_model:
            return False
        if self._primary_daily_quota_exhausted and self.escalation_model == self.model:
            return False
        has_event = any(raw.is_event for raw in extraction.events)
        if not has_event:
            return False
        return extraction.needs_deep_review or extraction.confidence < _ESCALATION_CONFIDENCE_THRESHOLD

    def extract_events(self, post: InstagramPost) -> list[ExtractedEvent]:
        """Extract zero or more ``ExtractedEvent`` objects from a single Instagram post.

        Args:
            post: Normalized Instagram post to analyze.

        Returns:
            List of validated ``ExtractedEvent`` models with source metadata attached
            and non-event items filtered out.
        """
        if not post.caption.strip() and not post.image_url and not post.image_urls:
            return []

        if self._posts_called > 0 and self.post_pacing_delay > 0:
            self._sleep(self.post_pacing_delay)
        self._posts_called += 1

        prompt = _build_extraction_prompt(post)
        contents = self._build_contents(post, prompt)
        has_fallback = bool(self.fallback_model and self.fallback_model != self.model)

        if self._primary_daily_quota_exhausted and has_fallback:
            fallback_response = self._generate_with_retries(
                model=self.fallback_model,
                contents=contents,
                thinking_level=self.thinking_level,
            )
            extraction = self._parse_response(fallback_response)
        else:
            try:
                response = self._generate_with_retries(
                    model=self.model,
                    contents=contents,
                    thinking_level=self.thinking_level,
                )
                extraction = self._parse_response(response)
            except Exception as exc:
                if _is_daily_quota_error(exc):
                    self._primary_daily_quota_exhausted = True
                    if has_fallback:
                        logger.warning(
                            "Primary Gemini model '%s' hit daily quota (429 PerDay) on post %s (@%s); "
                            "switching immediately to fallback '%s' for the remainder of the run.",
                            self.model,
                            post.post_id,
                            post.club_handle,
                            self.fallback_model,
                        )
                        fallback_response = self._generate_with_retries(
                            model=self.fallback_model,
                            contents=contents,
                            thinking_level=self.thinking_level,
                        )
                        extraction = self._parse_response(fallback_response)
                    else:
                        logger.warning(
                            "Primary Gemini model '%s' hit daily quota (429 PerDay) on post %s (@%s) "
                            "and no fallback model is configured.",
                            self.model,
                            post.post_id,
                            post.club_handle,
                        )
                        raise
                elif _is_transient_gemini_error(exc) and has_fallback:
                    logger.warning(
                        "Primary Gemini model '%s' exhausted retries for post %s (@%s): %s; "
                        "falling back to '%s'.",
                        self.model,
                        post.post_id,
                        post.club_handle,
                        exc,
                        self.fallback_model,
                    )
                    fallback_response = self._generate_with_retries(
                        model=self.fallback_model,
                        contents=contents,
                        thinking_level=self.thinking_level,
                    )
                    extraction = self._parse_response(fallback_response)
                else:
                    raise

        if self._should_escalate(extraction):
            logger.info(
                "Escalating extraction for post %s (@%s) to model=%s thinking_level=%s "
                "(confidence=%.2f, needs_deep_review=%s)",
                post.post_id,
                post.club_handle,
                self.escalation_model,
                self.escalation_thinking_level,
                extraction.confidence,
                extraction.needs_deep_review,
            )
            try:
                escalated_response = self._generate_with_retries(
                    model=self.escalation_model,
                    contents=contents,
                    thinking_level=self.escalation_thinking_level,
                )
                extraction = self._parse_response(escalated_response)
            except Exception as exc:
                if _is_daily_quota_error(exc):
                    if self.escalation_model == self.model:
                        self._primary_daily_quota_exhausted = True
                    logger.warning(
                        "Escalation model '%s' hit daily quota (429 PerDay) for post %s (@%s); "
                        "keeping primary extraction result.",
                        self.escalation_model,
                        post.post_id,
                        post.club_handle,
                    )
                elif _is_transient_gemini_error(exc):
                    logger.warning(
                        "Escalation model '%s' exhausted retries for post %s (@%s): %s; "
                        "keeping primary extraction result.",
                        self.escalation_model,
                        post.post_id,
                        post.club_handle,
                        exc,
                    )
                else:
                    raise

        events: list[ExtractedEvent] = []

        for raw in extraction.events:
            if not raw.is_event:
                continue

            title = raw.title.strip()
            if not title:
                continue

            start_dt = _parse_iso_datetime(raw.start_time)
            if start_dt is None:
                logger.warning(
                    "Skipping extracted event '%s' from post %s: invalid start_time=%r",
                    title,
                    post.post_id,
                    raw.start_time,
                )
                continue

            end_dt = _parse_iso_datetime(raw.end_time)
            location, room = _resolve_location_and_room(raw, post)
            reg_url = (
                raw.registration_url.strip()
                if isinstance(raw.registration_url, str) and raw.registration_url.strip()
                else None
            )

            try:
                event = ExtractedEvent(
                    is_event=True,
                    title=title,
                    start_time=start_dt,
                    end_time=end_dt,
                    location=location,
                    room=room,
                    description=(raw.description or "").strip(),
                    registration_url=reg_url,
                    club_name=post.club_name or post.club_handle,
                    club_handle=post.club_handle,
                    source_post_id=post.post_id,
                    source_post_url=post.url,
                    color_id=post.color_id,
                )
                events.append(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Skipping invalid extracted event '%s' from post %s: %s",
                    title,
                    post.post_id,
                    exc,
                )

        return events
