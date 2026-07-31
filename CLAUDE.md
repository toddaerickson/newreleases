# CLAUDE.md — Project Intelligence

## Project Overview
New Release Book Filter is a scheduled Python scraper that monitors **Goodreads and The StoryGraph** (plus opt-in Google Books) for new books, filters each source by its own rating/count thresholds, deduplicates against a persistent SQLite log, and delivers curated shortlists via markdown and email. It also produces a **second, independent list of award winners** (`awards.py`), which carries no rating bar. Runs weekly on GitHub Actions with optional monthly digest.

The final feed is a **union**: a book qualifies if it passes the Goodreads thresholds (rating ≥4.1, count ≥500) **or** the StoryGraph thresholds (rating >4.00, count >70). StoryGraph picks appear in their own section of the shortlist/email and carry a `Source` column in the catalog.

## Development Commands
```bash
# Install dependencies
pip install -r requirements.txt

# Lint + test (both run in CI on every push via .github/workflows/ci.yml)
pip install -r requirements-dev.txt
ruff check .
pytest

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

# Google Books is OPT-IN (off by default — see the note under Architecture)
python main.py --google-books --skip-email --recipient you@example.com

# Skip the award-winner scan (2 requests/source-year + rating lookups)
python main.py --skip-awards --skip-email --recipient you@example.com
```

## Architecture
Two independent lists, one run:
- **Releases:** *fetch* (Goodreads + StoryGraph + opt-in Google Books) → *dedup* (SQLite) → *enrich* (detail pages) → *filter* (per-source rating/count) → *genre exclusion* → *cross-source reconciliation* (Phase 4e) → *output*.
- **Awards:** *scan* (sfadb + Wikipedia) → *ledger dedup* (`award_winners`) → *append ratings* → *genre exclusion* → *output*. No rating bar, no release window, no `seen_books` dedup.

