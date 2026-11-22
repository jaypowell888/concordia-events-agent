# Concordia Events Agent

Automatically scrapes recent Instagram posts from Concordia University & JMSB student associations, extracts structured event details using Gemini, and syncs them into a shared Google Calendar with SQLite-backed deduplication.

## Setup

1. **Create and activate a Python 3.12 virtual environment:**

   ```bash
   # Windows (PowerShell)
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1

   # macOS / Linux
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables and Google credentials:**

   Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

   - `GEMINI_API_KEY`: API key from Google AI Studio.
   - `APIFY_TOKEN`: API token from Apify (uses `apify/instagram-scraper`).
   - `GOOGLE_CALENDAR_ID`: Target Google Calendar ID (e.g., `...@group.calendar.google.com`).
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: Path to your downloaded `service-account.json` key file (or the raw JSON string itself, useful in CI).
   - `LOOKBACK_DAYS`: Number of days of Instagram posts to inspect (default: `3`).

   Save your Google Cloud service account key as `./service-account.json` (git-ignored) and **share your target Google Calendar** with the service account's `client_email` giving it **Make changes to events** permission.

## Usage

### 1. Discover / Seed Clubs (`clubs.json`)

Generate or refresh the list of Concordia & JMSB club Instagram handles:

```bash
python discover_clubs.py
```

Useful flags:
- `--offline`: Write the curated Concordia/JMSB seed list without making network requests.
- `--output PATH`: Write to a custom file path (default: `clubs.json`).
- `--no-merge`: Overwrite instead of preserving manually added entries.

### 2. Dry Run (No Calendar Writes)

Preview extracted events with Montreal times (`America/Toronto`) and room numbers without writing to Google Calendar or `state/events.db`:

```bash
python main.py --dry-run
```

### 3. Live Sync

Scrape posts, extract events, sync to Google Calendar, and record deduplication state in `state/events.db`:

```bash
python main.py
```

## Automated Daily Sync (GitHub Actions)

The workflow at `.github/workflows/daily_sync.yml` runs daily at 8:00 AM EST (`0 13 * * *` UTC) and supports manual runs via **workflow_dispatch**.

Configure these repository secrets under **Settings -> Secrets and variables -> Actions**:

| Secret | Description |
| --- | --- |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `APIFY_TOKEN` | Apify API token |
| `GOOGLE_CALENDAR_ID` | Target Google Calendar ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of your `service-account.json` file |

Optionally set a repository variable `LOOKBACK_DAYS` (defaults to `3`). After each live run, the workflow commits and pushes updates to `state/events.db` so already-synced events are skipped or updated cleanly on future runs.
