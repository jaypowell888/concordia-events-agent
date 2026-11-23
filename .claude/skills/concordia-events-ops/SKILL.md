---
name: concordia-events-ops
description: Safely manage, test, and sync the Concordia Events Agent (club discovery, dry-run validation, extraction debugging, and Google Calendar sync).
---

# Concordia Events Agent Workflow

## When to Trigger
Use this skill whenever the user wants to add/update Concordia or JMSB clubs, run a calendar sync, debug a missed/misformatted Instagram event, or modify `extractor.py`, `scraper.py`, or `calendar_sync.py`.

## Guardrails (Non-Negotiable)
1. **Always dry-run first:** Never run `python main.py` (live sync) before running `python main.py --dry-run` and showing the user the extracted events.
2. **Timezone & Room Check:** Verify every extracted event uses Montreal time (`America/Toronto`) and includes building/room details when present in the post.
3. **Protect State & Secrets:** Never expose `.env` or `service-account.json`, and never manually edit `state/events.db` without backing it up first.
4. **Preserve Manual Clubs:** When refreshing club handles with `discover_clubs.py`, never pass `--no-merge` unless explicitly requested.

## Standard Workflows

### A. Safe Sync
1. Activate `.venv` (`source .venv/bin/activate`).
2. Run `python main.py --dry-run` and summarize extracted events (Title, Date/Time in `America/Toronto`, Location/Room, Source Club).
3. Ask for confirmation before running `python main.py` (live sync).
4. Verify `pytest` passes if any code was changed.

### B. Adding or Updating Clubs
1. Edit the appropriate list (`clubs.json`, `csu_clubs.json`, or `my_clubs.json`) or run `python discover_clubs.py`.
2. Run `python main.py --dry-run` to confirm Apify scrapes the new handle cleanly and Gemini extracts valid events.

### C. Debugging a Missed or Bad Event
1. Inspect the raw post payload from `scraper.py` vs. the schema in `models.py` and prompt in `extractor.py`.
2. Write a failing unit test in `tests/` using that exact Instagram caption/post before modifying `extractor.py`.
3. Re-run `pytest` and `python main.py --dry-run` to confirm the fix.
