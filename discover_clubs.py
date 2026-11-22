"""Discover and maintain the list of Concordia & JMSB student club Instagram handles."""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import DEFAULT_CLUBS_PATH
from models import Club

logger = logging.getLogger(__name__)

# Curated baseline of active Concordia University & JMSB student associations and clubs.
SEED_CLUBS: tuple[dict[str, str], ...] = (
    # Student Unions & General Campus
    {
        "name": "Concordia Student Union (CSU)",
        "handle": "csu_concordia",
        "faculty": "General",
        "source": "seed",
    },
    {
        "name": "District 3 Innovation Hub",
        "handle": "district3innovation",
        "faculty": "General",
        "source": "seed",
    },
    {
        "name": "Concordia Career & Placement Services (CAPS)",
        "handle": "concordiacaps",
        "faculty": "General",
        "source": "seed",
    },
    # John Molson School of Business (JMSB)
    {
        "name": "Commerce and Administration Students' Association (CASA-JMSB)",
        "handle": "casajmsb",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Marketing Association (JSMA / JMMA)",
        "handle": "jmmaconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "Finance and Investment Student Association (FISA)",
        "handle": "fisajmsb",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Accounting Society (JMAS)",
        "handle": "jmasjmsb",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Competition Committee (JMCC)",
        "handle": "jmcc_jmsb",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson MBA International Case Competition (JMICC)",
        "handle": "jmicc_official",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Undergraduate Case Competition (JMUCC)",
        "handle": "jmucc",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Sustainable Business Group (JSBG)",
        "handle": "jsbgconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Business Technology Management Association (JMBTMA)",
        "handle": "jmbtma",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Supply Chain and Business Technology Association (JSBA)",
        "handle": "jsba_jmsb",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Human Resources Association (JMHRA)",
        "handle": "jmhraconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Entrepreneurship Society (JMES)",
        "handle": "jmesconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "Enactus Concordia",
        "handle": "enactusconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    {
        "name": "John Molson Women in Business (JMWIB)",
        "handle": "jmwibconcordia",
        "faculty": "JMSB",
        "source": "seed",
    },
    # Gina Cody School of Engineering and Computer Science
    {
        "name": "Engineering and Computer Science Association (ECA)",
        "handle": "ecaconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Software Engineering and Computer Science Society (SCS)",
        "handle": "scsconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Concordia Artificial Intelligence (Concordia AI)",
        "handle": "concordiaai",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Data Science Society at Concordia (DSSC)",
        "handle": "dssc.concordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "HackConcordia",
        "handle": "hackconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "IEEE Concordia",
        "handle": "ieeeconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Women in Engineering Concordia (WIE)",
        "handle": "wieconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Space Concordia",
        "handle": "spaceconcordia",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    {
        "name": "Concordia Game Development (GameDev)",
        "handle": "concordiagamedev",
        "faculty": "Gina Cody",
        "source": "seed",
    },
    # Arts & Science and Fine Arts
    {
        "name": "Arts and Science Federation of Associations (ASFA)",
        "handle": "asfaconcordia",
        "faculty": "Arts & Science",
        "source": "seed",
    },
    {
        "name": "Economics Student Association (ESA Concordia)",
        "handle": "esaconcordia",
        "faculty": "Arts & Science",
        "source": "seed",
    },
    {
        "name": "Political Science Student Association (PSSA Concordia)",
        "handle": "pssaconcordia",
        "faculty": "Arts & Science",
        "source": "seed",
    },
    {
        "name": "Fine Arts Student Alliance (FASA)",
        "handle": "fasaconcordia",
        "faculty": "Fine Arts",
        "source": "seed",
    },
)

DIRECTORY_SOURCES: tuple[tuple[str, str], ...] = (
    ("https://portal.csu.qc.ca/csu-clubs-list", "General"),
    (
        "https://www.concordia.ca/students/life/all-groups.html?view=association&association=csu",
        "General",
    ),
    ("https://www.concordia.ca/students/life/all-groups.html", "General"),
    ("https://www.casajmsb.ca/subsidiaries", "JMSB"),
)

IGNORED_INSTAGRAM_PATHS: frozenset[str] = frozenset(
    {
        "about",
        "accounts",
        "api",
        "developer",
        "developers",
        "direct",
        "explore",
        "graphql",
        "intent",
        "legal",
        "locations",
        "oauth",
        "p",
        "reel",
        "reels",
        "share",
        "sharer",
        "static",
        "stories",
        "tags",
        "tv",
        "web",
    }
)

_HANDLE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._]{0,28}[a-z0-9])?$")
_INSTAGRAM_URL_IN_TEXT_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/@?([A-Za-z0-9._]{1,30})",
    re.IGNORECASE,
)
_LINKTREE_URL_IN_TEXT_RE = re.compile(
    r"https?://(?:www\.)?(?:linktr\.ee|linktree\.com)/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_IGNORED_LINKTREE_PATHS: frozenset[str] = frozenset(
    {"about", "discover", "help", "login", "marketplace", "register", "s", "signup", "terms"}
)
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def extract_instagram_handle(url: str) -> str | None:
    """Extract a normalized Instagram profile handle from a URL, or None if invalid."""
    if not url:
        return None
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    elif host.startswith("m."):
        host = host[2:]

    if host not in {"instagram.com", "instagr.am"}:
        return None

    segments = [seg.strip() for seg in parsed.path.split("/") if seg.strip()]
    if not segments:
        return None

    first = segments[0].lstrip("@").lower()
    if first in IGNORED_INSTAGRAM_PATHS:
        return None
    if ".." in first or not _HANDLE_PATTERN.match(first):
        return None

    return first


