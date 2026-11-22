"""Main CLI orchestrator for scraping Concordia club posts and syncing events to Google Calendar."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from calendar_sync import GoogleCalendarSync
from config import Settings, load_settings
from extractor import GeminiEventExtractor
from models import Club, InstagramPost
from scraper import ApifyInstagramScraper, load_clubs
from state_store import EventStateStore

logger = logging.getLogger(__name__)


def _load_posts_from_file(posts_path: Path) -> list[InstagramPost]:
    """Load previously saved InstagramPost records from a local JSON file."""
    path = Path(posts_path)
    if not path.is_file():
        raise FileNotFoundError(f"Saved posts file not found: {path}")

    raw_data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw_data, dict) and isinstance(raw_data.get("posts"), list):
        raw_items = raw_data["posts"]
    elif isinstance(raw_data, list):
        raw_items = raw_data
    else:
        raise ValueError(
            f"Invalid saved posts JSON format in '{path}': expected a JSON list of posts."
        )

    return [InstagramPost.model_validate(item) for item in raw_items]


def _save_posts_to_file(posts: list[InstagramPost], posts_path: Path) -> None:
    """Persist scraped InstagramPost records to a local JSON file."""
    path = Path(posts_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [post.model_dump(mode="json") for post in posts]
    path.write_text(json.dumps(serialized, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved %d scraped posts to %s", len(posts), path)
    print(f"[CACHE] Saved {len(posts)} scraped posts to {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for the Concordia Events sync pipeline."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scrape recent Concordia & JMSB club Instagram posts, extract events with Gemini, and sync to Google Calendar.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview extracted events without writing to Google Calendar or modifying SQLite state.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Number of days to look back for Instagram posts (overrides LOOKBACK_DAYS).",
    )
    parser.add_argument(
        "--clubs-file",
        type=Path,
        default=None,
        help="Path to the clubs JSON file (default: clubs.json).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to the SQLite deduplication database (default: state/events.db).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of recent posts to process.",
    )
    parser.add_argument(
        "--max-clubs",
        type=int,
        default=None,
        help="Optional maximum number of clubs from clubs.json to scrape.",
    )
    parser.add_argument(
        "--save-posts",
        type=Path,
        default=None,
        help="Save fetched Apify posts to a local JSON file before applying --limit.",
    )
    parser.add_argument(
        "--from-posts",
        type=Path,
        default=None,
        help="Load previously saved Instagram posts from a local JSON file and skip Apify.",
    )
    parser.add_argument(
        "--reprocess-posts",
        action="store_true",
        help="Re-scan posts even if their post ID was already recorded in the state database.",
    )
    parser.add_argument(
        "--sync-colors",
        action="store_true",
        help="Patch Google Calendar colorId on existing synced events using clubs file colors (no Apify or Gemini calls).",
    )
    return parser


def sync_events(
    settings: Settings,
    dry_run: bool = False,
    limit: int | None = None,
    reprocess_posts: bool = False,
    max_clubs: int | None = None,
    save_posts: Path | None = None,
    from_posts: Path | None = None,
    scraper: Any | None = None,
    extractor: Any | None = None,
    calendar: Any | None = None,
    store: EventStateStore | None = None,
) -> dict[str, int]:
    """Run the end-to-end scrape -> extract -> deduplicate -> calendar sync pipeline."""
    if limit is not None and limit < 0:
        raise ValueError("--limit must be a non-negative integer.")
    if max_clubs is not None and max_clubs < 0:
        raise ValueError("--max-clubs must be a non-negative integer.")

    clubs: list[Club] = []
    clubs_by_handle: dict[str, Club] = {}
    cached_posts: list[InstagramPost] | None = None
    active_scraper: Any | None = None
    if from_posts is not None:
        cached_posts = _load_posts_from_file(from_posts)
        if Path(settings.clubs_path).exists():
            clubs_by_handle = {
                club.handle.lower(): club for club in load_clubs(settings.clubs_path)
            }
    else:
        clubs = load_clubs(settings.clubs_path)
        clubs_by_handle = {club.handle.lower(): club for club in clubs}
        if max_clubs is not None:
            clubs = clubs[:max_clubs]
        active_scraper = (
            scraper
            if scraper is not None
            else ApifyInstagramScraper(api_token=settings.apify_token, actor_id=settings.apify_actor)
        )

    owns_store = False
    active_store = store
    if active_store is None:
        if not dry_run:
            active_store = EventStateStore(settings.db_path)
            owns_store = True
        elif Path(settings.db_path).exists():
            active_store = EventStateStore(settings.db_path, read_only=True)
            owns_store = True

    active_extractor = (
        extractor
        if extractor is not None
        else GeminiEventExtractor(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            thinking_level=settings.gemini_thinking_level,
            escalation_model=settings.gemini_escalation_model,
            escalation_thinking_level=settings.gemini_escalation_thinking_level,
            fallback_model=settings.gemini_fallback_model,
        )
    )
    active_calendar = calendar
    if not dry_run and active_calendar is None:
        active_calendar = GoogleCalendarSync(
            calendar_id=settings.google_calendar_id,
            service_account_json=settings.google_service_account_json,
        )

    stats: dict[str, int] = {
        "posts_scanned": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        if cached_posts is not None:
            posts = cached_posts
        else:
            posts = active_scraper.fetch_recent_posts(clubs, lookback_days=settings.lookback_days)
            if save_posts is not None:
                _save_posts_to_file(posts, save_posts)

        if limit is not None:
            posts = posts[:limit]

        for post in posts:
            if (
                not reprocess_posts
                and active_store is not None
                and active_store.is_post_processed(post.post_id)
            ):
                continue

            stats["posts_scanned"] += 1

            try:
                extracted_events = active_extractor.extract_events(post)
            except Exception as exc:
                logger.warning("Failed to extract events for post %s (@%s): %s", post.post_id, post.club_handle, exc)
                print(
                    f"[ERROR] Extraction failed for post {post.post_id} (@{post.club_handle}): {exc}",
                    file=sys.stderr,
                )
                stats["errors"] += 1
                continue

            post_had_error = False
            valid_events = [event for event in extracted_events if getattr(event, "is_event", True)]

            for event in valid_events:
                try:
                    if not event.color_id:
                        lookup_handle = (
                            (event.club_handle or post.club_handle).strip().lstrip("@").lower()
                        )
                        matched_club = clubs_by_handle.get(lookup_handle)
                        if matched_club is not None and matched_club.color_id:
                            event = event.model_copy(update={"color_id": matched_club.color_id})

                    existing = (
                        active_store.get_synced_event(event.fingerprint())
                        if active_store is not None
                        else None
                    )

                    if existing is None:
                        print(event.format_summary_line("CREATE"))
                        if not dry_run:
                            google_event_id = active_calendar.create_event(event)
                            if active_store is not None:
                                active_store.upsert_synced_event(event, google_event_id)
                        stats["created"] += 1
                    elif existing["content_hash"] != event.content_hash():
                        print(event.format_summary_line("UPDATE"))
                        if not dry_run:
                            google_event_id = active_calendar.update_event(
                                existing["google_event_id"], event
                            )
                            if active_store is not None:
                                active_store.upsert_synced_event(event, google_event_id)
                        stats["updated"] += 1
                    else:
                        print(event.format_summary_line("SKIP"))
                        stats["skipped"] += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to sync event '%s' from post %s: %s",
                        getattr(event, "title", "<unknown>"),
                        post.post_id,
                        exc,
                    )
                    print(
                        f"[ERROR] Sync failed for event '{getattr(event, 'title', '<unknown>')}': {exc}",
                        file=sys.stderr,
                    )
                    stats["errors"] += 1
                    post_had_error = True

            if not dry_run and active_store is not None and not post_had_error:
                active_store.mark_post_processed(post.post_id, post.club_handle, len(valid_events))
    finally:
        if owns_store and active_store is not None:
            active_store.close()

    mode_label = "DRY RUN" if dry_run else "LIVE"
    print(
        f"[{mode_label}] Summary: "
        f"posts_scanned={stats['posts_scanned']}, "
        f"created={stats['created']}, "
        f"updated={stats['updated']}, "
        f"skipped={stats['skipped']}, "
        f"errors={stats['errors']}"
    )
    return stats


def sync_colors(
    settings: Settings,
    dry_run: bool = False,
    calendar: Any | None = None,
    store: EventStateStore | None = None,
) -> dict[str, int]:
    """Patch Google Calendar colorId on existing synced events without calling Apify or Gemini."""
    clubs = load_clubs(settings.clubs_path)
    color_by_handle: dict[str, str] = {
        club.handle.lower(): club.color_id
        for club in clubs
        if club.color_id
    }

    owns_store = False
    active_store = store
    if active_store is None:
        active_store = EventStateStore(settings.db_path, read_only=True)
        owns_store = True

    active_calendar = calendar
    if not dry_run and active_calendar is None:
        active_calendar = GoogleCalendarSync(
            calendar_id=settings.google_calendar_id,
            service_account_json=settings.google_service_account_json,
        )

    stats: dict[str, int] = {
        "events_checked": 0,
        "patched": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        synced_events = active_store.list_synced_events_with_club()
        for row in synced_events:
            stats["events_checked"] += 1
            club_handle = str(row.get("club_handle") or "").strip().lstrip("@").lower()
            title = str(row.get("title") or "")
            start_time = str(row.get("start_time") or "")
            google_event_id = str(row.get("google_event_id") or "")

            color_id = color_by_handle.get(club_handle)
            if not color_id:
                handle_display = f"@{club_handle}" if club_handle else "@unknown"
                print(f"[SKIP-COLOR] {title} | {start_time} | {handle_display}")
                stats["skipped"] += 1
                continue

            print(f"[COLOR] {title} | {start_time} | colorId={color_id} | @{club_handle}")
            if not dry_run:
                try:
                    active_calendar.patch_event_color(google_event_id, color_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to patch colorId=%s on event '%s' (%s): %s",
                        color_id,
                        title,
                        google_event_id,
                        exc,
                    )
                    print(
                        f"[ERROR] Color patch failed for event '{title}' ({google_event_id}): {exc}",
                        file=sys.stderr,
                    )
                    stats["errors"] += 1
                    continue
            stats["patched"] += 1
    finally:
        if owns_store and active_store is not None:
            active_store.close()

    mode_label = "DRY RUN" if dry_run else "LIVE"
    print(
        f"[{mode_label} COLORS] Summary: "
        f"events_checked={stats['events_checked']}, "
        f"patched={stats['patched']}, "
        f"skipped={stats['skipped']}, "
        f"errors={stats['errors']}"
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(
            lookback_days=args.lookback_days,
            clubs_path=args.clubs_file,
            db_path=args.db_path,
        )
        if args.sync_colors:
            settings.validate_for_run(
                dry_run=args.dry_run,
                require_apify=False,
                require_gemini=False,
            )
        else:
            settings.validate_for_run(
                dry_run=args.dry_run,
                require_apify=(args.from_posts is None),
            )
    except ValueError as exc:
        print(
            f"Configuration error: {exc}\n"
            "Ensure required environment variables are configured in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1

    try:
        if args.sync_colors:
            sync_colors(
                settings=settings,
                dry_run=args.dry_run,
            )
        else:
            sync_events(
                settings=settings,
                dry_run=args.dry_run,
                limit=args.limit,
                reprocess_posts=args.reprocess_posts,
                max_clubs=args.max_clubs,
                save_posts=args.save_posts,
                from_posts=args.from_posts,
            )
    except FileNotFoundError as exc:
        print(f"File error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
