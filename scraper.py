"""Load configured Concordia clubs and fetch recent Instagram posts via Apify."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dateutil import parser as dateutil_parser

from config import DEFAULT_APIFY_ACTOR, MONTREAL_TZ
from models import Club, InstagramPost

logger = logging.getLogger(__name__)


def load_clubs(clubs_path: Path) -> list[Club]:
    """Load and validate student clubs from a JSON file.

    Accepts either a top-level JSON array of club objects or an object containing
    a ``"clubs"`` list.

    Raises:
        FileNotFoundError: If ``clubs_path`` does not exist, with instructions to
            run ``python discover_clubs.py``.
        ValueError: If the JSON file does not match an expected structure.
    """
    path = Path(clubs_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Clubs file not found at '{path}'. "
            "Run `python discover_clubs.py` first to generate clubs.json."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("clubs"), list):
        raw_items = data["clubs"]
    elif isinstance(data, list):
        raw_items = data
    else:
        raise ValueError(
            f"Invalid clubs file format in '{path}': expected a JSON list or "
            "an object with a 'clubs' list."
        )

    clubs: list[Club] = []
    seen_handles: set[str] = set()
    for item in raw_items:
        club = Club.model_validate(item)
        if club.handle not in seen_handles:
            seen_handles.add(club.handle)
            clubs.append(club)

    return clubs


def _parse_timestamp(raw_value: Any) -> datetime | None:
    """Parse an Apify timestamp (ISO string, Unix seconds/ms, or datetime) into MONTREAL_TZ."""
    if raw_value is None or raw_value == "":
        return None

    dt: datetime | None = None
    if isinstance(raw_value, datetime):
        dt = raw_value
    elif isinstance(raw_value, (int, float)):
        # Handle millisecond epoch timestamps if encountered.
        epoch = raw_value / 1000.0 if raw_value > 1e12 else float(raw_value)
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        try:
            dt = dateutil_parser.isoparse(text)
        except ValueError:
            try:
                dt = dateutil_parser.parse(text)
            except (ValueError, TypeError):
                return None

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(MONTREAL_TZ)


def _extract_handle_from_url(url: str | None) -> str:
    """Fallback helper to recover an Instagram handle from an Apify inputUrl."""
    if not url:
        return ""
    parsed = urlparse(str(url))
    segments = [seg.strip() for seg in parsed.path.split("/") if seg.strip()]
    if not segments:
        return ""
    return segments[0].lstrip("@").lower()


def _extract_image_urls(item: dict[str, Any]) -> list[str]:
    """Extract deduplicated poster/carousel image URLs from an Apify item, preserving order."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> None:
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)

    _add(item.get("displayUrl"))

    images = item.get("images")
    if isinstance(images, list):
        for entry in images:
            _add(entry)

    child_posts = item.get("childPosts")
    if isinstance(child_posts, list):
        for child in child_posts:
            if isinstance(child, dict):
                _add(child.get("displayUrl") or child.get("imageUrl"))

    if not urls:
        _add(item.get("imageUrl"))
        _add(item.get("thumbnailUrl"))

    return urls


def _extract_dataset_id(run: Any) -> str | None:
    """Extract the default dataset ID from either a dict or an Apify v3 Run object."""
    if run is None:
        return None
    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        dataset_id = getattr(run, "default_dataset_id", None) or getattr(
            run, "defaultDatasetId", None
        )
    if isinstance(dataset_id, str) and dataset_id.strip():
        return dataset_id.strip()
    return None