def is_linktree_url(url: str) -> bool:
    """Return True if ``url`` points to a Linktree profile page."""
    return normalize_linktree_url(url) is not None


def normalize_linktree_url(url: str) -> str | None:
    """Normalize a Linktree profile URL for deduplication, or return None if invalid."""
    if not url:
        return None
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"linktr.ee", "linktree.com"}:
        return None

    segments = [seg.strip() for seg in parsed.path.split("/") if seg.strip()]
    if not segments:
        return None

    slug = segments[0]
    if slug.lower() in _IGNORED_LINKTREE_PATHS:
        return None

    return f"https://linktr.ee/{slug}"


def extract_instagram_handles_from_html(html: str) -> list[str]:
    """Extract deduplicated Instagram profile handles from HTML anchors and raw/JSON text."""
    if not html:
        return []

    handles: list[str] = []
    seen: set[str] = set()

    def _add(raw_url: str) -> None:
        handle = extract_instagram_handle(raw_url)
        if handle and handle not in seen:
            seen.add(handle)
            handles.append(handle)

    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        _add(str(anchor.get("href", "")))

    # Scan unescaped raw HTML / embedded JSON (e.g. Linktree __NEXT_DATA__) as well.
    unescaped = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    for match in _INSTAGRAM_URL_IN_TEXT_RE.finditer(unescaped):
        _add(match.group(0))

    return handles


