# New Release Book Filter

Automated weekly pipeline that scrapes Goodreads for new book releases, filters by rating, deduplicates against a persistent log, and delivers a curated shortlist via email and markdown.

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/toddaerickson/newreleases.git
cd newreleases
pip install -r requirements.txt

# 2. Run (markdown only, no email)
python main.py --skip-email --recipient you@example.com

# 3. Run with email delivery
export BOOK_RECIPIENT='you@example.com'
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=sender@gmail.com
export SMTP_PASS='your-gmail-app-password'
python main.py
```

The tool scans Goodreads for books published in the last 90 days, filters for ratings >= 4.3 with at least 500 ratings, and outputs a markdown file to `shortlists/`. If email is configured, it sends the shortlist as a digest. Books are logged to a local SQLite database so they only appear once.

### Common recipes

```bash
# Lower the bar — catch more books earlier
python main.py --min-rating 4.0 --min-count 200 --skip-email --recipient you@example.com

# Wider time window (6 months)
python main.py --window 180 --skip-email --recipient you@example.com

# Narrow window (last 30 days only)
python main.py --window 30 --skip-email --recipient you@example.com

# Re-check books that previously failed the filter (ratings may have improved)
python main.py --recheck --skip-email --recipient you@example.com
```

## How it works

```text
Goodreads new releases (trailing 90 days)
        |
        v
   Dedup against SQLite log (seen_books.db)
        |
        v
   Enrich each unseen book (rating, author, genres, pub date)
        |
        v
   Filter: rating >= 4.3 AND rating_count >= 500
        |
        v
   Output: shortlist markdown + email digest
        |
        v
   Log all books (pass or fail) to SQLite for future dedup
```

## Setup

### Prerequisites

- Python 3.12+
- A Gmail / Google Workspace account with an [App Password](https://myaccount.google.com/apppasswords) (requires 2-Step Verification)

### Local

```bash
git clone https://github.com/toddaerickson/newreleases.git
cd newreleases
pip install -r requirements.txt
```

### GitHub Actions (automated weekly runs)

Add these **repository secrets** (Settings > Secrets and variables > Actions > New repository secret):

| Secret | Value | Example |
|--------|-------|---------|
| `BOOK_RECIPIENT` | Email address to receive the digest | `'user@example.com'` (wrap in single quotes if `@` causes issues) |
| `SMTP_HOST` | SMTP server hostname | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` | Sending email address | `sender@gmail.com` |
| `SMTP_PASS` | App Password (16-char, from Google) | `abcd efgh ijkl mnop` |

The workflow runs automatically every **Sunday at 6 PM Central** (23:00 UTC). Shortlist markdown files are committed back to the repo under `shortlists/`.

## Usage

### Run locally

```bash
# Full run with email
export BOOK_RECIPIENT='you@example.com'
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=sender@gmail.com
export SMTP_PASS='your-app-password'
python main.py

# Markdown only, no email
python main.py --skip-email --recipient you@example.com

# Custom filter thresholds
python main.py --min-rating 4.0 --min-count 200 --skip-email --recipient you@example.com

# Wider or narrower time window
python main.py --window 30 --skip-email --recipient you@example.com   # last 30 days
python main.py --window 180 --skip-email --recipient you@example.com  # last 6 months

# Re-check previously failed books (quarterly recommended)
python main.py --recheck --skip-email --recipient you@example.com
```

### Run via GitHub Actions (manual trigger)

1. Go to the **Actions** tab in the repo
2. Click **"Weekly Book Filter"** in the left sidebar
3. Click **"Run workflow"** > **"Run workflow"**

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--window N` | `90` | Trailing days to consider a book "new" |
| `--min-rating F` | `4.3` | Minimum Goodreads rating to pass filter |
| `--min-count N` | `500` | Minimum number of ratings to pass filter |
| `--recipient EMAIL` | `BOOK_RECIPIENT` env var | Email address to send the digest to |
| `--recheck` | off | Re-check previously failed books that haven't been checked in 90+ days |
| `--skip-email` | off | Write markdown shortlist only, skip email |

## Project structure

```text
newreleases/
├── main.py              # Orchestrator — phases 1-5, CLI entry point
├── scraper.py           # Goodreads scraping (new releases + book detail pages)
├── filters.py           # Rating/count filter logic
├── db.py                # SQLite seen_books persistence and dedup
├── notify.py            # Markdown shortlist writer + email sender
├── requirements.txt     # Python dependencies
├── .gitignore
├── shortlists/          # Generated markdown shortlists (committed by CI)
└── .github/workflows/
    └── weekly.yml       # GitHub Actions weekly cron + manual trigger
