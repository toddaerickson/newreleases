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

The tool scans Goodreads for books published in the last 90 days, filters for ratings >= 4.1 with at least 500 ratings, and outputs a markdown file to `shortlists/`. If email is configured, it sends the shortlist as a digest. Books are logged to a local SQLite database so they only appear once.

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

# Monthly digest — summarize books that passed the filter in the last 30 days
python digest.py --skip-email --recipient you@example.com

# Digest with custom lookback window
python digest.py --days 60 --skip-email --recipient you@example.com
```

## How it works

The weekly run produces **two independent lists**: new releases that cleared a
rating bar, and award winners announced since the last run.

```text
NEW RELEASES (a union — a book qualifies if ANY source's bar is met)

  Goodreads (>=4.1, >=500)   StoryGraph (>4.0, >70)   Google Books (opt-in)
              \                      |                      /
               +---------------------+----------------------+
                                     v
                  Dedup against SQLite log (seen_books.db)
                     ISBN-13/hash key, plus a title+author
                     check so one book is not listed twice
                     under two sites' edition ISBNs
                                     v
                  Enrich unseen books (rating, genres, pub date)
                                     v
                  Per-source rating filter, then genre exclusion
                                     v
                  Prefer Goodreads when one book clears two sources
                                     v
                  Log every book (pass or fail) for future dedup

AWARD WINNERS (no rating bar — the jury is the signal)

  sfadb.com/<year>_Results        Wikipedia "<year> in literature"
              \                              /
               +----------------------------+
                             v
        Dedup against award_winners on award+year+title
                             v
        Append Goodreads + StoryGraph ratings, best-effort
                             v
        Genre exclusion (recorded, but not shown)

                          BOTH LISTS
                             v
              shortlist markdown + email digest
```

Award winners are deliberately **not** routed through the book pipeline. Most
prizes honour previous-year books, so a winner is normally already a `seen_books`
row and sits outside the release window — routing them through dedup would
silently suppress nearly all of them.

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

The weekly workflow runs automatically every **Sunday at 6 PM Central** (23:00 UTC). Shortlist markdown files and the SQLite database are committed back to the repo.

A **monthly digest** workflow runs on the **1st of each month at 10 AM Central** (15:00 UTC), emailing a summary of all books that passed the filter in the previous 30 days.

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
| `--min-rating F` | `4.1` | Minimum Goodreads rating to pass filter |
| `--min-count N` | `500` | Minimum number of ratings to pass filter |
| `--recipient EMAIL` | `BOOK_RECIPIENT` env var | Email address to send the digest to |
| `--recheck` | off | Re-check previously failed books that haven't been checked in 90+ days |
| `--skip-email` | off | Write markdown shortlist only, skip email |
| `--backfill-genres` | off | Re-enrich all passed books missing genres, then exit |
| `--skip-storygraph` | off | Skip the StoryGraph source (Goodreads only) |
| `--sg-min-rating F` | `4.0` | StoryGraph min rating (exclusive) |
| `--sg-min-count N` | `70` | StoryGraph min ratings count (exclusive) |
| `--google-books` | off | Enable the Google Books science-fiction source |
| `--gb-min-rating F` | `4.0` | Google Books min rating |
| `--gb-min-count N` | `10` | Google Books min ratings count |
| `--skip-awards` | off | Skip the award-winner scan |

The Google Books source is **off by default**. Its `ratingsCount` is absent
on most volumes and in single digits on the rest, so it cannot meaningfully
clear a popularity bar; it is a discovery feed for recent science fiction,
not a quality filter. Enable it with `--google-books` if you want the extra
coverage and are willing to accept noisier picks.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .      # catches undefined names, unused imports, bad f-strings
pytest            # 56 tests, no network access required
```

Both run in CI on every push (`.github/workflows/ci.yml`). The tests deliberately
cover the pure logic — thresholds, dedup keys, cross-source write guards, output
assembly — plus an end-to-end `run()` smoke test with the three fetchers stubbed.
That smoke test is the one that matters: the pipeline's real failure mode is an
undefined name or a renamed kwarg, which otherwise stays hidden until the weekly
cron fires.

HTML parsing is intentionally **not** unit-tested against saved fixtures. A frozen
fixture keeps passing after Goodreads changes its markup, so it would provide
false assurance about the one thing it appears to test. Upstream drift is caught
at runtime instead: a zero-candidate fetch logs an error and exits non-zero, which
turns the weekly Actions run red.

## Project structure

```text
newreleases/
├── main.py              # Orchestrator — phases 1-5, CLI entry point
├── digest.py            # Monthly digest — queries DB, emails summary
├── scraper.py           # Goodreads scraping (new releases + book detail pages)
├── filters.py           # Rating/count filter logic
├── db.py                # SQLite seen_books persistence and dedup
├── notify.py            # Markdown shortlist writer + email sender
├── requirements.txt     # Python dependencies
├── .gitignore
├── shortlists/          # Generated markdown shortlists (committed by CI)
├── seen_books.db        # SQLite database (committed by CI)
└── .github/workflows/
    ├── weekly.yml       # Weekly scrape + filter + email
    └── monthly.yml      # Monthly digest email
```

### Module details

**`scraper.py`** — Fetches Goodreads `popular_by_date` pages for each month in the trailing window. Extracts book links, then enriches each with a detail-page fetch to get rating, rating count, author, publication date, genre tags, and ISBN. Uses a persistent session with automatic retry/backoff for 429 and 5xx responses. Requests are jittered (2-3 seconds between fetches) to avoid bot fingerprinting. Detects Cloudflare challenge pages and logs errors instead of parsing garbage HTML. Validates all URLs against an allowlist before fetching.

**`db.py`** — SQLite database with a `seen_books` table. Primary key is ISBN-13 when available, falling back to a truncated SHA-256 hash of normalized title+author. Tracks both first-seen and last-checked ratings so books can be re-evaluated as ratings mature. Schema initialization uses explicit transactions (avoids `executescript()` implicit commit). Handles insert/update with rollback on failure.

**`filters.py`** — The single source of truth for every threshold: `GOODREADS_MIN_RATING/COUNT` (4.1/500, inclusive), `STORYGRAPH_MIN_RATING/COUNT` (4.0/70, strictly-greater), `GOOGLE_BOOKS_MIN_RATING/COUNT` (4.0/10, inclusive). One predicate per source, plus genre exclusion (`is_excluded_by_genre`, `EXCLUDED_GENRES`). The CLI defaults, email footer, and catalog page all read these constants, so a threshold cannot be reported differently from the one applied.

**`storygraph.py`** — StoryGraph scraper. Uses `curl_cffi` with Chrome TLS impersonation; plain `requests` gets a Cloudflare 403. StoryGraph's own sort/filter is login-gated, so the browse is paged and filtered client-side by publication date — coverage is popularity-biased. Ratings come from the lazy `/books/<uuid>/community_reviews` fragment.

**`googlebooks.py`** — Opt-in (`--google-books`) volumes-API feed for recent science fiction. A documented JSON API, not scraping. Off by default: `ratingsCount` is absent on most volumes and single-digit on the rest, so it is a discovery feed rather than a quality filter.

**`awards.py`** — Award-winner scan, ~40 prizes in two requests per source-year. Parses `sfadb.com/<year>_Results` (book-length winners are wrapped in `<b>`, which distinguishes them from short fiction and person awards) and Wikipedia's `<year> in literature` awards table via the API's rendered HTML. `AWARD_ALLOWLIST` decides which prizes count, `CATEGORY_SKIP` drops short fiction, screenplays, art books, and the genre exclusions. Rating lookups go through `choose_match`, which rejects phantom zero-rating editions and study-guide impostors rather than showing the wrong book's rating.

**`notify.py`** — Writes markdown shortlists to `shortlists/shortlist_YYYY-MM-DD.md`. Sends plaintext email via SMTP with STARTTLS (port 587, SSL context enforced). Includes proper `Date` and `Message-ID` headers. Validates recipients against header injection. Rejects port 465 (requires SMTP_SSL, not supported). SMTP config comes entirely from environment variables. Specific exception handling for auth failures vs. network errors vs. SMTP protocol errors. Also provides `send_digest_email()` for the monthly digest.

**`main.py`** — Orchestrates the pipeline: fetch, dedup, enrich, filter, genre exclusion, cross-source reconciliation, award scan, output. All configuration via CLI args or env vars — no hardcoded values. Connection cleanup guaranteed via try/finally. A zero-candidate Goodreads fetch is treated as breakage rather than a quiet week: it logs an error and exits non-zero *after* the shortlist, catalog, and email are written, so a Goodreads outage still delivers what the other sources found while the Actions run goes red.

**`digest.py`** — Monthly digest entry point. Queries `seen_books` for books that passed the filter within a configurable lookback window (default 30 days) and emails a summary. Does not scrape or modify the database — read-only. CLI flags: `--days`, `--recipient`, `--skip-email`.

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
    description       TEXT,
    source            TEXT DEFAULT 'goodreads',
    storygraph_url    TEXT,
    google_books_url  TEXT,
    notes             TEXT
);