class ApifyInstagramScraper:
    """Fetches recent Instagram posts for Concordia clubs using an Apify Actor."""

    def __init__(
        self,
        api_token: str,
        actor_id: str = DEFAULT_APIFY_ACTOR,
        client: Any | None = None,
    ) -> None:
        self.api_token = api_token
        self.actor_id = actor_id
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily construct and return the underlying ApifyClient."""
        if self._client is None:
            from apify_client import ApifyClient

            self._client = ApifyClient(self.api_token)
        return self._client

    def _normalize_item(
        self,
        item: dict[str, Any],
        clubs_by_handle: dict[str, Club],
    ) -> InstagramPost | None:
        """Convert a raw Apify dataset item into an InstagramPost, or None if invalid."""
        if not isinstance(item, dict) or item.get("error"):
            return None

        raw_ts = item.get("timestamp")
        if raw_ts is None:
            raw_ts = item.get("takenAtTimestamp")
        timestamp = _parse_timestamp(raw_ts)
        if timestamp is None:
            logger.warning("Skipping Apify item without valid timestamp: %s", item.get("id"))
            return None

        shortcode = str(
            item.get("shortCode") or item.get("shortcode") or item.get("code") or ""
        ).strip()
        post_id = str(item.get("id") or item.get("postId") or shortcode or "").strip()

        url = str(item.get("url") or "").strip()
        if not url and shortcode:
            url = f"https://www.instagram.com/p/{shortcode}/"
        if not post_id and url:
            post_id = url
        if not post_id or not url:
            logger.warning("Skipping Apify item missing both id and url.")
            return None

        raw_handle = (
            item.get("ownerUsername")
            or item.get("username")
            or _extract_handle_from_url(item.get("inputUrl"))
            or ""
        )
        club_handle = str(raw_handle).strip().lstrip("@").lower()
        if not club_handle:
            club_handle = "unknown"

        matched_club = clubs_by_handle.get(club_handle)
        club_name = (
            matched_club.name
            if matched_club is not None
            else str(item.get("ownerFullName") or club_handle).strip()
        )

        caption = str(item.get("caption") or item.get("text") or "").strip()
        image_urls = _extract_image_urls(item)
        image_url = image_urls[0] if image_urls else None

        return InstagramPost(
            post_id=post_id,
            shortcode=shortcode,
            club_handle=club_handle,
            club_name=club_name,
            caption=caption,
            url=url,
            image_url=image_url,
            image_urls=image_urls,
            timestamp=timestamp,
        )

    def fetch_recent_posts(
        self,
        clubs: list[Club],
        lookback_days: int = 3,
        max_posts_per_club: int = 3,
    ) -> list[InstagramPost]:
        """Fetch recent Instagram posts for the given clubs within ``lookback_days``.

        Args:
            clubs: Student clubs whose Instagram profiles should be scraped.
            lookback_days: Only keep posts published within this many days of now
                (America/Toronto).
            max_posts_per_club: Maximum number of posts requested per profile.

        Returns:
            Deduplicated list of ``InstagramPost`` objects sorted newest first.
        """
        if not clubs:
            return []

        effective_lookback = max(0, int(lookback_days))
        now = datetime.now(MONTREAL_TZ)
        cutoff = now - timedelta(days=effective_lookback)

        clubs_by_handle: dict[str, Club] = {club.handle.lower(): club for club in clubs}
        direct_urls = [f"https://www.instagram.com/{handle}/" for handle in clubs_by_handle]

        run_input: dict[str, Any] = {
            "directUrls": direct_urls,
            "resultsType": "posts",
            "resultsLimit": max(1, int(max_posts_per_club)),
            "onlyPostsNewerThan": cutoff.date().isoformat(),
        }

        logger.info(
            "Running Apify actor '%s' for %d clubs (lookback=%d days)",
            self.actor_id,
            len(direct_urls),
            effective_lookback,
        )
        run = self.client.actor(self.actor_id).call(run_input=run_input)
        dataset_id = _extract_dataset_id(run)
        if not dataset_id:
            logger.warning("Apify actor run returned no defaultDatasetId.")
            return []

        posts_by_id: dict[str, InstagramPost] = {}
        for item in self.client.dataset(dataset_id).iterate_items():
            post = self._normalize_item(item, clubs_by_handle)
            if post is None:
                continue
            if post.timestamp < cutoff:
                continue
            if post.post_id not in posts_by_id:
                posts_by_id[post.post_id] = post

        return sorted(posts_by_id.values(), key=lambda p: p.timestamp, reverse=True)