```

### Module details

**`scraper.py`** — Fetches Goodreads `popular_by_date` pages for each month in the trailing window. Extracts book links, then enriches each with a detail-page fetch to get rating, rating count, author, publication date, genre tags, and ISBN. Uses a persistent session with automatic retry/backoff for 429 and 5xx responses. Requests are jittered (2-3 seconds between fetches) to avoid bot fingerprinting. Detects Cloudflare challenge pages and logs errors instead of parsing garbage HTML. Validates all URLs against an allowlist before fetching.

**`db.py`** — SQLite database with a `seen_books` table. Primary key is ISBN-13 when available, falling back to a truncated SHA-256 hash of normalized title+author. Tracks both first-seen and last-checked ratings so books can be re-evaluated as ratings mature. Schema initialization uses explicit transactions (avoids `executescript()` implicit commit). Handles insert/update with rollback on failure.

**`filters.py`** — Single function `passes_filter()` that checks rating and rating count against caller-provided thresholds. No defaults — the caller (main.py) owns the policy values.

**`notify.py`** — Writes markdown shortlists to `shortlists/shortlist_YYYY-MM-DD.md`. Sends plaintext email via SMTP with STARTTLS (port 587, SSL context enforced). Includes proper `Date` and `Message-ID` headers. Validates recipients against header injection. Rejects port 465 (requires SMTP_SSL, not supported). SMTP config comes entirely from environment variables. Specific exception handling for auth failures vs. network errors vs. SMTP protocol errors.

**`main.py`** — Orchestrates the five-phase pipeline: fetch, dedup, enrich, filter, output. All configuration via CLI args or env vars — no hardcoded values. Connection cleanup guaranteed via try/finally.

## Database schema

```sql
CREATE TABLE seen_books (
    isbn13            TEXT PRIMARY KEY,  -- ISBN-13 or hash:xxxx fallback
    goodreads_id      TEXT,
    title             TEXT NOT NULL,
    author            TEXT NOT NULL,
    pub_date          DATE,
    first_seen_date   DATE NOT NULL,
    last_checked_date DATE,
    first_rating      REAL,
    first_rating_count INTEGER,
    last_rating       REAL,
    last_rating_count  INTEGER,
    passed_filter     BOOLEAN,
    genre_tags        TEXT,              -- comma-separated
    goodreads_url     TEXT,
    notes             TEXT
);
```

The `first_*` and `last_*` rating fields allow tracking how a book's rating evolves over time. The `--recheck` flag uses `last_checked_date` to find stale entries worth re-evaluating.

## How deduplication works

1. On each run, every candidate book is checked against `seen_books` by ISBN-13 (or title+author hash if no ISBN).
2. If already seen, the book is skipped entirely — no re-fetch, no re-filter, no email.
3. All books (pass or fail) are logged after enrichment, so they won't appear again in future runs.
4. The `--recheck` flag overrides this for previously-failed books older than 90 days, allowing them a second chance as their ratings mature.

## Output format

### Markdown shortlist (`shortlists/shortlist_2026-04-19.md`)

```markdown
# New book shortlist — 2026-04-19

## New this week (2)

### 1. Project Hail Mary — Andy Weir

- **Rating:** 4.52 (1,234,567 ratings)
- **Published:** 2021-05-04
- **Genres:** Science Fiction, Fiction, Audiobook
- **Goodreads:** https://www.goodreads.com/book/show/54493401

### 2. ...
```

### Email

Same content in plaintext, sent with subject line (threshold reflects `--min-rating`):
`[Books] 2 new candidates over 4.3 — 2026-04-19`

Only sent when at least one book passes the filter.

## Filter tuning notes

- The **4.3 rating threshold** is aggressive. Most new releases sit 3.8-4.2 in their first weeks before ratings stabilize. You'll get more hits from the 3-6 months back cohort than from week-one releases.
- The **500 rating count minimum** guards against small-sample bias (advance reader copies skew high). Lowering to 200 catches buzzy new releases earlier but introduces more noise.
- The **90-day window** balances rating maturity against freshness. A 30-day window misses most qualifying books; 180 days catches more but "new" starts to feel stale.
- Use `--recheck` quarterly to catch books that crossed 4.3 after their initial detection failed the filter.

## Dependencies

- [requests](https://pypi.org/project/requests/) >=2.31, <3 — HTTP client (with urllib3 retry support)
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/) >=4.12, <5 — HTML parsing
- [lxml](https://pypi.org/project/lxml/) >=5.0, <6.1 — Fast HTML parser backend for BeautifulSoup
- Python stdlib: `sqlite3`, `smtplib`, `ssl`, `argparse`, `logging`

## Known limitations

- **Goodreads scraping is fragile.** If Goodreads changes their HTML structure, the CSS selectors in `scraper.py` will need updating. The scraper logs warnings on parse failures rather than crashing. Cloudflare bot challenges are detected and logged, but cannot be bypassed.
- **Rate limiting.** Goodreads may return 429 responses under heavy load. The scraper retries automatically with exponential backoff (3s, 6s, 12s) for 429 and 5xx responses. Jittered delays (2-3 seconds) between requests keep volume low (~200 fetches max per run).
- **No pagination.** The new-releases pages are scraped as single pages. If Goodreads paginates them, only the first page is captured.
- **Translated works and re-releases** may show recent publication dates but aren't truly "new." These can slip through the filter.
- **Database persistence in CI** relies on GitHub Actions artifacts (400-day retention). If the artifact expires, the dedup log resets and previously-seen books may reappear.
