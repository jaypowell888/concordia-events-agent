"""Unit tests for config.py, models.py, and state_store.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from config import (
    DEFAULT_APIFY_ACTOR,
    DEFAULT_CLUBS_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_GEMINI_ESCALATION_MODEL,
    DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL,
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_THINKING_LEVEL,
    DEFAULT_LOOKBACK_DAYS,
    MONTREAL_TZ,
    Settings,
    load_settings,
)
from models import Club, ExtractedEvent, InstagramPost
from state_store import EventStateStore


# ---------------------------------------------------------------------------
# config.py tests
# ---------------------------------------------------------------------------


def test_config_constants() -> None:
    assert MONTREAL_TZ == ZoneInfo("America/Toronto")
    assert DEFAULT_CLUBS_PATH == Path("clubs.json")
    assert DEFAULT_DB_PATH == Path("state/events.db")
    assert DEFAULT_LOOKBACK_DAYS == 3
    assert DEFAULT_GEMINI_MODEL == "gemini-4-hydrogen"
    assert DEFAULT_GEMINI_THINKING_LEVEL == "medium"
    assert DEFAULT_GEMINI_ESCALATION_MODEL == ""
    assert DEFAULT_GEMINI_ESCALATION_THINKING_LEVEL == "high"
    assert DEFAULT_GEMINI_FALLBACK_MODEL == "gemini-3.5-flash-lite"
    assert DEFAULT_APIFY_ACTOR == "apify/instagram-scraper"


def test_load_settings_from_env_file_and_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "GEMINI_API_KEY",
        "APIFY_TOKEN",
        "GOOGLE_CALENDAR_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "LOOKBACK_DAYS",
        "GEMINI_MODEL",
        "GEMINI_THINKING_LEVEL",
        "GEMINI_ESCALATION_MODEL",
        "GEMINI_ESCALATION_THINKING_LEVEL",
        "GEMINI_FALLBACK_MODEL",
        "APIFY_ACTOR",
        "CLUBS_PATH",
        "DB_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    # Defaults when optional Gemini env vars are absent
    minimal_env = tmp_path / ".env.minimal"
    minimal_env.write_text('GEMINI_API_KEY="k"\nAPIFY_TOKEN="t"\n', encoding="utf-8")
    defaults = load_settings(env_file=minimal_env)
    assert defaults.gemini_model == "gemini-4-hydrogen"
    assert defaults.gemini_thinking_level == "medium"
    assert defaults.gemini_escalation_model == ""
    assert defaults.gemini_escalation_thinking_level == "high"
    assert defaults.gemini_fallback_model == "gemini-3.5-flash-lite"

    # Explicit empty GEMINI_FALLBACK_MODEL="" disables fallback
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "")
    disabled_fallback = load_settings(env_file=minimal_env)
    assert disabled_fallback.gemini_fallback_model == ""

    for var in ("GEMINI_API_KEY", "APIFY_TOKEN", "GEMINI_FALLBACK_MODEL"):
        monkeypatch.delenv(var, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'GEMINI_API_KEY="gem-key-123"',
                'APIFY_TOKEN="apify-tok-456"',
                'GOOGLE_CALENDAR_ID="cal-id@group.calendar.google.com"',
                'GOOGLE_SERVICE_ACCOUNT_JSON="./creds/sa.json"',
                'LOOKBACK_DAYS="5"',
                'GEMINI_MODEL="gemini-4-hydrogen"',
                'GEMINI_THINKING_LEVEL="low"',
                'GEMINI_ESCALATION_MODEL="gemini-4-neon"',
                'GEMINI_ESCALATION_THINKING_LEVEL="high"',
                'GEMINI_FALLBACK_MODEL="gemini-2.5-pro"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)
    assert settings.gemini_api_key == "gem-key-123"
    assert settings.apify_token == "apify-tok-456"
    assert settings.google_calendar_id == "cal-id@group.calendar.google.com"
    assert settings.google_service_account_json == "./creds/sa.json"
    assert settings.lookback_days == 5
    assert settings.gemini_model == "gemini-4-hydrogen"
    assert settings.gemini_thinking_level == "low"
    assert settings.gemini_escalation_model == "gemini-4-neon"
    assert settings.gemini_escalation_thinking_level == "high"
    assert settings.gemini_fallback_model == "gemini-2.5-pro"
    assert settings.clubs_path == DEFAULT_CLUBS_PATH
    assert settings.db_path == DEFAULT_DB_PATH

    custom_clubs = tmp_path / "custom_clubs.json"
    custom_db = tmp_path / "nested" / "custom.db"
    overridden = load_settings(
        env_file=env_file,
        lookback_days=7,
        clubs_path=custom_clubs,
        db_path=custom_db,
    )
    assert overridden.lookback_days == 7
    assert overridden.clubs_path == custom_clubs
    assert overridden.db_path == custom_db


def test_env_example_documents_gemini_4_vars() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert 'GEMINI_MODEL="gemini-4-hydrogen"' in env_example
    assert 'GEMINI_THINKING_LEVEL="medium"' in env_example
    assert 'GEMINI_ESCALATION_MODEL=""' in env_example
    assert 'GEMINI_ESCALATION_THINKING_LEVEL="high"' in env_example
    assert 'GEMINI_FALLBACK_MODEL="gemini-3.5-flash-lite"' in env_example
    assert 'CLUBS_PATH="my_clubs.json"' in env_example


def test_clubs_path_env_workflow_gitignore_and_my_clubs_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper import load_clubs

    repo_root = Path(__file__).resolve().parent.parent

    # 1. config.load_settings() reads CLUBS_PATH from env and supports explicit override
    monkeypatch.setenv("CLUBS_PATH", "my_clubs.json")
    monkeypatch.setattr("config.load_dotenv", lambda *a, **k: False)
    from_env = load_settings()
    assert from_env.clubs_path == Path("my_clubs.json")
    explicit = load_settings(clubs_path=Path("other.json"))
    assert explicit.clubs_path == Path("other.json")

    # 2. Workflow sets CLUBS_PATH: my_clubs.json and GEMINI_FALLBACK_MODEL: gemini-3.5-flash-lite
    workflow_text = (repo_root / ".github" / "workflows" / "daily_sync.yml").read_text(
        encoding="utf-8"
    )
    assert "CLUBS_PATH: my_clubs.json" in workflow_text
    assert "GEMINI_FALLBACK_MODEL: gemini-3.5-flash-lite" in workflow_text

    # 3. .gitignore ignores state/last_scrape.json while keeping state/events.db unignored
    gitignore_lines = [
        line.strip()
        for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "state/last_scrape.json" in gitignore_lines
    assert "state/events.db" not in gitignore_lines

    # 4. my_clubs.json validates cleanly via load_clubs() and matches the exact 20 clubs
    clubs = load_clubs(repo_root / "my_clubs.json")
    assert len(clubs) == 20
    expected = [
        ("CASA-JMSB", "casajmsb"),
        ("JMAS", "jmasofficial"),
        ("EngComm", "engcommofficial"),
        ("BIA JMSB", "biajmsb"),
        ("Concordia Relay For Life", "concordiarelay"),
        ("CBV Institute", "cbvinstitute"),
        ("Concordia Real Estate Club", "concordiarealestateclub"),
        ("FISA", "fisajmsb"),
        ("JMCC", "jmcconline"),
        ("REIMI", "reimijmsb"),
        ("MCC", "mcconcordia"),
        ("QUARCC", "quarcc.mtl"),
        ("JMTL", "jmtl_concordia"),
        ("JHMA", "jhmajmsb"),
        ("JMIBA", "jmiba_jmsb"),
        ("JMSM", "jmsmcommittee"),
        ("JMUCC", "jmuccofficial"),
        ("CASA X", "jmsbcasax"),
        ("CASA Cares", "jmsbcasacares"),
        ("JMIS", "jmis.ca"),
    ]
    assert [(c.name, c.handle) for c in clubs] == expected


def test_validate_for_run_dry_run_vs_live() -> None:
    empty = Settings(google_service_account_json="")
    with pytest.raises(ValueError) as dry_err:
        empty.validate_for_run(dry_run=True)
    assert "GEMINI_API_KEY" in str(dry_err.value)
    assert "APIFY_TOKEN" in str(dry_err.value)
    assert "GOOGLE_CALENDAR_ID" not in str(dry_err.value)

    dry_ready = Settings(
        gemini_api_key="key",
        apify_token="tok",
        google_calendar_id="",
        google_service_account_json="",
    )
    dry_ready.validate_for_run(dry_run=True)

    with pytest.raises(ValueError) as live_err:
        dry_ready.validate_for_run(dry_run=False)
    assert "GOOGLE_CALENDAR_ID" in str(live_err.value)
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(live_err.value)

    live_ready = Settings(
        gemini_api_key="key",
        apify_token="tok",
        google_calendar_id="cal@group.calendar.google.com",
        google_service_account_json="./service-account.json",
    )
    live_ready.validate_for_run(dry_run=False)

    # require_apify=False allows cached --from-posts dry runs without APIFY_TOKEN
    no_apify = Settings(gemini_api_key="key", apify_token="")
    no_apify.validate_for_run(dry_run=True, require_apify=False)
    with pytest.raises(ValueError, match="APIFY_TOKEN"):
        no_apify.validate_for_run(dry_run=True, require_apify=True)


# ---------------------------------------------------------------------------
# models.py tests
# ---------------------------------------------------------------------------


def test_club_handle_normalization() -> None:
    club = Club(name="  CASA-JMSB  ", handle="  @@CASAJMSB  ", faculty="JMSB")
    assert club.name == "CASA-JMSB"
    assert club.handle == "casajmsb"
    assert club.faculty == "JMSB"
    assert club.source == "seed"

    with pytest.raises(ValidationError):
        Club(name="Empty Handle", handle="  @@@  ")


def test_instagram_post_timestamp_normalization() -> None:
    # Naive datetime is assumed UTC and converted to Montreal time (EST = UTC-5 in Nov).
    naive_utc = datetime(2026, 11, 20, 20, 0, 0)
    post = InstagramPost(
        post_id="p1",
        shortcode="ABC123",
        club_handle="@CSU_Concordia",
        club_name="CSU",
        caption="Town hall tonight!",
        url="https://www.instagram.com/p/ABC123/",
        timestamp=naive_utc,
    )
    assert post.club_handle == "csu_concordia"
    assert post.timestamp.tzinfo == MONTREAL_TZ
    assert post.timestamp.hour == 15
    assert post.timestamp.utcoffset() == timedelta(hours=-5)

    # Aware UTC datetime during EDT (July = UTC-4).
    aware_utc = datetime(2026, 7, 10, 18, 30, 0, tzinfo=timezone.utc)
    summer_post = InstagramPost(
        post_id="p2",
        club_handle="scsconcordia",
        url="https://www.instagram.com/p/XYZ/",
        timestamp=aware_utc,
    )
    assert summer_post.timestamp.tzinfo == MONTREAL_TZ
    assert summer_post.timestamp.hour == 14
    assert summer_post.timestamp.minute == 30
    assert summer_post.image_urls == []
    assert summer_post.image_url is None

    # Carousel image_urls coexists with backward-compatible image_url
    carousel_post = InstagramPost(
        post_id="p3",
        club_handle="casajmsb",
        url="https://www.instagram.com/p/CAR3/",
        image_url="https://cdn.example.com/1.jpg",
        image_urls=["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"],
        timestamp=aware_utc,
    )
    assert carousel_post.image_url == "https://cdn.example.com/1.jpg"
    assert carousel_post.image_urls == [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.jpg",
    ]


def test_extracted_event_timezone_and_default_end_time() -> None:
    # Naive start_time assumed America/Toronto; missing end_time defaults to +2h.
    ev = ExtractedEvent(
        title="JMSB Case Prep Night",
        start_time=datetime(2026, 11, 25, 18, 0, 0),
        room="MB S2.330",
        club_handle="@CASAJMSB",
        source_post_id="post-101",
    )
    assert ev.start_time.tzinfo == MONTREAL_TZ
    assert ev.start_time.hour == 18
    assert ev.end_time is not None
    assert ev.end_time.tzinfo == MONTREAL_TZ
    assert ev.end_time == ev.start_time + timedelta(hours=2)
    assert ev.club_handle == "casajmsb"

    # UTC start_time is converted to America/Toronto; end_time <= start_time resets to +2h.
    utc_start = datetime(2026, 11, 25, 23, 0, 0, tzinfo=timezone.utc)  # 18:00 EST
    earlier_end = datetime(2026, 11, 25, 22, 0, 0, tzinfo=timezone.utc)
    ev2 = ExtractedEvent(
        title="Evening Workshop",
        start_time=utc_start,
        end_time=earlier_end,
    )
    assert ev2.start_time.hour == 18
    assert ev2.end_time == ev2.start_time + timedelta(hours=2)


def test_extracted_event_fingerprint_content_hash_and_summary() -> None:
    base = ExtractedEvent(
        title="Concordia AI Paper Reading",
        start_time=datetime(2026, 11, 25, 18, 0, tzinfo=MONTREAL_TZ),
        end_time=datetime(2026, 11, 25, 20, 0, tzinfo=MONTREAL_TZ),
        location="Hall Building, Concordia University",
        room="H-937",
        description="Deep dive on agents.",
        registration_url="https://lu.ma/concordia-ai",
        club_name="Concordia AI",
        club_handle="concordiaai",
        source_post_id="ig-999",
        source_post_url="https://www.instagram.com/p/ig999/",
    )
    whitespace_variant = base.model_copy(
        update={
            "title": "  Concordia   AI Paper Reading ",
            "room": " H-937 ",
            "description": "  Deep dive on agents.  ",
        }
    )
    edited_room = base.model_copy(update={"room": "H-110"})

    assert len(base.fingerprint()) == 64
    assert len(base.content_hash()) == 64
    assert base.fingerprint() == whitespace_variant.fingerprint()
    assert base.fingerprint() == edited_room.fingerprint()
    assert base.content_hash() == whitespace_variant.content_hash()
    assert base.content_hash() != edited_room.content_hash()

    # Fallback fingerprint when source_post_id is blank uses handle + lowercased title + start date.
    no_post_id_a = base.model_copy(update={"source_post_id": ""})
    no_post_id_b = base.model_copy(
        update={
            "source_post_id": "",
            "title": "  concordia  ai paper reading  ",
            "club_handle": "@CONCORDIAAI",
        }
    )
    assert no_post_id_a.fingerprint() == no_post_id_b.fingerprint()
    assert no_post_id_a.fingerprint() != base.fingerprint()

    summary = base.format_summary_line("create")
    assert summary == "[CREATE] Concordia AI Paper Reading | 2026-11-25 18:00 EST | H-937 | @concordiaai"

    no_room = base.model_copy(update={"room": None})
    assert (
        no_room.format_summary_line("UPDATE")
        == "[UPDATE] Concordia AI Paper Reading | 2026-11-25 18:00 EST | Hall Building, Concordia University | @concordiaai"
    )


# ---------------------------------------------------------------------------
# state_store.py tests
# ---------------------------------------------------------------------------


def test_state_store_nested_creation_posts_and_synced_events(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "events.db"
    assert not db_path.exists()

    event = ExtractedEvent(
        title="FISA Stock Pitch Competition",
        start_time=datetime(2026, 11, 27, 17, 30, tzinfo=MONTREAL_TZ),
        location="John Molson Building, Concordia University",
        room="MB S2.330",
        club_name="FISA",
        club_handle="fisajmsb",
        source_post_id="post-fisa-1",
        source_post_url="https://www.instagram.com/p/fisa1/",
    )

    with EventStateStore(db_path) as store:
        assert db_path.exists()
        assert store.is_post_processed("post-fisa-1") is False

        store.mark_post_processed("post-fisa-1", "fisajmsb", events_found=1)
        assert store.is_post_processed("post-fisa-1") is True

        # Idempotent re-marking succeeds.
        store.mark_post_processed("post-fisa-1", "fisajmsb", events_found=2)
        assert store.is_post_processed("post-fisa-1") is True

        assert store.get_synced_event(event.fingerprint()) is None
        store.upsert_synced_event(event, "gcal-evt-001")

        stored = store.get_synced_event(event.fingerprint())
        assert stored is not None
        assert stored["google_event_id"] == "gcal-evt-001"
        assert stored["content_hash"] == event.content_hash()
        assert stored["source_post_id"] == "post-fisa-1"
        assert stored["title"] == "FISA Stock Pitch Competition"

        # Update existing record when event details change.
        updated_event = event.model_copy(update={"room": "MB 9th Floor"})
        store.upsert_synced_event(updated_event, "gcal-evt-001")
        stored_after = store.get_synced_event(updated_event.fingerprint())
        assert stored_after is not None
        assert stored_after["content_hash"] == updated_event.content_hash()
        assert stored_after["content_hash"] != event.content_hash()

    # Read-only access reads persisted data without allowing mutations.
    with EventStateStore(db_path, read_only=True) as ro_store:
        assert ro_store.is_post_processed("post-fisa-1") is True
        ro_row = ro_store.get_synced_event(event.fingerprint())
        assert ro_row is not None
        assert ro_row["google_event_id"] == "gcal-evt-001"
        with pytest.raises(RuntimeError):
            ro_store.mark_post_processed("post-2", "casajmsb", 0)
        with pytest.raises(RuntimeError):
            ro_store.upsert_synced_event(event, "gcal-evt-002")


def test_state_store_read_only_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "events.db"
    with pytest.raises(FileNotFoundError):
        EventStateStore(missing, read_only=True)
