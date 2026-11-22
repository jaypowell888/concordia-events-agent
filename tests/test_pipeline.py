"""Unit tests for discover_clubs.py, scraper.py, and extractor.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from config import MONTREAL_TZ
from discover_clubs import (
    DIRECTORY_SOURCES,
    discover_clubs,
    extract_instagram_handle,
    scrape_directory_page,
)
from extractor import EventExtractionResponse, GeminiEventExtractor, RawExtractedEvent
from models import Club, InstagramPost
from scraper import ApifyInstagramScraper, load_clubs


# ---------------------------------------------------------------------------
# discover_clubs.py tests
# ---------------------------------------------------------------------------


def test_extract_instagram_handle_filters_non_profiles() -> None:
    assert extract_instagram_handle("https://www.instagram.com/CASAJMSB/") == "casajmsb"
    assert extract_instagram_handle("https://instagram.com/@csu_concordia/?hl=en") == "csu_concordia"
    assert extract_instagram_handle("//m.instagram.com/dssc.concordia/") == "dssc.concordia"

    assert extract_instagram_handle("https://www.instagram.com/p/C9xyz123/") is None
    assert extract_instagram_handle("https://www.instagram.com/reel/abc123/") is None
    assert extract_instagram_handle("https://www.instagram.com/stories/casajmsb/") is None
    assert extract_instagram_handle("https://www.instagram.com/explore/tags/concordia/") is None
    assert extract_instagram_handle("https://example.com/casajmsb/") is None


def test_discover_clubs_offline_dedup_and_merge(tmp_path: Path) -> None:
    output = tmp_path / "clubs.json"
    clubs = discover_clubs(output_path=output, offline=True)
    assert output.exists()
    assert len(clubs) >= 15

    handles = [c.handle for c in clubs]
    assert handles == sorted(set(handles))
    assert "csu_concordia" in handles
    assert "casajmsb" in handles
    assert "ecaconcordia" in handles
    assert "scsconcordia" in handles
    assert "fisajmsb" in handles

    # Verify JSON on disk is valid and matches returned clubs.
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert len(raw) == len(clubs)

    # Pre-seed a manual club + an override for an existing handle, then merge.
    manual_payload = [
        {
            "name": "My Custom Concordia Robotics Club",
            "handle": "@CustomRobotics_CU",
            "faculty": "Gina Cody",
            "source": "manual",
        },
        {
            "name": "Custom CASA Label",
            "handle": "casajmsb",
            "faculty": "JMSB",
            "source": "manual",
        },
    ]
    output.write_text(json.dumps(manual_payload), encoding="utf-8")

    merged = discover_clubs(output_path=output, offline=True, merge_existing=True)
    by_handle = {c.handle: c for c in merged}
    assert "customrobotics_cu" in by_handle
    assert by_handle["customrobotics_cu"].source == "manual"
    # Existing manual entry takes precedence without duplicating casajmsb.
    assert by_handle["casajmsb"].name == "Custom CASA Label"
    assert len([c for c in merged if c.handle == "casajmsb"]) == 1

    # With merge_existing=False, manual-only clubs are replaced.
    overwritten = discover_clubs(output_path=output, offline=True, merge_existing=False)
    assert "customrobotics_cu" not in {c.handle for c in overwritten}


def test_scrape_directory_page_parses_links_and_online_handles_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = """
    <html><body>
      <a href="https://www.instagram.com/new_concordia_club/">New Concordia Club</a>
      <a href="https://instagram.com/new_concordia_club/">Duplicate Link</a>
      <a href="https://www.instagram.com/p/notaprofile/">Some Post</a>
      <a href="https://www.instagram.com/jmcc_jmsb/">Instagram</a>
    </body></html>
    """

    class FakeResponse:
        text = html

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def get(self, url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
            return FakeResponse()

    found = scrape_directory_page(
        "https://www.csu.qc.ca/clubs/",
        default_faculty="General",
        session=FakeSession(),  # type: ignore[arg-type]
    )
    by_handle = {c.handle: c for c in found}
    assert set(by_handle.keys()) == {"new_concordia_club", "jmcc_jmsb"}
    assert by_handle["new_concordia_club"].name == "New Concordia Club"
    assert by_handle["new_concordia_club"].source == "scraped:www.csu.qc.ca"

    # Online discover_clubs should gracefully survive network failures and still write seeds.
    def _fail_get(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise requests.ConnectionError("network disabled")

    monkeypatch.setattr(requests.Session, "get", _fail_get)
    out = tmp_path / "online_fallback.json"
    result = discover_clubs(output_path=out, offline=False, merge_existing=False)
    assert out.exists()
    assert "casajmsb" in {c.handle for c in result}


def test_discover_clubs_csu_urls_and_linktree_resolution() -> None:
    source_urls = [url for url, _ in DIRECTORY_SOURCES]
    assert "https://www.csu.qc.ca/clubs-and-associations/" not in source_urls
    assert "https://portal.csu.qc.ca/csu-clubs-list" in source_urls
    assert (
        "https://www.concordia.ca/students/life/all-groups.html?view=association&association=csu"
        in source_urls
    )

    directory_html = """
    <html><body>
      <a href="https://linktr.ee/csu_club_alpha">CSU Club Alpha</a>
      <a href="https://www.linktr.ee/csu_club_alpha/">Duplicate Linktree</a>
      <a href="https://linktr.ee/csu_club_beta">Linktree</a>
      <a href="https://www.instagram.com/direct_csu_club/">Direct CSU Club</a>
    </body></html>
    """
    linktree_alpha_html = """
    <html><body>
      <a href="https://www.instagram.com/alpha_concordia/">Follow on Instagram</a>
    </body></html>
    """
    linktree_beta_html = """
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"links":[{"url":"https:\\/\\/instagram.com\\/beta_concordia\\/"}]}}}
      </script>
    </body></html>
    """

    requested_urls: list[str] = []

    class FakeResp:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def get(self, url: str, headers: dict[str, str], timeout: float) -> FakeResp:
            requested_urls.append(url)
            if url == "https://portal.csu.qc.ca/csu-clubs-list":
                return FakeResp(directory_html)
            if url == "https://linktr.ee/csu_club_alpha":
                return FakeResp(linktree_alpha_html)
            if url == "https://linktr.ee/csu_club_beta":
                return FakeResp(linktree_beta_html)
            raise requests.HTTPError(f"Unexpected URL: {url}")

    found = scrape_directory_page(
        "https://portal.csu.qc.ca/csu-clubs-list",
        default_faculty="General",
        session=FakeSession(),  # type: ignore[arg-type]
    )
    by_handle = {c.handle: c for c in found}
    assert set(by_handle.keys()) == {"direct_csu_club", "alpha_concordia", "beta_concordia"}
    assert by_handle["alpha_concordia"].name == "CSU Club Alpha"
    assert by_handle["alpha_concordia"].source == "scraped:linktr.ee"
    assert by_handle["beta_concordia"].source == "scraped:linktr.ee"
    # Duplicate Linktree URL is fetched only once
    assert requested_urls.count("https://linktr.ee/csu_club_alpha") == 1
    assert requested_urls.count("https://linktr.ee/csu_club_beta") == 1


# ---------------------------------------------------------------------------
# scraper.py tests
# ---------------------------------------------------------------------------


def test_load_clubs_list_dict_and_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing_clubs.json"
    with pytest.raises(FileNotFoundError) as exc_info:
        load_clubs(missing)
    assert "python discover_clubs.py" in str(exc_info.value)

    list_file = tmp_path / "clubs_list.json"
    list_file.write_text(
        json.dumps(
            [
                {"name": "CASA-JMSB", "handle": "@CASAJMSB", "faculty": "JMSB"},
                {"name": "CASA dup", "handle": "casajmsb", "faculty": "JMSB"},
                {"name": "SCS", "handle": "scsconcordia", "faculty": "Gina Cody"},
            ]
        ),
        encoding="utf-8",
    )
    clubs_from_list = load_clubs(list_file)
    assert [c.handle for c in clubs_from_list] == ["casajmsb", "scsconcordia"]

    dict_file = tmp_path / "clubs_dict.json"
    dict_file.write_text(
        json.dumps(
            {
                "clubs": [
                    {"name": "CSU", "handle": "csu_concordia", "faculty": "General"},
                ]
            }
        ),
        encoding="utf-8",
    )
    clubs_from_dict = load_clubs(dict_file)
    assert len(clubs_from_dict) == 1
    assert clubs_from_dict[0].handle == "csu_concordia"


def test_apify_scraper_filters_by_lookback_and_normalizes() -> None:
    now_utc = datetime.now(timezone.utc)
    recent_1 = (now_utc - timedelta(hours=6)).isoformat()
    recent_2 = (now_utc - timedelta(days=2)).isoformat()
    too_old = (now_utc - timedelta(days=8)).isoformat()

    raw_items: list[dict[str, Any]] = [
        {
            "id": "post-recent-1",
            "shortCode": "REC1",
            "ownerUsername": "CASAJMSB",
            "caption": "Networking mixer this Thursday in MB S2.330!",
            "url": "https://www.instagram.com/p/REC1/",
            "displayUrl": "https://cdn.example.com/poster1.jpg",
            "timestamp": recent_1,
        },
        {
            "id": "post-recent-2",
            "shortCode": "REC2",
            "ownerUsername": "scsconcordia",
            "caption": "Linux Installfest in H-937",
            "url": "https://www.instagram.com/p/REC2/",
            "timestamp": recent_2,
        },
        {
            "id": "post-too-old",
            "shortCode": "OLD1",
            "ownerUsername": "casajmsb",
            "caption": "Old event from last week",
            "url": "https://www.instagram.com/p/OLD1/",
            "timestamp": too_old,
        },
        {
            "id": "post-recent-1",  # duplicate ID should be deduplicated
            "shortCode": "REC1",
            "ownerUsername": "casajmsb",
            "caption": "Duplicate item",
            "url": "https://www.instagram.com/p/REC1/",
            "timestamp": recent_1,
        },
        {
            "error": "private_account",
        },
    ]

    captured: dict[str, Any] = {}

    class FakeActorClient:
        def call(self, *, run_input: dict[str, Any]) -> dict[str, Any]:
            captured["run_input"] = run_input
            return {"defaultDatasetId": "dataset-xyz"}

    class FakeDatasetClient:
        def iterate_items(self) -> list[dict[str, Any]]:
            return raw_items

    class FakeApifyClient:
        def actor(self, actor_id: str) -> FakeActorClient:
            captured["actor_id"] = actor_id
            return FakeActorClient()

        def dataset(self, dataset_id: str) -> FakeDatasetClient:
            captured["dataset_id"] = dataset_id
            return FakeDatasetClient()

    clubs = [
        Club(name="CASA-JMSB", handle="casajmsb", faculty="JMSB", color_id="6"),
        Club(name="SCS Concordia", handle="scsconcordia", faculty="Gina Cody"),
    ]
    scraper = ApifyInstagramScraper(api_token="fake-token", client=FakeApifyClient())

    # Empty clubs short-circuits cleanly.
    assert scraper.fetch_recent_posts([], lookback_days=3) == []

    posts = scraper.fetch_recent_posts(clubs, lookback_days=3, max_posts_per_club=5)
    assert captured["actor_id"] == "apify/instagram-scraper"
    assert captured["dataset_id"] == "dataset-xyz"
    assert captured["run_input"]["resultsLimit"] == 5
    assert "https://www.instagram.com/casajmsb/" in captured["run_input"]["directUrls"]

    assert [p.post_id for p in posts] == ["post-recent-1", "post-recent-2"]
    assert posts[0].club_handle == "casajmsb"
    assert posts[0].club_name == "CASA-JMSB"
    assert posts[0].color_id == "6"
    assert posts[0].image_url == "https://cdn.example.com/poster1.jpg"
    assert posts[0].image_urls == ["https://cdn.example.com/poster1.jpg"]
    assert posts[0].timestamp.tzinfo == MONTREAL_TZ
    assert posts[1].club_name == "SCS Concordia"
    assert posts[1].color_id is None


def test_apify_scraper_extracts_deduped_carousel_image_urls() -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    carousel_item: dict[str, Any] = {
        "id": "carousel-1",
        "shortCode": "CAR1",
        "ownerUsername": "casajmsb",
        "caption": "Swipe for event schedule!",
        "url": "https://www.instagram.com/p/CAR1/",
        "displayUrl": "https://cdn.example.com/slide1.jpg",
        "images": [
            "https://cdn.example.com/slide1.jpg",
            "https://cdn.example.com/slide2.jpg",
        ],
        "childPosts": [
            {"displayUrl": "https://cdn.example.com/slide2.jpg"},
            {"displayUrl": "https://cdn.example.com/slide3.jpg"},
            {"displayUrl": "https://cdn.example.com/slide4.jpg"},
        ],
        "timestamp": now_utc,
    }

    captured_run_input: dict[str, Any] = {}

    class FakeApifyClient:
        def actor(self, actor_id: str) -> Any:
            def _call(*, run_input: dict[str, Any]) -> dict[str, Any]:
                captured_run_input.update(run_input)
                return {"defaultDatasetId": "ds-1"}

            return SimpleNamespace(call=_call)

        def dataset(self, dataset_id: str) -> Any:
            return SimpleNamespace(iterate_items=lambda: [carousel_item])

    scraper = ApifyInstagramScraper(api_token="tok", client=FakeApifyClient())
    posts = scraper.fetch_recent_posts(
        [Club(name="CASA-JMSB", handle="casajmsb", faculty="JMSB")],
        lookback_days=3,
    )
    assert captured_run_input["resultsLimit"] == 3
    assert len(posts) == 1
    assert posts[0].image_url == "https://cdn.example.com/slide1.jpg"
    assert posts[0].image_urls == [
        "https://cdn.example.com/slide1.jpg",
        "https://cdn.example.com/slide2.jpg",
        "https://cdn.example.com/slide3.jpg",
        "https://cdn.example.com/slide4.jpg",
    ]


def test_apify_scraper_supports_v3_run_object_with_default_dataset_id() -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    item = {
        "id": "v3-post-1",
        "shortCode": "V3P1",
        "ownerUsername": "casajmsb",
        "caption": "JMSB Mixer tonight!",
        "url": "https://www.instagram.com/p/V3P1/",
        "displayUrl": "https://cdn.example.com/v3.jpg",
        "timestamp": now_utc,
    }
    captured_ds: list[str] = []

    class FakeApifyV3Client:
        def actor(self, actor_id: str) -> Any:
            return SimpleNamespace(
                call=lambda **kw: SimpleNamespace(default_dataset_id="ds-v3-snake")
            )

        def dataset(self, dataset_id: str) -> Any:
            captured_ds.append(dataset_id)
            return SimpleNamespace(iterate_items=lambda: [item])

    scraper = ApifyInstagramScraper(api_token="tok", client=FakeApifyV3Client())
    posts = scraper.fetch_recent_posts(
        [Club(name="CASA-JMSB", handle="casajmsb", faculty="JMSB")],
        lookback_days=3,
    )
    assert captured_ds == ["ds-v3-snake"]
    assert [p.post_id for p in posts] == ["v3-post-1"]


# ---------------------------------------------------------------------------
# extractor.py tests
# ---------------------------------------------------------------------------


def test_gemini_extractor_parses_events_filters_non_events_and_attaches_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure poster fetch failures gracefully fall back to caption-only.
    def _fail_image_get(*args: Any, **kwargs: Any) -> Any:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", _fail_image_get)

    structured_json = json.dumps(
        {
            "events": [
                {
                    "is_event": True,
                    "title": "JMSB Finance Networking Night",
                    "start_time": "2026-11-26T18:00:00-05:00",
                    "end_time": "2026-11-26T20:30:00-05:00",
                    "location": "John Molson Building (MB), Concordia University",
                    "room": "MB S2.330",
                    "description": "Meet alumni and recruiters. Business casual.",
                    "registration_url": "https://eventbrite.ca/e/jmsb-finance-night",
                },
                {
                    "is_event": False,
                    "title": "Throwback Gala Photo Dump",
                    "start_time": None,
                    "end_time": None,
                    "location": "Concordia University, Montreal, QC",
                    "room": None,
                    "description": "Recap photos from last month.",
                    "registration_url": None,
                },
            ]
        }
    )

    recorded_call: dict[str, Any] = {}

    class FakeModels:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            recorded_call["model"] = model
            recorded_call["contents"] = contents
            recorded_call["config"] = config
            return SimpleNamespace(text=structured_json, parsed=None)

    fake_genai_client = SimpleNamespace(models=FakeModels())

    post = InstagramPost(
        post_id="ig-post-42",
        shortcode="FIN42",
        club_handle="fisajmsb",
        club_name="Finance and Investment Student Association (FISA)",
        caption="Join us this Thursday at 6 PM in MB S2.330! RSVP: https://eventbrite.ca/e/jmsb-finance-night",
        url="https://www.instagram.com/p/FIN42/",
        image_url="https://cdn.example.com/flyer.jpg",
        color_id="9",
        timestamp=datetime(2026, 11, 23, 14, 0, tzinfo=MONTREAL_TZ),
    )

    extractor = GeminiEventExtractor(api_key="fake-gemini-key", client=fake_genai_client)
    events = extractor.extract_events(post)

    assert recorded_call["model"] == "gemini-4-hydrogen"
    cfg = recorded_call["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.temperature is None
    assert cfg.thinking_config is not None
    assert str(cfg.thinking_config.thinking_level).lower().endswith("medium")
    assert cfg.thinking_config.thinking_budget is None
    prompt_text = recorded_call["contents"][0]
    assert "America/Toronto" in prompt_text
    assert "@fisajmsb" in prompt_text
    assert "MB S2.330" in prompt_text
    # Verify both new extraction prompt rules
    assert "18:00:00" in prompt_text
    assert "Time TBA — " in prompt_text
    assert "Link in bio" in prompt_text
    assert "http://" in prompt_text or "https://" in prompt_text

    assert len(events) == 1
    ev = events[0]
    assert ev.title == "JMSB Finance Networking Night"
    assert ev.start_time.tzinfo == MONTREAL_TZ
    assert ev.start_time.hour == 18
    assert ev.end_time is not None and ev.end_time.hour == 20 and ev.end_time.minute == 30
    assert ev.room == "MB S2.330"
    assert ev.registration_url == "https://eventbrite.ca/e/jmsb-finance-night"
    assert ev.club_name == "Finance and Investment Student Association (FISA)"
    assert ev.club_handle == "fisajmsb"
    assert ev.color_id == "9"
    assert ev.source_post_id == "ig-post-42"
    assert ev.source_post_url == "https://www.instagram.com/p/FIN42/"


def test_gemini_extractor_supports_parsed_response_and_empty_post() -> None:
    parsed_payload = EventExtractionResponse(
        events=[
            RawExtractedEvent(
                is_event=True,
                title="Hall Building Hack Night",
                start_time="2026-11-28T17:00:00",
                end_time=None,
                location="Hall Building, Concordia University (SGW)",
                room="H-937",
                description="Bring your laptop.",
            )
        ]
    )

    calls = {"count": 0}

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> Any:
            calls["count"] += 1
            return SimpleNamespace(parsed=parsed_payload, text="")

    extractor = GeminiEventExtractor(
        api_key="fake-key",
        client=SimpleNamespace(models=FakeModels()),
        fetch_images=False,
    )

    empty_post = InstagramPost(
        post_id="empty-1",
        club_handle="scsconcordia",
        caption="   ",
        url="https://www.instagram.com/p/EMPTY/",
        image_url=None,
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    assert extractor.extract_events(empty_post) == []
    assert calls["count"] == 0

    post = InstagramPost(
        post_id="hack-1",
        club_handle="scsconcordia",
        club_name="SCS",
        caption="Hack Night in H-937 at 5 PM!",
        url="https://www.instagram.com/p/HACK1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    events = extractor.extract_events(post)
    assert calls["count"] == 1
    assert len(events) == 1
    assert events[0].room == "H-937"
    assert events[0].start_time.tzinfo == MONTREAL_TZ
    assert events[0].end_time == events[0].start_time + timedelta(hours=2)


def test_gemini_extractor_carousel_images_limit_5_timeout_and_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_calls: list[dict[str, Any]] = []

    class FakeImgResponse:
        headers = {"Content-Type": "image/jpeg"}
        content = b"\xff\xd8\xff\xe0fake-jpeg"

        def raise_for_status(self) -> None:
            return None

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> FakeImgResponse:
        fetch_calls.append({"url": url, "timeout": timeout, "headers": headers})
        return FakeImgResponse()

    monkeypatch.setattr(requests, "get", _fake_get)

    captured_contents: list[list[Any]] = []

    class FakeModels:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            captured_contents.append(list(contents))
            return SimpleNamespace(parsed=EventExtractionResponse(events=[]), text="")

    extractor = GeminiEventExtractor(
        api_key="fake-key",
        client=SimpleNamespace(models=FakeModels()),
        sleep_fn=lambda _: None,
    )
    assert extractor.image_timeout == 12.0

    # 7 carousel images -> only up to 5 downloaded & attached
    seven_urls = [f"https://cdn.example.com/slide-{i}.jpg" for i in range(1, 8)]
    carousel_post = InstagramPost(
        post_id="car-7",
        club_handle="casajmsb",
        caption="Swipe for schedule",
        url="https://www.instagram.com/p/CAR7/",
        image_url=seven_urls[0],
        image_urls=seven_urls,
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    extractor.extract_events(carousel_post)
    assert len(fetch_calls) == 5
    assert [c["url"] for c in fetch_calls] == seven_urls[:5]
    assert all(c["timeout"] == 12.0 for c in fetch_calls)
    assert all("Mozilla/5.0" in c["headers"]["User-Agent"] for c in fetch_calls)
    # contents = [prompt_str, Part1, Part2, Part3, Part4, Part5]
    assert len(captured_contents[0]) == 6

    # Fallback to [post.image_url] when post.image_urls is empty
    fetch_calls.clear()
    captured_contents.clear()
    legacy_post = InstagramPost(
        post_id="legacy-1",
        club_handle="casajmsb",
        caption="Poster details",
        url="https://www.instagram.com/p/LEG1/",
        image_url="https://cdn.example.com/only.jpg",
        image_urls=[],
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    extractor.extract_events(legacy_post)
    assert len(fetch_calls) == 1
    assert fetch_calls[0]["url"] == "https://cdn.example.com/only.jpg"
    assert len(captured_contents[0]) == 2


def test_gemini_extractor_escalation_logic() -> None:
    event_v1 = RawExtractedEvent(
        is_event=True,
        title="Draft Event",
        start_time="2026-11-28T18:00:00-05:00",
        room="H-937",
    )
    event_v2 = RawExtractedEvent(
        is_event=True,
        title="Escalated Accurate Event",
        start_time="2026-11-28T19:00:00-05:00",
        room="MB S2.330",
    )
    post = InstagramPost(
        post_id="esc-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Dense poster text",
        url="https://www.instagram.com/p/ESC1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )

    # 1. Paid-tier escalation triggered by confidence < 0.75 on an event post
    calls_1: list[tuple[str, Any]] = []

    class FakeModelsLowConf:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            calls_1.append((model, config))
            if len(calls_1) == 1:
                return SimpleNamespace(
                    parsed=EventExtractionResponse(
                        events=[event_v1], confidence=0.6, needs_deep_review=False
                    ),
                    text="",
                )
            return SimpleNamespace(
                parsed=EventExtractionResponse(
                    events=[event_v2], confidence=0.95, needs_deep_review=False
                ),
                text="",
            )

    ext1 = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        thinking_level="medium",
        escalation_model="gemini-4-neon",
        escalation_thinking_level="high",
        client=SimpleNamespace(models=FakeModelsLowConf()),
        fetch_images=False,
    )
    res1 = ext1.extract_events(post)
    assert len(calls_1) == 2
    assert calls_1[0][0] == "gemini-4-hydrogen"
    assert str(calls_1[0][1].thinking_config.thinking_level).lower().endswith("medium")
    assert calls_1[1][0] == "gemini-4-neon"
    assert str(calls_1[1][1].thinking_config.thinking_level).lower().endswith("high")
    assert calls_1[1][1].temperature is None
    assert calls_1[1][1].thinking_config.thinking_budget is None
    assert len(res1) == 1 and res1[0].title == "Escalated Accurate Event"

    # 2. Free-tier same-model escalation (medium -> high) triggered by needs_deep_review=True
    calls_2: list[tuple[str, Any]] = []

    class FakeModelsDeepReview:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            calls_2.append((model, config))
            if len(calls_2) == 1:
                return SimpleNamespace(
                    parsed=EventExtractionResponse(
                        events=[event_v1], confidence=0.9, needs_deep_review=True
                    ),
                    text="",
                )
            return SimpleNamespace(
                parsed=EventExtractionResponse(
                    events=[event_v2], confidence=0.98, needs_deep_review=False
                ),
                text="",
            )

    ext2 = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        thinking_level="medium",
        escalation_model="gemini-4-hydrogen",
        escalation_thinking_level="high",
        client=SimpleNamespace(models=FakeModelsDeepReview()),
        fetch_images=False,
    )
    res2 = ext2.extract_events(post)
    assert len(calls_2) == 2
    assert calls_2[0][0] == "gemini-4-hydrogen"
    assert calls_2[1][0] == "gemini-4-hydrogen"
    assert str(calls_2[1][1].thinking_config.thinking_level).lower().endswith("high")
    assert res2[0].title == "Escalated Accurate Event"

    # 3. Non-event post does NOT escalate even with low confidence + needs_deep_review=True
    calls_3: list[str] = []

    class FakeModelsNonEvent:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            calls_3.append(model)
            return SimpleNamespace(
                parsed=EventExtractionResponse(
                    events=[RawExtractedEvent(is_event=False, title="Meme")],
                    confidence=0.2,
                    needs_deep_review=True,
                ),
                text="",
            )

    ext3 = GeminiEventExtractor(
        api_key="k",
        escalation_model="gemini-4-neon",
        client=SimpleNamespace(models=FakeModelsNonEvent()),
        fetch_images=False,
    )
    assert ext3.extract_events(post) == []
    assert len(calls_3) == 1

    # 4. Empty escalation_model disables escalation
    calls_4: list[str] = []

    class FakeModelsDisabled:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            calls_4.append(model)
            return SimpleNamespace(
                parsed=EventExtractionResponse(
                    events=[event_v1], confidence=0.3, needs_deep_review=True
                ),
                text="",
            )

    ext4 = GeminiEventExtractor(
        api_key="k",
        escalation_model="",
        client=SimpleNamespace(models=FakeModelsDisabled()),
        fetch_images=False,
    )
    res4 = ext4.extract_events(post)
    assert len(calls_4) == 1
    assert res4[0].title == "Draft Event"


class _FakeGeminiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


def test_gemini_extractor_retries_backoff_pacing_fallback_and_model_aware_thinking() -> None:
    post_1 = InstagramPost(
        post_id="retry-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Case workshop in H-937!",
        url="https://www.instagram.com/p/RET1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    post_2 = post_1.model_copy(update={"post_id": "retry-2"})
    valid_event = RawExtractedEvent(
        is_event=True,
        title="Recovered Workshop",
        start_time="2026-11-28T18:00:00-05:00",
        room="H-937",
    )

    # 1. Transient 503 then 429 -> retries with exponential backoff + jitter, then succeeds;
    #    pacing delay of 1.5s occurs before post_2 (not before post_1).
    sleeps: list[float] = []
    attempts = {"n": 0}

    class FakeTransientRecovery:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _FakeGeminiError(503, "UNAVAILABLE")
            if attempts["n"] == 2:
                raise _FakeGeminiError(429, "RESOURCE_EXHAUSTED")
            return SimpleNamespace(
                parsed=EventExtractionResponse(events=[valid_event]),
                text="",
            )

    ext = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        max_retries=3,
        retry_base_delay=2.0,
        retry_max_delay=30.0,
        post_pacing_delay=1.5,
        sleep_fn=sleeps.append,
        client=SimpleNamespace(models=FakeTransientRecovery()),
        fetch_images=False,
    )
    res_1 = ext.extract_events(post_1)
    assert len(res_1) == 1 and res_1[0].title == "Recovered Workshop"
    # 0 pacing sleep before post_1; 2 retry sleeps: [2.0, 3.0] and [4.0, 5.0]
    assert len(sleeps) == 2
    assert 2.0 <= sleeps[0] <= 3.0
    assert 4.0 <= sleeps[1] <= 5.0

    # Second post triggers 1.5s pacing sleep before calling Gemini
    res_2 = ext.extract_events(post_2)
    assert len(res_2) == 1
    assert len(sleeps) == 3
    assert sleeps[2] == 1.5

    # 2. Non-transient 400 / 401 errors are NOT retried and do NOT fall back
    for bad_code, bad_msg in ((400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED")):
        bad_sleeps: list[float] = []
        bad_calls: list[str] = []

        class FakeNonTransient:
            def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
                bad_calls.append(model)
                raise _FakeGeminiError(bad_code, bad_msg)

        bad_ext = GeminiEventExtractor(
            api_key="k",
            fallback_model="gemini-2.5-flash",
            max_retries=3,
            sleep_fn=bad_sleeps.append,
            client=SimpleNamespace(models=FakeNonTransient()),
            fetch_images=False,
        )
        with pytest.raises(_FakeGeminiError):
            bad_ext.extract_events(post_1)
        assert bad_calls == ["gemini-4-hydrogen"]
        assert bad_sleeps == []

    # 3. Exhausted primary 503 retries fall back to gemini-2.5-flash WITHOUT thinking_config
    fb_calls: list[tuple[str, Any]] = []

    class FakeFallbackModels:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            fb_calls.append((model, config))
            if model == "gemini-4-hydrogen":
                raise _FakeGeminiError(503, "UNAVAILABLE")
            return SimpleNamespace(
                parsed=EventExtractionResponse(events=[valid_event]),
                text="",
            )

    fb_ext = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        fallback_model="gemini-2.5-flash",
        max_retries=2,
        sleep_fn=lambda _: None,
        client=SimpleNamespace(models=FakeFallbackModels()),
        fetch_images=False,
    )
    fb_res = fb_ext.extract_events(post_1)
    assert len(fb_res) == 1 and fb_res[0].title == "Recovered Workshop"
    # 3 attempts on primary (initial + 2 retries), then 1 call on fallback
    assert [m for m, _ in fb_calls] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-2.5-flash",
    ]
    # Primary gemini-4-* includes thinking_config; fallback gemini-2.5-flash omits it!
    assert fb_calls[0][1].thinking_config is not None
    assert fb_calls[0][1].temperature is None
    assert fb_calls[-1][1].thinking_config is None
    assert fb_calls[-1][1].temperature is None

    # 4. Escalation hitting exhausted 503 keeps the primary result gracefully
    esc_calls: list[str] = []

    class FakeEscalation503:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            esc_calls.append(model)
            if model == "gemini-4-hydrogen":
                return SimpleNamespace(
                    parsed=EventExtractionResponse(
                        events=[valid_event], confidence=0.5, needs_deep_review=True
                    ),
                    text="",
                )
            raise _FakeGeminiError(503, "UNAVAILABLE")

    esc_ext = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        escalation_model="gemini-4-neon",
        max_retries=1,
        sleep_fn=lambda _: None,
        client=SimpleNamespace(models=FakeEscalation503()),
        fetch_images=False,
    )
    esc_res = esc_ext.extract_events(post_1)
    assert esc_calls == ["gemini-4-hydrogen", "gemini-4-neon", "gemini-4-neon"]
    assert len(esc_res) == 1 and esc_res[0].title == "Recovered Workshop"


def test_gemini_extractor_preserves_external_venues_and_online_webinars() -> None:
    captured_prompts: list[str] = []

    cbv_post = InstagramPost(
        post_id="cbv-webinar-1",
        club_handle="cbvinstitute",
        club_name="CBV Institute",
        caption="Join our free live Zoom webinar on Business Valuation this Thursday at 6 PM!",
        url="https://www.instagram.com/p/CBV1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    gala_post = InstagramPost(
        post_id="casa-gala-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Annual Gala at Hotel Bonaventure Montreal, 900 Rue de la Gauchetière O!",
        url="https://www.instagram.com/p/GALA1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )

    responses = [
        # Model mistakenly leaves Concordia default on a webinar -> post-processor normalizes to "Online"
        EventExtractionResponse(
            events=[
                RawExtractedEvent(
                    is_event=True,
                    title="CBV Valuation Info Webinar",
                    start_time="2026-11-26T18:00:00-05:00",
                    location="Concordia University, Montreal, QC",
                    room="Zoom",
                    description="Live Zoom webinar for aspiring CBVs.",
                ),
                RawExtractedEvent(
                    is_event=True,
                    title="CBV Q&A Session",
                    start_time="2026-11-27T12:00:00-05:00",
                    location="Online (Zoom)",
                    room=None,
                    description="Virtual Q&A.",
                ),
            ]
        ),
        # Off-campus downtown Montreal venue preserved intact
        EventExtractionResponse(
            events=[
                RawExtractedEvent(
                    is_event=True,
                    title="CASA Annual Gala",
                    start_time="2026-11-29T19:00:00-05:00",
                    location="Hotel Bonaventure Montreal, 900 Rue de la Gauchetière O",
                    room="Ballroom A",
                    description="Formal dinner.",
                )
            ]
        ),
    ]

    class FakeVenueModels:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            captured_prompts.append(contents[0])
            return SimpleNamespace(parsed=responses.pop(0), text="")

    ext = GeminiEventExtractor(
        api_key="k",
        client=SimpleNamespace(models=FakeVenueModels()),
        fetch_images=False,
        sleep_fn=lambda _: None,
    )

    cbv_events = ext.extract_events(cbv_post)
    assert len(cbv_events) == 2
    assert cbv_events[0].location == "Online"
    assert cbv_events[0].room is None
    assert cbv_events[1].location == "Online (Zoom)"
    assert cbv_events[1].room is None

    gala_events = ext.extract_events(gala_post)
    assert len(gala_events) == 1
    assert gala_events[0].location == "Hotel Bonaventure Montreal, 900 Rue de la Gauchetière O"
    assert gala_events[0].room == "Ballroom A"

    # Prompt explicitly includes Online and non-Concordia external venue instructions
    prompt = captured_prompts[0]
    assert '"Online"' in prompt
    assert "Off-campus / external / downtown Montreal venues" in prompt
    assert 'NEVER default virtual/online events to "Concordia University, Montreal, QC"' in prompt


def test_gemini_extractor_daily_quota_429_fast_fallback_skips_retries_and_sticks() -> None:
    post_1 = InstagramPost(
        post_id="rpd-1",
        club_handle="casajmsb",
        club_name="CASA-JMSB",
        caption="Case workshop in H-937!",
        url="https://www.instagram.com/p/RPD1/",
        timestamp=datetime(2026, 11, 23, 12, 0, tzinfo=MONTREAL_TZ),
    )
    post_2 = post_1.model_copy(update={"post_id": "rpd-2"})
    valid_event = RawExtractedEvent(
        is_event=True,
        title="Fast-Fallback Event",
        start_time="2026-11-28T18:00:00-05:00",
        room="H-937",
    )

    sleeps: list[float] = []
    calls: list[tuple[str, Any]] = []

    class FakeDailyQuotaModels:
        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            calls.append((model, config))
            if model == "gemini-4-hydrogen":
                raise _FakeGeminiError(
                    429,
                    "RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel-FreeTier exceeded",
                )
            return SimpleNamespace(
                parsed=EventExtractionResponse(events=[valid_event]),
                text="",
            )

    ext = GeminiEventExtractor(
        api_key="k",
        model="gemini-4-hydrogen",
        fallback_model="gemini-3.5-flash-lite",
        max_retries=3,
        post_pacing_delay=1.5,
        sleep_fn=sleeps.append,
        client=SimpleNamespace(models=FakeDailyQuotaModels()),
        fetch_images=False,
    )

    res_1 = ext.extract_events(post_1)
    assert len(res_1) == 1 and res_1[0].title == "Fast-Fallback Event"
    # 0 retry sleeps on daily quota 429!
    assert sleeps == []
    assert ext._primary_daily_quota_exhausted is True
    assert [m for m, _ in calls] == ["gemini-4-hydrogen", "gemini-3.5-flash-lite"]
    # Fallback gemini-3.5-flash-lite omits thinking_config
    assert calls[0][1].thinking_config is not None
    assert calls[1][1].thinking_config is None

    # Subsequent post goes STRAIGHT to gemini-3.5-flash-lite without calling primary again
    res_2 = ext.extract_events(post_2)
    assert len(res_2) == 1 and res_2[0].title == "Fast-Fallback Event"
    assert [m for m, _ in calls] == [
        "gemini-4-hydrogen",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
    ]
    # Only the single 1.5s pacing sleep before post_2 (still 0 retry sleeps)
    assert sleeps == [1.5]