CREATE TABLE award_winners (
    award_key       TEXT PRIMARY KEY,  -- slug(award)|year|slug(title)
    award_name      TEXT NOT NULL,
    award_year      INTEGER NOT NULL,
    category        TEXT,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    listed_date     DATE NOT NULL,
    genre_tags      TEXT,
    goodreads_url   TEXT,
    goodreads_rating REAL,
    goodreads_rating_count INTEGER,
    storygraph_url  TEXT,
    storygraph_rating REAL,
    storygraph_rating_count INTEGER
);
```

The `first_*` and `last_*` rating fields allow tracking how a book's rating evolves over time. The `--recheck` flag uses `last_checked_date` to find stale entries worth re-evaluating.

Columns added after the initial schema are also listed in `MIGRATIONS`, which runs
`ALTER TABLE` on every connection and ignores "already exists" — so an existing
`seen_books.db` upgrades in place with no manual step.

`award_winners` is a separate ledger keyed on award+year+title. There is no source
component in the key, so a book listed by both sfadb and Wikipedia collides
harmlessly instead of being reported twice. **On a first run the table is empty,
which would mean months of stale announcements** — so the first run seeds every
winner as already-reported, fetches no ratings, and shows nothing. Delete a row to
have that winner listed.

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

Same content in plaintext, sent with subject line:
`[Books] 2 new candidates — 2026-04-19`

The subject deliberately carries no rating threshold: the feed is a union of
per-source bars, so no single number describes every book in it. The applied
thresholds are stated in the email footer instead.

Only sent when at least one book passes the filter.

## Filter tuning notes

- The **4.1 rating threshold** is already fairly aggressive. Most new releases sit 3.8-4.2 in their first weeks before ratings stabilize. You'll get more hits from the 3-6 months back cohort than from week-one releases.
- The **500 rating count minimum** guards against small-sample bias (advance reader copies skew high). Lowering to 200 catches buzzy new releases earlier but introduces more noise.
- The **90-day window** balances rating maturity against freshness. A 30-day window misses most qualifying books; 180 days catches more but "new" starts to feel stale.
- Use `--recheck` quarterly to catch books that crossed 4.1 after their initial detection failed the filter.
- Thresholds are defined once in `filters.py` (`GOODREADS_MIN_RATING`, `GOODREADS_MIN_COUNT`, and the StoryGraph/Google Books equivalents). The CLI defaults, email footer, and catalog page all read them from there, so changing the constant changes every place the number is reported.

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
- **Database in git.** `seen_books.db` is committed to the repo by the weekly workflow. Binary diffs are small at this scale (~200 KB/year). The database is the single source of dedup history.
