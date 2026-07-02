# CLAUDE.md — Project Intelligence

## Project Overview
New Release Book Filter is a scheduled Python scraper that monitors **Goodreads and The StoryGraph** for new books, filters each source by its own rating/count thresholds, deduplicates against a persistent SQLite log, and delivers curated shortlists via markdown and email. Runs weekly on GitHub Actions with optional monthly digest.

The final feed is a **union**: a book qualifies if it passes the Goodreads thresholds (rating ≥4.1, count ≥500) **or** the StoryGraph thresholds (rating >4.00, count >70). StoryGraph picks appear in their own section of the shortlist/email and carry a `Source` column in the catalog.

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

# Goodreads only (skip StoryGraph), or tune StoryGraph thresholds
python main.py --skip-storygraph --skip-email --recipient you@example.com
python main.py --sg-min-rating 4.0 --sg-min-count 70 --skip-email --recipient you@example.com
```

## Architecture
Pipeline: *fetch* (Goodreads + StoryGraph) → *dedup* (SQLite) → *enrich* (detail pages) → *filter* (per-source rating/count) → *genre exclusion* → *output* (markdown + email).

- `scraper.py` — Goodreads HTML/Apollo parsing. Shared `Book` dataclass (with `source`/`storygraph_url`/`storygraph_id` fields) and `book_link(book)` helper. Persistent session with retry/backoff, jittered delays, Cloudflare detection, URL allowlist.
- `storygraph.py` — StoryGraph scraper. Uses **`curl_cffi` with Chrome TLS impersonation** (plain `requests` gets a Cloudflare 403). `fetch_storygraph_new_releases` pages the browse and filters client-side by pub date (StoryGraph's sort/filter is login-gated, so coverage is popularity-biased). `enrich_storygraph_book` pulls the community average rating + count from the lazy `/books/<uuid>/community_reviews` fragment.
- `db.py` — SQLite with ISBN-13 primary key (fallback: title+author SHA-256 hash). `source` + `storygraph_url` columns. `is_seen_by_name` does cross-source dedup on normalized title+author (the ISBN/hash key can't match the same book across two sites). Tracks first/last ratings.
- `filters.py` — `passes_filter` (Goodreads) + `passes_storygraph_filter` (>4.0/>70, strictly-greater) + genre exclusion (`is_excluded_by_genre`, `EXCLUDED_GENRES`). Callers own policy.
- `notify.py` — Markdown writer + SMTP sender; separate Goodreads/StoryGraph sections. Port 587 + STARTTLS only.
- `main.py` — CLI orchestrator. Runs both sources; enriches passing/un-seen books only (saves requests).

## Key Conventions & Pitfalls
- **Dedup is permanent.** All books logged after enrichment; reappear only via `--recheck` for stale failures (90+ days). *Note:* `--recheck` uses `goodreads_url`, so StoryGraph-only failures (e.g. a book that hadn't yet hit >70 ratings) are not currently rechecked.
- **StoryGraph dedup is union-aware (name-based).** A StoryGraph candidate is suppressed only if its title+author was already **shown** (a `passed_filter=1` row exists, either source) or is **genre-excluded** (`_sg_name_suppressed` in `main.py`, via `get_seen_rows_by_name`). A Goodreads twin that merely *failed* its rating bar does NOT suppress it — this honors the OR union (a book can fail Goodreads' ≥500 bar yet clear StoryGraph's >70). When the **same** book passes *both* sources in one run, Phase 4e drops the StoryGraph copy so it lists once as Goodreads; `log_book` also refuses to let a StoryGraph write clobber a *passing* Goodreads row, and `write_catalog` collapses any residual cross-source duplicate to the Goodreads entry. Trade-off: StoryGraph books that were rated but failed (or whose Goodreads twin failed) are re-enriched each run until they pass or age out of the window.
- **StoryGraph needs `curl_cffi`.** Cloudflare blocks plain `requests` (403). If StoryGraph fetches start 403-ing, bump the `curl_cffi` browser profile (`IMPERSONATE` in `storygraph.py`).
- **ISBN fallback.** Missing ISBN → `hash:` prefix key using truncated SHA-256(normalized title+author).
- **Enrichment is best-effort.** Per-book crashes logged, not fatal.
- **Genre exclusion runs after enrichment (Phase 4c).** `EXCLUDED_GENRES` (case-insensitive substring) drops matching books from the shortlist and re-logs them `passed_filter=0` so they also leave the catalog + digest but stay deduped. Untagged books are kept (rating-based feed). Currently excludes `romance`, `romantasy`, `erotica`, `rom com`, `romantic comedy`.
- **SMTP config env-only.** No hardcoded values; rejects missing secrets with clear error.

## CI/Deploy
- **Weekly** (`weekly.yml`): Sundays 23:00 UTC. Runs `main.py`, commits shortlists/ + seen_books.db to repo.
- **Monthly digest** (`monthly.yml`): 1st of month, 15:00 UTC. Read-only DB query, no scraping.
- **Secrets required:** `BOOK_RECIPIENT`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.
- **Manual trigger:** Actions tab → "Weekly Book Filter" → "Run workflow".