def resolve_linktree_handles(
    linktree_url: str,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> list[str]:
    """Fetch a Linktree profile page and extract any Instagram handles it links to."""
    http = session or requests
    try:
        response = http.get(linktree_url, headers=_REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        return extract_instagram_handles_from_html(response.text)
    except requests.RequestException as exc:
        logger.warning("Failed to fetch Linktree page %s: %s", linktree_url, exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse Linktree page %s: %s", linktree_url, exc)
        return []


def _humanize_handle(handle: str) -> str:
    """Create a readable fallback club name from an Instagram handle."""
    words = [part for part in re.split(r"[._-]+", handle) if part]
    if not words:
        return handle
    return " ".join(word.upper() if len(word) <= 4 else word.capitalize() for word in words)


def _infer_link_name(anchor_text: str, handle: str) -> str:
    """Pick a descriptive club name from anchor text when useful."""
    cleaned = " ".join(anchor_text.split()).strip()
    lowered = cleaned.lower()
    if (
        not cleaned
        or lowered
        in {
            "instagram",
            "ig",
            "follow",
            "follow us",
            "link",
            "links",
            "linktree",
            "linktr.ee",
            "social",
            "website",
        }
        or lowered.startswith("http")
        or lowered.lstrip("@") == handle
    ):
        return _humanize_handle(handle)
    return cleaned


def scrape_directory_page(
    url: str,
    default_faculty: str = "General",
    timeout: float = 10.0,
    session: requests.Session | None = None,
    seen_linktrees: set[str] | None = None,
) -> list[Club]:
    """Scrape a public web page for Instagram club links (resolving Linktree URLs too)."""
    http = session or requests
    response = http.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Club] = {}
    linktree_candidates: list[tuple[str, str]] = []
    local_linktrees = seen_linktrees if seen_linktrees is not None else set()
    source_host = urlparse(url).netloc

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        anchor_text = anchor.get_text(" ", strip=True)

        handle = extract_instagram_handle(href)
        if handle:
            if handle not in found:
                found[handle] = Club(
                    name=_infer_link_name(anchor_text, handle),
                    handle=handle,
                    faculty=default_faculty,
                    source=f"scraped:{source_host}",
                )
            continue

        lt_url = normalize_linktree_url(href)
        if lt_url:
            linktree_candidates.append((lt_url, anchor_text))

    # Secondary scan of raw HTML / embedded JSON for direct Instagram or Linktree URLs.
    unescaped = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    for match in _INSTAGRAM_URL_IN_TEXT_RE.finditer(unescaped):
        handle = extract_instagram_handle(match.group(0))
        if handle and handle not in found:
            found[handle] = Club(
                name=_humanize_handle(handle),
                handle=handle,
                faculty=default_faculty,
                source=f"scraped:{source_host}",
            )

    for match in _LINKTREE_URL_IN_TEXT_RE.finditer(unescaped):
        lt_url = normalize_linktree_url(match.group(0))
        if lt_url:
            linktree_candidates.append((lt_url, ""))

    # Resolve each unique Linktree URL at most once.
    for lt_url, anchor_text in linktree_candidates:
        if lt_url in local_linktrees:
            continue
        local_linktrees.add(lt_url)
        lt_handles = resolve_linktree_handles(lt_url, timeout=timeout, session=session)
        for handle in lt_handles:
            if handle not in found:
                found[handle] = Club(
                    name=_infer_link_name(anchor_text, handle),
                    handle=handle,
                    faculty=default_faculty,
                    source="scraped:linktr.ee",
                )

    return list(found.values())


def _load_existing_clubs(path: Path) -> list[Club]:
    """Read existing clubs from JSON file if present, tolerating list or dict payloads."""
    if not path.exists():
        return []

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    data = json.loads(raw_text)
    if isinstance(data, dict) and isinstance(data.get("clubs"), list):
        items = data["clubs"]
    elif isinstance(data, list):
        items = data
    else:
        logger.warning(
            "Existing clubs file %s has unexpected JSON structure; ignoring it.", path
        )
        return []

    clubs: list[Club] = []
    for item in items:
        try:
            clubs.append(Club.model_validate(item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping invalid club entry in %s: %s", path, exc)
    return clubs


def _merge_clubs(*club_groups: Iterable[Club]) -> list[Club]:
    """Deduplicate clubs by lowercase handle (earlier groups take precedence) and sort."""
    merged: dict[str, Club] = {}
    for group in club_groups:
        for club in group:
            key = club.handle.lower()
            if key not in merged:
                merged[key] = club
    return sorted(merged.values(), key=lambda c: c.handle)


def _discover_clubs_with_stats(
    output_path: Path = DEFAULT_CLUBS_PATH,
    offline: bool = False,
    merge_existing: bool = True,
) -> tuple[list[Club], dict[str, int]]:
    """Run club discovery and return both the merged clubs and scraping statistics."""
    output_path = Path(output_path)

    existing_clubs: list[Club] = []
    if merge_existing and output_path.exists():
        try:
            existing_clubs = _load_existing_clubs(output_path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read existing clubs from %s (%s); continuing without merge.",
                output_path,
                exc,
            )

    seed_clubs = [Club.model_validate(entry) for entry in SEED_CLUBS]

    scraped_clubs: list[Club] = []
    seen_linktrees: set[str] = set()
    if not offline:
        with requests.Session() as session:
            for url, faculty in DIRECTORY_SOURCES:
                try:
                    discovered = scrape_directory_page(
                        url=url,
                        default_faculty=faculty,
                        session=session,
                        seen_linktrees=seen_linktrees,
                    )
                    scraped_clubs.extend(discovered)
                    logger.info("Discovered %d Instagram handles from %s", len(discovered), url)
                except requests.RequestException as exc:
                    logger.warning("Failed to fetch directory %s: %s", url, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to parse directory %s: %s", url, exc)

    unique_scraped = _merge_clubs(scraped_clubs)
    scraped_linktree_count = sum(1 for club in unique_scraped if club.source == "scraped:linktr.ee")

    # Preserve manually maintained existing entries first, then curated seeds, then scraped handles.
    merged = _merge_clubs(existing_clubs, seed_clubs, unique_scraped)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [club.model_dump(mode="json") for club in merged]
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    stats = {
        "existing": len(existing_clubs),
        "seed": len(seed_clubs),
        "scraped_total": len(unique_scraped),
        "scraped_linktree": scraped_linktree_count,
        "linktrees_visited": len(seen_linktrees),
        "total_written": len(merged),
    }
    return merged, stats


def discover_clubs(
    output_path: Path = DEFAULT_CLUBS_PATH,
    offline: bool = False,
    merge_existing: bool = True,
) -> list[Club]:
    """Discover Concordia & JMSB student clubs and write them to a JSON file.

    Args:
        output_path: Destination path for the deduplicated, sorted clubs JSON file.
        offline: If True, skip live web scraping and use only curated seed (+ existing) clubs.
        merge_existing: If True and ``output_path`` exists, preserve existing clubs.

    Returns:
        Sorted, deduplicated list of ``Club`` models written to ``output_path``.
    """
    merged, _ = _discover_clubs_with_stats(
        output_path=output_path,
        offline=offline,
        merge_existing=merge_existing,
    )
    return merged


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for club discovery."""
    parser = argparse.ArgumentParser(
        description="Discover Concordia & JMSB student club Instagram handles and write clubs.json."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_CLUBS_PATH,
        help=f"Output JSON file path (default: {DEFAULT_CLUBS_PATH}).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip live web directory scraping and use only curated seed clubs.",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Overwrite the output file without merging existing manually added clubs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for discovering Concordia student clubs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    clubs, stats = _discover_clubs_with_stats(
        output_path=args.output,
        offline=args.offline,
        merge_existing=not args.no_merge,
    )
    print(
        f"Scraped {stats['scraped_total']} live handle(s) "
        f"({stats['scraped_linktree']} via Linktree); "
        f"wrote {len(clubs)} total clubs to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
