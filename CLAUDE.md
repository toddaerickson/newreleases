# CLAUDE.md — Project Intelligence

## Project Overview
New Release Book Filter is a scheduled Python scraper that monitors Goodreads for new books, filters by rating (≥4.3) and rating count (≥500), deduplicates against a persistent SQLite log, and delivers curated shortlists via markdown and email. Runs weekly on GitHub Actions with optional monthly digest.

## Development Commands
```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (markdown only, no email)
python main.py --skip-email --recipient you@example.com

# Run with custom filters
python main.py --window 30 --min-rating 4.0 --min-count 200 --skip-email --recipient you@example.com

# Re-check previously failed books (quarterly)
python main.py --recheck --skip-email --recipient you@example.com

# Monthly digest (read-only, no scraping)
python digest.py --days 30 --skip-email --recipient you@example.com
```

## Architecture
Five-phase pipeline: *fetch* (Goodreads) → *dedup* (SQLite) → *enrich* (detail pages) → *filter* (rating/count) → *output* (markdown + email).

- `scraper.py` — HTML parsing (BeautifulSoup/lxml), persistent session with retry/backoff, jittered delays, Cloudflare detection, URL allowlist.
- `db.py` — SQLite with ISBN-13 primary key (fallback: title+author SHA-256 hash). Tracks first/last ratings for re-evaluation.
- `filters.py` — Single-responsibility rating/count checker (no defaults — caller owns policy).
- `notify.py` — Markdown writer + SMTP sender. Port 587 + STARTTLS only.
- `main.py` — CLI orchestrator. Enriches passing books only (saves requests).

## Key Conventions & Pitfalls
- **Dedup is permanent.** All books logged after enrichment; reappear only via `--recheck` for stale failures (90+ days).
- **ISBN fallback.** Missing ISBN → `hash:` prefix key using truncated SHA-256(normalized title+author).
- **Enrichment is best-effort.** Per-book crashes logged, not fatal.
- **SMTP config env-only.** No hardcoded values; rejects missing secrets with clear error.

## CI/Deploy
- **Weekly** (`weekly.yml`): Sundays 23:00 UTC. Runs `main.py`, commits shortlists/ + seen_books.db to repo.
- **Monthly digest** (`monthly.yml`): 1st of month, 15:00 UTC. Read-only DB query, no scraping.
- **Secrets required:** `BOOK_RECIPIENT`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.
- **Manual trigger:** Actions tab → "Weekly Book Filter" → "Run workflow".
