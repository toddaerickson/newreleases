# Roadmap — Code Review and Feature Review

Big-picture assessment of what's solid, what needs attention, and what would make this tool significantly more useful.

---

## Code review: outstanding issues

### HIGH priority

**1. Scraper selector fragility — no validation that selectors are still working**

The scraper relies on ~15 CSS selectors across `scraper.py` to extract data from Goodreads HTML. If Goodreads ships a redesign (they do this periodically), selectors silently return `None` and every book fails enrichment. There is no mechanism to detect this.

*Fix:* Add a "canary" check — on each run, fetch a known book (e.g., a classic with a stable page) and verify that rating, author, and title all parse successfully. If the canary fails, abort the run and send an alert email instead of a shortlist. This prevents silent weeks of zero results.

**2. No tests**

Zero test coverage. Every change to the filter logic, scraper selectors, or dedup hash is deployed untested.

*Fix:* Add pytest tests for:
- `filters.passes_filter()` — trivial to unit test with mock Book objects
- `db.is_seen()` / `db.log_book()` — test against an in-memory SQLite database
- `notify.write_shortlist()` — verify markdown output format
- `scraper._is_allowed_url()` — verify URL validation
- Integration test: feed known HTML fixtures through `enrich_book()` and verify parsed fields

**3. ~~Database artifact persistence is brittle~~ (DONE)**

~~The SQLite database is stored as a GitHub Actions artifact with 400-day retention. If the artifact expires, or if GitHub changes artifact retention policies, the entire dedup history is lost and previously-seen books reappear.~~

*Fixed:* `seen_books.db` is now committed to the repo. Removed from `.gitignore`, added to the weekly workflow's `git add` step. Artifact upload/download steps removed. Both weekly and monthly workflows use `concurrency: group: book-pipeline` to prevent race conditions.

**4. `search_and_enrich()` is dead code**

`scraper.search_and_enrich()` is defined but never called from any module. It was built for a future use case (manual title+author lookup) but is currently untested and unmaintained.

