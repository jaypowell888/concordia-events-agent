"""Unit and integration tests for calendar_sync.py and main.py."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import calendar_sync
import main as main_module
from calendar_sync import CALENDAR_SCOPES, GoogleCalendarSync
from config import MONTREAL_TZ, Settings
from models import ExtractedEvent, InstagramPost
from state_store import EventStateStore


# ---------------------------------------------------------------------------
# calendar_sync.py tests
# ---------------------------------------------------------------------------


def test_google_calendar_build_event_body() -> None:
    fake_service = MagicMock()
    sync = GoogleCalendarSync(
        calendar_id="concordia-events@group.calendar.google.com",
        service_account_json="./service-account.json",
        service=fake_service,
    )

    event = ExtractedEvent(
        title="SCS Linux Workshop",
        start_time=datetime(2026, 11, 25, 18, 0, tzinfo=MONTREAL_TZ),
        end_time=datetime(2026, 11, 25, 20, 0, tzinfo=MONTREAL_TZ),
        location="Hall Building, Concordia University",
        room="H-937",
        description="Hands-on intro to Linux & Git.",
        registration_url="https://scs.concordia.ca/rsvp",
        club_name="Software Engineering and Computer Science Society (SCS)",
        club_handle="scsconcordia",
        source_post_id="post-scs-1",
        source_post_url="https://www.instagram.com/p/SCS1/",
    )

    body = sync.build_event_body(event)
    assert body["summary"] == "SCS Linux Workshop"
    assert body["location"] == "H-937, Hall Building, Concordia University"
    assert body["start"]["timeZone"] == "America/Toronto"
    assert body["end"]["timeZone"] == "America/Toronto"
    assert body["start"]["dateTime"].endswith("-05:00")
    assert body["end"]["dateTime"].endswith("-05:00")
    assert "Hands-on intro to Linux & Git." in body["description"]
    assert "Hosted by: Software Engineering and Computer Science Society (SCS) (@scsconcordia)" in body["description"]
    assert "Registration: https://scs.concordia.ca/rsvp" in body["description"]
    assert "Source Instagram Post: https://www.instagram.com/p/SCS1/" in body["description"]

    # Room already present in location should not be duplicated.
    event_with_room_in_loc = event.model_copy(
        update={"location": "Hall Building H-937, Concordia University"}
    )
    body_dedup = sync.build_event_body(event_with_room_in_loc)
    assert body_dedup["location"] == "Hall Building H-937, Concordia University"


def test_google_calendar_credential_loading_inline_vs_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}
    fake_creds = object()
    fake_service = MagicMock()

    def _fake_from_info(info: dict[str, Any], *, scopes: list[str]) -> Any:
        calls["from_info"] = (info, scopes)
        return fake_creds

    def _fake_from_file(path: str, *, scopes: list[str]) -> Any:
        calls["from_file"] = (path, scopes)
        return fake_creds

    def _fake_build(api: str, version: str, *, credentials: Any, cache_discovery: bool) -> Any:
        calls["build"] = (api, version, credentials, cache_discovery)
        return fake_service

    monkeypatch.setattr(calendar_sync.Credentials, "from_service_account_info", _fake_from_info)
    monkeypatch.setattr(calendar_sync.Credentials, "from_service_account_file", _fake_from_file)
    monkeypatch.setattr(calendar_sync, "build", _fake_build)

    # 1. Inline JSON string (e.g., from GitHub Actions secret)
    inline_json = json.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com"})
    GoogleCalendarSync(calendar_id="cal-1", service_account_json=inline_json)
    assert calls["from_info"][0]["client_email"] == "bot@proj.iam.gserviceaccount.com"
    assert calls["from_info"][1] == CALENDAR_SCOPES
    assert calls["build"][:2] == ("calendar", "v3")

    # 2. File path
    cred_file = tmp_path / "service-account.json"
    cred_file.write_text(inline_json, encoding="utf-8")
    GoogleCalendarSync(calendar_id="cal-1", service_account_json=str(cred_file))
    assert calls["from_file"][0] == str(cred_file)
    assert calls["from_file"][1] == CALENDAR_SCOPES


def test_google_calendar_create_and_update_calls() -> None:
    fake_service = MagicMock()
    fake_service.events.return_value.insert.return_value.execute.return_value = {"id": "gcal-123"}
    fake_service.events.return_value.update.return_value.execute.return_value = {"id": "gcal-123"}

    sync = GoogleCalendarSync(
        calendar_id="cal-concordia@group.calendar.google.com",
        service_account_json="{}",
        service=fake_service,
    )
    event = ExtractedEvent(
        title="JMSB Case Info Session",
        start_time=datetime(2026, 11, 26, 17, 0, tzinfo=MONTREAL_TZ),
        room="MB S2.330",
        club_handle="jmcc_jmsb",
        source_post_id="p-jmcc-1",
    )

    created_id = sync.create_event(event)
    assert created_id == "gcal-123"
    insert_kwargs = fake_service.events.return_value.insert.call_args.kwargs
    assert insert_kwargs["calendarId"] == "cal-concordia@group.calendar.google.com"
    assert insert_kwargs["body"]["summary"] == "JMSB Case Info Session"

    updated_id = sync.update_event("gcal-123", event)
    assert updated_id == "gcal-123"
    update_kwargs = fake_service.events.return_value.update.call_args.kwargs
    assert update_kwargs["calendarId"] == "cal-concordia@group.calendar.google.com"
    assert update_kwargs["eventId"] == "gcal-123"


# ---------------------------------------------------------------------------
# main.py tests
# ---------------------------------------------------------------------------


def _write_sample_clubs(path: Path) -> Path:
    path.write_text(
        json.dumps([{"name": "CASA-JMSB", "handle": "casajmsb", "faculty": "JMSB"}]),
        encoding="utf-8",
    )
    return path


class _FakeScraper:
    def __init__(self, posts: list[InstagramPost]) -> None:
        self.posts = posts

    def fetch_recent_posts(self, clubs: list[Any], lookback_days: int = 3) -> list[InstagramPost]:
        return list(self.posts)


class _FakeExtractor:
    def __init__(self, events_by_post: dict[str, list[ExtractedEvent]]) -> None:
        self.events_by_post = events_by_post

    def extract_events(self, post: InstagramPost) -> list[ExtractedEvent]:
        val = self.events_by_post[post.post_id]
        return list(val)


class _FakeCalendar:
    def __init__(self) -> None:
        self.created: list[tuple[str, ExtractedEvent]] = []
        self.updated: list[tuple[str, ExtractedEvent]] = []
        self._counter = 0

    def create_event(self, event: ExtractedEvent) -> str:
        self._counter += 1
        event_id = f"gcal-evt-{self._counter}"
        self.created.append((event_id, event))
        return event_id

    def update_event(self, google_event_id: str, event: ExtractedEvent) -> str:
        self.updated.append((google_event_id, event))
        return google_event_id


def test_dry_run_prints_create_without_creating_db_or_calling_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clubs_path = _write_sample_clubs(tmp_path / "clubs.json")
    db_path = tmp_path / "state" / "events.db"

    def _forbid_calendar(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("GoogleCalendarSync must NOT be instantiated during --dry-run")

    monkeypatch.setattr(main_module, "GoogleCalendarSync", _forbid_calendar)

    post = InstagramPost(
        post_id="post-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Case workshop in H-937!",
        url="https://www.instagram.com/p/POST1/",
        timestamp=datetime(2026, 11, 22, 12, 0, tzinfo=MONTREAL_TZ),
    )
    event = ExtractedEvent(
        title="CASA Case Workshop",
        start_time=datetime(2026, 11, 25, 18, 0, tzinfo=MONTREAL_TZ),
        location="Hall Building, Concordia University",
        room="H-937",
        club_name="CASA-JMSB",
        club_handle="casajmsb",
        source_post_id="post-1",
        source_post_url=post.url,
    )

    settings = Settings(
        gemini_api_key="fake-gem",
        apify_token="fake-apify",
        clubs_path=clubs_path,
        db_path=db_path,
    )

    stats = main_module.sync_events(
        settings=settings,
        dry_run=True,
        scraper=_FakeScraper([post]),
        extractor=_FakeExtractor({"post-1": [event]}),
    )

    captured = capsys.readouterr()
    assert "[CREATE] CASA Case Workshop | 2026-11-25 18:00 EST | H-937 | @casajmsb" in captured.out
    assert "[DRY RUN] Summary:" in captured.out
    assert stats == {"posts_scanned": 1, "created": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_live_run_create_skip_and_update_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clubs_path = _write_sample_clubs(tmp_path / "clubs.json")
    db_path = tmp_path / "state" / "events.db"

    post = InstagramPost(
        post_id="post-live-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Networking mixer in MB S2.330 at 6 PM",
        url="https://www.instagram.com/p/LIVE1/",
        timestamp=datetime(2026, 11, 22, 14, 0, tzinfo=MONTREAL_TZ),
    )
    event_v1 = ExtractedEvent(
        title="JMSB Networking Mixer",
        start_time=datetime(2026, 11, 26, 18, 0, tzinfo=MONTREAL_TZ),
        location="John Molson Building, Concordia University",
        room="MB S2.330",
        club_name="CASA-JMSB",
        club_handle="casajmsb",
        source_post_id="post-live-1",
        source_post_url=post.url,
    )
    settings = Settings(
        gemini_api_key="fake-gem",
        apify_token="fake-apify",
        google_calendar_id="cal@group.calendar.google.com",
        google_service_account_json="./service-account.json",
        clubs_path=clubs_path,
        db_path=db_path,
    )
    calendar = _FakeCalendar()

    # 1) First live run -> [CREATE] and writes state/events.db
    stats1 = main_module.sync_events(
        settings=settings,
        dry_run=False,
        scraper=_FakeScraper([post]),
        extractor=_FakeExtractor({"post-live-1": [event_v1]}),
        calendar=calendar,
    )
    out1 = capsys.readouterr().out
    assert "[CREATE] JMSB Networking Mixer | 2026-11-26 18:00 EST | MB S2.330 | @casajmsb" in out1
    assert stats1 == {"posts_scanned": 1, "created": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert db_path.exists()
    assert len(calendar.created) == 1
    created_google_id = calendar.created[0][0]

    # Verify DB state persisted cleanly.
    with EventStateStore(db_path, read_only=True) as ro_store:
        assert ro_store.is_post_processed("post-live-1") is True
        saved = ro_store.get_synced_event(event_v1.fingerprint())
        assert saved is not None
        assert saved["google_event_id"] == created_google_id

    # 2a) Second live run (default) -> already-processed post skipped without duplicate API calls
    stats2a = main_module.sync_events(
        settings=settings,
        dry_run=False,
        scraper=_FakeScraper([post]),
        extractor=_FakeExtractor({"post-live-1": [event_v1]}),
        calendar=calendar,
    )
    capsys.readouterr()
    assert stats2a == {"posts_scanned": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    assert len(calendar.created) == 1
    assert len(calendar.updated) == 0

    # 2b) Second live run with reprocess_posts=True -> [SKIP]
    stats2b = main_module.sync_events(
        settings=settings,
        dry_run=False,
        reprocess_posts=True,
        scraper=_FakeScraper([post]),
        extractor=_FakeExtractor({"post-live-1": [event_v1]}),
        calendar=calendar,
    )
    out2b = capsys.readouterr().out
    assert "[SKIP] JMSB Networking Mixer | 2026-11-26 18:00 EST | MB S2.330 | @casajmsb" in out2b
    assert stats2b == {"posts_scanned": 1, "created": 0, "updated": 0, "skipped": 1, "errors": 0}
    assert len(calendar.created) == 1
    assert len(calendar.updated) == 0

    # 3) Edited event (room + time changed) with reprocess_posts=True -> [UPDATE]
    event_v2 = event_v1.model_copy(
        update={
            "room": "MB 9th Floor",
            "start_time": datetime(2026, 11, 26, 19, 0, tzinfo=MONTREAL_TZ),
            "end_time": datetime(2026, 11, 26, 21, 0, tzinfo=MONTREAL_TZ),
        }
    )
    stats3 = main_module.sync_events(
        settings=settings,
        dry_run=False,
        reprocess_posts=True,
        scraper=_FakeScraper([post]),
        extractor=_FakeExtractor({"post-live-1": [event_v2]}),
        calendar=calendar,
    )
    out3 = capsys.readouterr().out
    assert "[UPDATE] JMSB Networking Mixer | 2026-11-26 19:00 EST | MB 9th Floor | @casajmsb" in out3
    assert stats3 == {"posts_scanned": 1, "created": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert len(calendar.updated) == 1
    assert calendar.updated[0][0] == created_google_id

    # Verify DB hash updated to match v2.
    with EventStateStore(db_path, read_only=True) as ro_store:
        saved_v2 = ro_store.get_synced_event(event_v2.fingerprint())
        assert saved_v2 is not None
        assert saved_v2["content_hash"] == event_v2.content_hash()


def test_main_cli_arg_parser_and_missing_env_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = main_module.build_arg_parser()
    args = parser.parse_args(
        [
            "--dry-run",
            "--lookback-days",
            "5",
            "--clubs-file",
            "custom.json",
            "--db-path",
            "state/custom.db",
            "--limit",
            "10",
            "--max-clubs",
            "5",
            "--reprocess-posts",
        ]
    )
    assert args.dry_run is True
    assert args.lookback_days == 5
    assert args.clubs_file == Path("custom.json")
    assert args.db_path == Path("state/custom.db")
    assert args.limit == 10
    assert args.max_clubs == 5
    assert args.reprocess_posts is True

    for var in (
        "GEMINI_API_KEY",
        "APIFY_TOKEN",
        "GOOGLE_CALENDAR_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("config.load_dotenv", lambda *a, **k: False)

    exit_code = main_module.main(["--dry-run"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "GEMINI_API_KEY" in err
    assert "APIFY_TOKEN" in err


def test_dry_run_against_existing_db_is_read_only_and_isolates_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clubs_path = _write_sample_clubs(tmp_path / "clubs.json")
    db_path = tmp_path / "state" / "events.db"

    existing_event = ExtractedEvent(
        title="Existing Workshop",
        start_time=datetime(2026, 11, 25, 18, 0, tzinfo=MONTREAL_TZ),
        room="H-937",
        club_handle="casajmsb",
        source_post_id="p-existing",
        source_post_url="https://www.instagram.com/p/EX1/",
    )
    with EventStateStore(db_path) as store:
        store.upsert_synced_event(existing_event, "gcal-existing")
        store.mark_post_processed("p-existing", "casajmsb", 1)

    posts = [
        InstagramPost(
            post_id="p-existing",
            club_handle="casajmsb",
            caption="Existing Workshop",
            url="https://www.instagram.com/p/EX1/",
            timestamp=datetime(2026, 11, 22, 12, 0, tzinfo=MONTREAL_TZ),
        ),
        InstagramPost(
            post_id="p-bad",
            club_handle="casajmsb",
            caption="Broken post",
            url="https://www.instagram.com/p/BAD/",
            timestamp=datetime(2026, 11, 22, 13, 0, tzinfo=MONTREAL_TZ),
        ),
        InstagramPost(
            post_id="p-new",
            club_handle="casajmsb",
            caption="New Talk",
            url="https://www.instagram.com/p/NEW/",
            timestamp=datetime(2026, 11, 22, 14, 0, tzinfo=MONTREAL_TZ),
        ),
    ]
    new_event = ExtractedEvent(
        title="New AI Talk",
        start_time=datetime(2026, 11, 29, 17, 0, tzinfo=MONTREAL_TZ),
        room="EV 1.605",
        club_handle="casajmsb",
        source_post_id="p-new",
        source_post_url="https://www.instagram.com/p/NEW/",
    )

    class _FlakyExtractor:
        def extract_events(self, post: InstagramPost) -> list[ExtractedEvent]:
            if post.post_id == "p-bad":
                raise RuntimeError("Gemini rate limit")
            if post.post_id == "p-existing":
                return [existing_event]
            return [new_event]

    settings = Settings(
        gemini_api_key="fake",
        apify_token="fake",
        clubs_path=clubs_path,
        db_path=db_path,
    )

    stats = main_module.sync_events(
        settings=settings,
        dry_run=True,
        reprocess_posts=True,
        scraper=_FakeScraper(posts),
        extractor=_FlakyExtractor(),
    )
    captured = capsys.readouterr()
    assert "[SKIP] Existing Workshop" in captured.out
    assert "[CREATE] New AI Talk | 2026-11-29 17:00 EST | EV 1.605 | @casajmsb" in captured.out
    assert "[ERROR] Extraction failed for post p-bad" in captured.err
    assert stats == {"posts_scanned": 3, "created": 1, "updated": 0, "skipped": 1, "errors": 1}

    # Verify p-new was NOT persisted into the DB during dry-run.
    with EventStateStore(db_path, read_only=True) as ro_store:
        assert ro_store.is_post_processed("p-new") is False
        assert ro_store.get_synced_event(new_event.fingerprint()) is None


def test_sync_events_wires_gemini_4_thinking_and_escalation_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clubs_path = _write_sample_clubs(tmp_path / "clubs.json")
    db_path = tmp_path / "state" / "events.db"

    captured_kwargs: dict[str, Any] = {}

    class _SpyExtractor:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

        def extract_events(self, post: InstagramPost) -> list[ExtractedEvent]:
            return []

    monkeypatch.setattr(main_module, "GeminiEventExtractor", _SpyExtractor)

    settings = Settings(
        gemini_api_key="gem-secret",
        apify_token="apify-secret",
        gemini_model="gemini-4-hydrogen",
        gemini_thinking_level="medium",
        gemini_escalation_model="gemini-4-neon",
        gemini_escalation_thinking_level="high",
        gemini_fallback_model="gemini-3.5-flash-lite",
        clubs_path=clubs_path,
        db_path=db_path,
    )

    main_module.sync_events(
        settings=settings,
        dry_run=True,
        scraper=_FakeScraper([]),
    )

    assert captured_kwargs == {
        "api_key": "gem-secret",
        "model": "gemini-4-hydrogen",
        "thinking_level": "medium",
        "escalation_model": "gemini-4-neon",
        "escalation_thinking_level": "high",
        "fallback_model": "gemini-3.5-flash-lite",
    }


def test_sync_events_max_clubs_slices_before_scraping_and_validates(
    tmp_path: Path,
) -> None:
    clubs_path = tmp_path / "clubs.json"
    clubs_path.write_text(
        json.dumps(
            [
                {"name": "CSU", "handle": "csu_concordia", "faculty": "General"},
                {"name": "CASA-JMSB", "handle": "casajmsb", "faculty": "JMSB"},
                {"name": "ECA", "handle": "ecaconcordia", "faculty": "Gina Cody"},
                {"name": "ASFA", "handle": "asfaconcordia", "faculty": "Arts & Science"},
            ]
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "state" / "events.db"
    settings = Settings(
        gemini_api_key="fake",
        apify_token="fake",
        clubs_path=clubs_path,
        db_path=db_path,
    )

    scraped_handles: list[str] = []

    class _RecordingScraper:
        def fetch_recent_posts(self, clubs: list[Any], lookback_days: int = 3) -> list[InstagramPost]:
            scraped_handles.extend(c.handle for c in clubs)
            return []

    main_module.sync_events(
        settings=settings,
        dry_run=True,
        max_clubs=2,
        scraper=_RecordingScraper(),
        extractor=_FakeExtractor({}),
    )
    assert scraped_handles == ["csu_concordia", "casajmsb"]

    with pytest.raises(ValueError, match="--max-clubs"):
        main_module.sync_events(
            settings=settings,
            dry_run=True,
            max_clubs=-1,
            scraper=_RecordingScraper(),
            extractor=_FakeExtractor({}),
        )


def test_save_posts_and_from_posts_skip_apify_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clubs_path = _write_sample_clubs(tmp_path / "clubs.json")
    cache_path = tmp_path / "cache" / "posts.json"
    db_path = tmp_path / "state" / "events.db"

    posts = [
        InstagramPost(
            post_id=f"post-{i}",
            club_handle="casajmsb",
            club_name="CASA-JMSB",
            caption=f"Event #{i} in H-937",
            url=f"https://www.instagram.com/p/P{i}/",
            image_url=f"https://cdn.example.com/{i}.jpg",
            image_urls=[f"https://cdn.example.com/{i}.jpg"],
            timestamp=datetime(2026, 11, 22, 12 + i, 0, tzinfo=MONTREAL_TZ),
        )
        for i in range(1, 4)
    ]
    events_by_post = {
        p.post_id: [
            ExtractedEvent(
                title=f"Title {p.post_id}",
                start_time=datetime(2026, 11, 26, 18, 0, tzinfo=MONTREAL_TZ),
                room="H-937",
                club_handle="casajmsb",
                source_post_id=p.post_id,
                source_post_url=p.url,
            )
        ]
        for p in posts
    }

    settings = Settings(
        gemini_api_key="gem-key",
        apify_token="apify-tok",
        clubs_path=clubs_path,
        db_path=db_path,
    )

    # 1. --save-posts saves ALL 3 scraped posts BEFORE applying --limit 1
    stats_save = main_module.sync_events(
        settings=settings,
        dry_run=True,
        limit=1,
        save_posts=cache_path,
        scraper=_FakeScraper(posts),
        extractor=_FakeExtractor(events_by_post),
    )
    out_save = capsys.readouterr().out
    assert "[CACHE] Saved 3 scraped posts" in out_save
    assert stats_save["posts_scanned"] == 1
    assert stats_save["created"] == 1
    assert cache_path.exists()
    saved_json = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(saved_json) == 3

    # 2. --from-posts skips load_clubs & Apify completely and works without APIFY_TOKEN
    def _forbid_apify(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ApifyInstagramScraper must NOT be instantiated with --from-posts")

    def _forbid_load_clubs(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("load_clubs must NOT be called with --from-posts")

    monkeypatch.setattr(main_module, "ApifyInstagramScraper", _forbid_apify)
    monkeypatch.setattr(main_module, "load_clubs", _forbid_load_clubs)

    no_apify_settings = Settings(
        gemini_api_key="gem-key",
        apify_token="",
        clubs_path=tmp_path / "nonexistent_clubs.json",
        db_path=db_path,
    )
    stats_from = main_module.sync_events(
        settings=no_apify_settings,
        dry_run=True,
        limit=2,
        from_posts=cache_path,
        extractor=_FakeExtractor(events_by_post),
    )
    capsys.readouterr()
    assert stats_from["posts_scanned"] == 2
    assert stats_from["created"] == 2

    # Missing --from-posts file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        main_module.sync_events(
            settings=no_apify_settings,
            dry_run=True,
            from_posts=tmp_path / "missing_cache.json",
            extractor=_FakeExtractor({}),
        )

    # 3. CLI main() with --from-posts succeeds without APIFY_TOKEN in env
    for var in ("APIFY_TOKEN", "GOOGLE_CALENDAR_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(
        main_module,
        "GeminiEventExtractor",
        lambda **kw: _FakeExtractor(events_by_post),
    )

    rc = main_module.main(
        ["--dry-run", "--from-posts", str(cache_path), "--limit", "1", "--db-path", str(db_path)]
    )
    assert rc == 0
    assert not db_path.exists()