- `scraper.py` — Goodreads HTML/Apollo parsing. Shared `Book` dataclass (with `source` and the per-source `storygraph_*` / `google_books_*` url+id fields) and `book_link(book)` helper, which returns the link for whichever source the book came from. Persistent session with retry/backoff, jittered delays, Cloudflare detection, URL allowlist.
- `storygraph.py` — StoryGraph scraper. Uses **`curl_cffi` with Chrome TLS impersonation** (plain `requests` gets a Cloudflare 403). `fetch_storygraph_new_releases` pages the browse and filters client-side by pub date (StoryGraph's sort/filter is login-gated, so coverage is popularity-biased). `enrich_storygraph_book` pulls the community average rating + count from the lazy `/books/<uuid>/community_reviews` fragment.
- `db.py` — SQLite. `seen_books` keyed on ISBN-13 (fallback: title+author SHA-256 hash), with `source` + per-source url columns; `is_seen_by_name` / `get_seen_rows_by_name` do cross-source dedup on normalized title+author (the ISBN/hash key can't match the same book across two sites, which give it different edition ISBNs). Tracks first/last ratings. Separate `award_winners` table keyed on `award_key`. New columns go in **both** `SCHEMA` and `MIGRATIONS` — the latter runs `ALTER TABLE` on every connection and swallows "already exists", so a committed `seen_books.db` upgrades in place.
- `googlebooks.py` — **Opt-in** (`--google-books`, default off) volumes-API feed for recent science fiction. Plain `requests` against a documented JSON API — no scraping. Categories are BISAC-style *paths* ("Fiction / Science Fiction / General"), so `ALLOWED_GENRES` matches as a **substring**; exact set-membership would reject every categorised volume. Off by default because `ratingsCount` is absent on most volumes and single-digit on the rest, so it cannot clear a popularity bar — it is a discovery feed, not a quality filter.
- `filters.py` — **single source of truth for every threshold.** `GOODREADS_MIN_RATING/COUNT` (4.1/500, inclusive), `STORYGRAPH_MIN_RATING/COUNT` (4.0/70, strictly-greater), `GOOGLE_BOOKS_MIN_RATING/COUNT` (4.0/10, inclusive). `passes_filter` / `passes_storygraph_filter` / `passes_google_books_filter` + genre exclusion (`is_excluded_by_genre`, `EXCLUDED_GENRES`). Callers own policy. The CLI defaults, email footer, and catalog page all read these constants — never re-type a threshold as a literal anywhere else.
- `notify.py` — Markdown writer + SMTP sender; one section per contributing source plus an award-winners section. Port 587 + STARTTLS only. Thresholds in the email footer and catalog page are interpolated from the `filters.py` constants, never typed as literals.
- `awards.py` — **Second, independent list.** Scans `sfadb.com/<year>_Results` and Wikipedia's `<year> in literature` table for ~40 prizes, two requests per source-year. No rating bar — the jury is the quality signal; ratings are appended best-effort and left blank when unresolvable. `AWARD_ALLOWLIST` selects prizes (matched **longest-key-first and on word boundaries**, so "booker" cannot shadow "international booker" and "story prize" cannot match inside "Mark Lynton History Prize"); `CATEGORY_SKIP` drops short fiction, screenplays, art books, person awards, and the genre exclusions. On sfadb, a `<b>` wrapper is the book-vs-short-fiction discriminator. `choose_match` gates rating lookups on three independent checks (non-zero rating, author surname containment, title-similarity floor) because the observed junk for one title was a study guide, a workbook, and two phantom zero-rating editions — one of which carried the *correct* author.
- `main.py` — CLI orchestrator. Runs both sources; enriches passing/un-seen books only (saves requests). `collect_award_winners` handles Phase 6 and is wrapped whole: a broken award source logs and continues, never costing the release feed.

## Key Conventions & Pitfalls
- **Dedup is permanent.** All books logged after enrichment; reappear only via `--recheck` for stale failures (90+ days). *Note:* `--recheck` uses `goodreads_url`, so StoryGraph-only failures (e.g. a book that hadn't yet hit >70 ratings) are not currently rechecked.
- **StoryGraph dedup is union-aware (name-based).** A StoryGraph candidate is suppressed only if its title+author was already **shown** (a `passed_filter=1` row exists, either source) or is **genre-excluded** (`_sg_name_suppressed` in `main.py`, via `get_seen_rows_by_name`). A Goodreads twin that merely *failed* its rating bar does NOT suppress it — this honors the OR union (a book can fail Goodreads' ≥500 bar yet clear StoryGraph's >70). When the **same** book passes *both* sources in one run, Phase 4e drops the StoryGraph copy so it lists once as Goodreads; `log_book` also refuses to let a StoryGraph write clobber a *passing* Goodreads row, and `write_catalog` collapses any residual cross-source duplicate to the Goodreads entry. Trade-off: StoryGraph books that were rated but failed (or whose Goodreads twin failed) are re-enriched each run until they pass or age out of the window.
- **StoryGraph needs `curl_cffi`, and the profile rots.** Cloudflare blocks plain `requests` (403). `IMPERSONATE` in `storygraph.py` is currently **`firefox135`**: as of 2026-07-29 every `chrome*`/`safari*`/android profile returns 403 "Just a moment…", which is how the source died silently for weeks (`_get` returns `None` on 403, so the feed just looked quiet). When it 403s again, try newer profiles from `curl_cffi.requests.impersonate` before assuming the site changed. Sustained scraping also earns a temporary IP-level 403 that no profile clears — if everything suddenly 403s mid-session, wait rather than churn profiles.
- **ISBN fallback.** Missing ISBN → `hash:` prefix key using truncated SHA-256(normalized title+author).
- **Enrichment is best-effort.** Per-book crashes logged, not fatal.
- **Genre exclusion runs after enrichment (Phase 4c).** `EXCLUDED_GENRES` (case-insensitive substring) drops matching books from the shortlist and re-logs them `passed_filter=0` so they also leave the catalog + digest but stay deduped. Untagged books are kept (rating-based feed). Currently excludes `romance`, `romantasy`, `erotica`, `rom com`, `romantic comedy`.
- **SMTP config env-only.** No hardcoded values; rejects missing secrets with clear error.
- **Zero candidates = breakage, not a quiet week — and it is reported in the email.** A source returning nothing is invisible otherwise: its section just reads "(0)", every other filter still runs, and the run looks normal. So `run()` collects `down_sources`, and `notify` renders it as a **bold red first line** above the shortlist (`_outage_message` + the three format-specific banners), prefixes the subject with ⚠, and writes the same warning into the committed markdown. **The other sources always carry on** — an outage never short-circuits the pipeline.
  - **Goodreads** additionally sets `scraper_alarm`, so `run()` exits 1 *at the very end*, after the shortlist/catalog/email are written. The Actions run goes red instead of committing an empty shortlist and staying green, but the email still went out first.
  - **StoryGraph** is banner-only, deliberately: its browse coverage is genuinely patchy, and a red run every thin week would train the failure signal to be ignored.
  - `--skip-storygraph` is a *choice*, not an outage, and must never appear in `down_sources`.
  - An outage sends mail **even when nothing passed** — otherwise a week with a dead Goodreads and no other picks sends nothing at all, which is exactly the silence the warning exists to break.
- **Cross-source dedup applies to every secondary source.** `_name_suppressed` (not just StoryGraph — Google Books too) covers the case where the same book carries a different edition ISBN per site, which the `is_seen` ISBN/hash key cannot catch. Phase 4e then collapses same-run overlap by source priority: Goodreads > StoryGraph > Google Books.
- **Awards are a ledger, not a feed, and the first run SEEDS.** `award_winners` is keyed `slug(award)|year|slug(title)` — no source component, so a winner listed by both sfadb and Wikipedia collides harmlessly instead of appearing twice. `award_key` must be computed from the **source** title and never recomputed after enrichment: `_enrich_from_apollo` rewrites `Book.title` to the fuller `titleComplete` form ("Endling" → "Endling: A Novel"), which would mint a new key and re-list the winner every week. On an empty table every winner of the scanned years is "new", which would email months of stale announcements — so a first run records them all as already-reported, fetches no ratings, and shows nothing. Delete a row to have that winner listed.
- **The award rating budget defers, it does not drop.** `RATING_BUDGET_SECONDS` bounds wall-clock, not request count, because `storygraph._get` can burn ~96s per URL when the site times out. On exhaustion the loop breaks and leaves the remaining winners **unlogged**, so they are retried next week rather than silently lost. The deferred count must be computed from winners the loop *finished with* (reported **or** genre-excluded), not from `reported` alone.
- **Never format a possibly-None rating count.** `rating` and `rating_count` are nullable *independently* in the DB, so `f"{count:,}"` behind an `if rating is not None` guard raises `TypeError` — and in `write_catalog` that aborts the run at output time, after every page has already been scraped. Use `notify._rating_cell`.

## Testing
`ruff check .` + `pytest` run in CI on every push (`ci.yml`); `weekly.yml` is not a test signal — it runs once a week against live sites. Thresholds are pinned at their boundaries (Goodreads inclusive vs StoryGraph strictly-greater), and `tests/test_pipeline.py` drives `run()` end to end with the three fetchers monkeypatched. **That smoke test is the important one** — this pipeline's real failure mode is an undefined name or a renamed kwarg, not a subtly wrong rating.

**Book-source** HTML parsing (Goodreads, StoryGraph browse) is deliberately **not** fixture-tested: a saved page keeps passing after the site changes its markup, so it would give false assurance about the exact thing it appears to cover. Upstream drift is caught by the runtime canary above instead.

**Award-source** parsing *is* fixture-tested (`tests/fixtures/`), because those assertions encode structural facts that were verified once and are otherwise invisible — sfadb bolding its game-writing and screenplay winners, Wikipedia putting `rowspan` on the category and ref columns, `"story prize"` matching inside `"Mark Lynton History Prize"`, and Goodreads autocomplete ranking a study guide and two phantom editions above the real book. These are regression guards on *our* logic, not proof the sites are unchanged; the refresh commands are in the test module's docstring.

## CI/Deploy
- **Weekly** (`weekly.yml`): Sundays 23:00 UTC. Runs `main.py`, commits shortlists/ + seen_books.db to repo.
- ⚠️ **The award ledger's rollback depends on the commit step staying gated on success.** It has no `if:`, so it defaults to `if: success()` and is skipped when `main.py` exits 1 (e.g. email delivery failed). That discards the run's DB writes, which is what makes "winner recorded as listed" and "winner actually emailed" atomic. Adding `if: always()` would silently convert every failed-email week into winners marked listed but never shown.
- **Local runs must not touch the tracked DB.** `get_conn()` defaults to the committed `seen_books.db`, so pass `--db-path /tmp/copy.db` when testing, or a botched run gets committed by the next cron.
- **Monthly digest** (`monthly.yml`): 1st of month, 15:00 UTC. Read-only DB query, no scraping.
- **Secrets required:** `BOOK_RECIPIENT`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.
- **Manual trigger:** Actions tab → "Weekly Book Filter" → "Run workflow".