*Fix:* Either wire it up (see Feature #3 below) or remove it to reduce surface area.

### MEDIUM priority

**5. No retry logic on transient HTTP failures**

A single 429 or 5xx response causes that month's entire new-release page to be skipped. No retry, no backoff, no indication of how many books were lost.

*Fix:* Add exponential backoff with 2-3 retries for 429 and 5xx responses in `_get()`. Log the retry count. Abort the run if more than N pages fail consecutively (indicates a systemic block).

**6. Email subject hardcodes "4.3" threshold** — ✅ RESOLVED

The threshold was removed from the subject entirely rather than interpolated: the
feed is a union of per-source bars, so no single number describes every book in
it. The applied thresholds are stated in the email footer, interpolated from the
`filters.py` constants. All thresholds now live in `filters.py`
(`GOODREADS_MIN_RATING` etc.) and every report site reads them from there, so the
drift class is closed, not just this instance.

**7. No monitoring or alerting on silent failures** — ✅ RESOLVED

A zero-candidate Goodreads fetch now logs an error and sets `scraper_alarm`, and
`run()` exits 1 at the very end — after the shortlist, catalog, and email are
written, so a Goodreads outage still delivers whatever StoryGraph found instead of
costing a whole week's feed. The non-zero exit turns the weekly Actions run red,
which is the alert. A zero-candidate StoryGraph fetch logs an error but does not
fail the run, since its browse coverage is genuinely patchy.

**8. GitHub Actions dependencies pinned to mutable tags**

`actions/checkout@v4`, `actions/setup-python@v5`, etc. are mutable tags that can be updated by the upstream maintainer. A compromised action could exfiltrate secrets.

*Fix:* Pin to commit SHAs:
```yaml
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4
uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
```

### LOW priority

**9. Dedup hash collision is theoretically possible**

The fallback primary key (`hash:` + 16 hex chars = 64 bits) has a ~1-in-18-trillion collision probability per pair. Acceptable at current scale, but if two books share a normalized title+author (different editions, different publishers), one is silently suppressed.

*Fix:* Expand the hash to 32 hex chars (128 bits) for negligible collision risk. Also consider a secondary dedup check on `goodreads_id` when available.

**10. `shortlists/` directory grows without bound**

Every weekly run adds a markdown file. Over years this accumulates. Not a problem at current scale but worth noting.

*Fix:* Add a cleanup step to the workflow that deletes shortlists older than 1 year, or configure a `.gitattributes` to keep the directory lean.

---

## Feature review: potential enhancements

### HIGH value

**1. Genre whitelist/blacklist filter**

Currently every genre passes if the rating threshold is met. Adding a configurable genre filter (via env var or config file) would cut noise significantly.

Example: `GENRE_WHITELIST=science-fiction,thriller,espionage,finance` would reduce weekly candidates from ~3-8 to ~0-2.

*Implementation:* Add `genre_whitelist` and `genre_blacklist` parameters to `passes_filter()`. Parse from a comma-separated env var. Check against `book.genre_tags`.

**2. Purchase/library links in the shortlist**

The shortlist shows Goodreads URLs but no purchase or library links. Adding Amazon, Kobo, and Libby/OverDrive links would make the shortlist actionable without leaving the email.

*Implementation:* Construct Amazon search URL from title+author (`https://www.amazon.com/s?k={title}+{author}&i=digital-text`). For Libby, use the OverDrive search API if a library card is configured.

**3. Manual book lookup mode**

The existing `search_and_enrich()` function (currently dead code) could power a CLI mode: `python main.py --lookup "Project Hail Mary" "Andy Weir"` that fetches and displays a single book's details without running the full pipeline.

*Implementation:* Add a `--lookup` flag to argparse that takes title and author, calls `search_and_enrich()`, and prints the result.

### MEDIUM value

**4. HTML email with cover images**

The current email is plaintext. An HTML version with book cover thumbnails, color-coded ratings, and clickable links would be much more scannable.

*Implementation:* Add an HTML alternative to the MIME message. Goodreads book pages include Open Graph `og:image` meta tags — scrape those during enrichment and embed as `<img>` tags.

**5. Configurable data sources beyond Goodreads**

Goodreads is the single point of failure. Adding Hardcover API (which issues API keys, unlike Goodreads) as a fallback or alternative source would improve resilience.

*Implementation:* Abstract the scraper behind a `BookSource` interface. Add a `HardcoverSource` that uses their REST API. Fall back to it when Goodreads returns errors.

**6. Rating history tracking and trend detection**

The database already stores `first_rating` and `last_rating`. A report showing "books whose rating climbed above 4.3 since last check" would surface late bloomers.

*Implementation:* Add a `--trending` flag that queries `seen_books WHERE last_rating >= 4.3 AND first_rating < 4.3`. Include these in a separate section of the shortlist.

**7. Weekly digest summary statistics**

Add a header to the email/shortlist with run stats: books scanned, books enriched, books passed, books failed, scraper errors, enrichment failures. Makes it easy to spot degradation without reading logs.

*Implementation:* Collect counters during `run()` and pass them to `write_shortlist()` / `send_email()`.

### LOW value / nice-to-have

**8. Calibre integration**

Auto-import purchased EPUBs into Calibre and tag them with Goodreads metadata. Useful if the pipeline eventually connects to a purchase workflow.

**9. Slack/Discord notification alternative**

For users who prefer chat notifications over email. Add a webhook URL env var and a `notify_slack()` function alongside `send_email()`.

**10. Web dashboard**

A simple static site (GitHub Pages) that renders the shortlists directory as a browsable archive with search and filtering. Overkill for a single-user tool but nice if shared.

---

## Recommended next steps (in order)

1. Add the canary check (issue #1) — prevents silent scraper death
2. Add pytest tests for filters, db, and notify (issue #2) — prevents regression
3. Fix database persistence (issue #3) — prevents dedup history loss
4. Add genre whitelist filter (feature #1) — highest-value feature for reducing noise
5. Add purchase/library links (feature #2) — makes the shortlist actionable
6. Wire up the manual lookup mode (feature #3) — uses existing dead code
7. Add retry logic (issue #5) — improves resilience
8. Pin GitHub Actions to SHAs (issue #8) — security hygiene
