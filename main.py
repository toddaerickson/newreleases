#!/usr/bin/env python3
"""
New Release Book Filter — Orchestrator

Scrapes Goodreads and StoryGraph new releases, filters each source by its own
rating/count thresholds (see filters.py for the defaults), deduplicates against
a persistent SQLite log, and outputs a markdown shortlist plus optional email
notification. Google Books is an opt-in third source (--google-books).
"""

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

from db import (
    get_conn, is_seen, get_seen_rows_by_name, log_book, get_books_for_recheck,
    get_all_catalog_books, get_books_missing_genres,
    award_seen, count_award_rows, log_award_winner,
)
from scraper import Book, fetch_new_releases, enrich_book
from storygraph import fetch_storygraph_new_releases, enrich_storygraph_book
from googlebooks import fetch_google_books_new_releases
from filters import (
    passes_filter, passes_storygraph_filter, passes_google_books_filter,
    is_excluded_by_genre, genres_excluded,
    GOODREADS_MIN_RATING, GOODREADS_MIN_COUNT,
    STORYGRAPH_MIN_RATING, STORYGRAPH_MIN_COUNT,
    GOOGLE_BOOKS_MIN_RATING, GOOGLE_BOOKS_MIN_COUNT,
)
from notify import write_shortlist, send_email, write_catalog
import awards as awards_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def backfill_genres(conn) -> None:
    """Re-enrich all passed books missing genres and write results to DB."""
    missing = get_books_missing_genres(conn)
    logger.info("Backfilling genres for %d books...", len(missing))
    fixed = 0
    for row in missing:
        b = Book(
            title=row["title"],
            author=row["author"],
            goodreads_url=row["goodreads_url"],
            isbn13=row["isbn13"],
            goodreads_id=row.get("goodreads_id"),
        )
        try:
            enrich_book(b, force=True)
        except Exception as e:
            logger.error("Backfill enrichment crashed for %r: %s", b.title, e)
            continue
        if b.genre_tags:
            log_book(
                conn,
                title=b.title,
                author=b.author,
                isbn13=b.isbn13,
                goodreads_id=b.goodreads_id,
                pub_date=b.pub_date,
                rating=b.rating,
                rating_count=b.rating_count,
                passed_filter=True,
                genre_tags=", ".join(b.genre_tags),
                goodreads_url=b.goodreads_url,
            )
            fixed += 1
            logger.info("Backfilled %r → %s", b.title, ", ".join(b.genre_tags))
        else:
            logger.warning("Still no genres for %r after backfill", b.title)
    logger.info("Backfill complete: %d/%d books updated", fixed, len(missing))


def _norm_name(book: Book) -> tuple[str, str]:
    return (book.title.strip().lower(), book.author.strip().lower())


def _name_suppressed(conn, book: Book) -> bool:
    """Union-aware cross-run dedup for a secondary-source candidate.

    Used for StoryGraph and Google Books, whose ISBN/hash key routinely differs
    from the Goodreads key for the same title (different edition ISBN), so the
    plain is_seen() check cannot spot a cross-source repeat.

    Suppress ONLY if this title was already SHOWN in a feed (a passed_filter=1
    row exists, any source) or is a genre-excluded title (romance, etc.).
    A Goodreads twin that merely FAILED its rating bar does NOT suppress the
    secondary pick — that is the whole point of the OR union.
    """
    for row in get_seen_rows_by_name(conn, book.title, book.author):
        if row["passed_filter"]:
            return True
        tags = [t.strip() for t in (row["genre_tags"] or "").split(",") if t.strip()]
        if genres_excluded(tags):
            return True
    return False


def _apply_genre_exclusion(conn, books: list[Book]) -> tuple[list[Book], int]:
    """Drop books tagged with an excluded genre; re-log them passed_filter=0.

    Works for any source — the Book carries its own source/url fields. Excluded
    books stay "seen" (won't reappear) but leave the catalog + digest.
    """
    kept: list[Book] = []
    excluded = 0
    for book in books:
        if is_excluded_by_genre(book):
            excluded += 1
            logger.info("Excluding %r by genre: %s", book.title, ", ".join(book.genre_tags))
            log_book(
                conn,
                title=book.title,
                author=book.author,
                isbn13=book.isbn13,
                goodreads_id=book.goodreads_id,
                pub_date=book.pub_date,
                rating=book.rating,
                rating_count=book.rating_count,
                passed_filter=False,
                genre_tags=", ".join(book.genre_tags) if book.genre_tags else None,
                goodreads_url=book.goodreads_url,
                description=book.description,
                source=book.source,
                storygraph_url=book.storygraph_url,
                google_books_url=book.google_books_url,
            )
        else:
            kept.append(book)
    return kept, excluded


def collect_award_winners(conn) -> tuple[list, list[str]]:
    """Scan the award sources and return (winners to report, source notes).

    Award winners bypass the book pipeline entirely — no rating filter, no
    release window, no seen_books dedup. "Already reported" is tracked in
    award_winners, keyed on award+year+title, so each winner is listed exactly
    once, the week it is first detected.

    On a first run (empty table) every winner of the scanned years is new, which
    would be months of stale announcements. Seed instead: record them all as
    already reported, make no rating requests, and report nothing.
    """
    winners, notes = awards_mod.fetch_award_winners()
    logger.info("Award scan: %s", " · ".join(notes) if notes else "no sources responded")

    fresh = [w for w in winners if not award_seen(conn, w.award_key)]
    if not fresh:
        logger.info("No new award winners (%d already on record)", len(winners))
        return [], notes

    if count_award_rows(conn) == 0:
        logger.info("First award run — seeding %d winner(s) as already reported, "
                    "no ratings fetched. Delete a row to have it listed.", len(fresh))
        for w in fresh:
            log_award_winner(
                conn, award_key=w.award_key, award_name=w.award_name,
                award_year=w.award_year, category=w.category,
                title=w.title, author=w.author,
            )
            logger.info("  seeded: %s — %s (%s)", w.title, w.author, w.award_label)
        return [], notes

    logger.info("%d new award winner(s) — resolving ratings", len(fresh))
    reported: list = []
    memo: dict = {}
    deadline = time.monotonic() + awards_mod.RATING_BUDGET_SECONDS
    # Counts winners this loop has *finished with*, whether they ended up reported
    # or genre-excluded. `reported` alone would undercount, overstating how many
    # are actually deferred.
    handled = 0

    for winner in fresh:
        if time.monotonic() > deadline:
            # Out of time, not out of winners: leave the rest unlogged so they
            # are picked up next week rather than silently dropped.
            logger.warning("Award rating budget exhausted — %d winner(s) deferred "
                           "to next run", len(fresh) - handled)
            break
        try:
            awards_mod.append_ratings(winner, memo)
        except Exception as e:
            logger.error("Rating lookup crashed for %r: %s", winner.title, e)

        if genres_excluded(winner.genre_tags):
            # Recorded, so it is not reconsidered, but not shown.
            logger.info("Excluding award winner %r by genre: %s",
                        winner.title, ", ".join(winner.genre_tags))
            log_award_winner(
                conn, award_key=winner.award_key, award_name=winner.award_name,
                award_year=winner.award_year, category=winner.category,
                title=winner.title, author=winner.author,
                genre_tags=", ".join(winner.genre_tags) if winner.genre_tags else None,
            )
            handled += 1
            continue

        log_award_winner(
            conn, award_key=winner.award_key, award_name=winner.award_name,
            award_year=winner.award_year, category=winner.category,
            title=winner.title, author=winner.author,
            genre_tags=", ".join(winner.genre_tags) if winner.genre_tags else None,
            goodreads_url=winner.goodreads_url,
            goodreads_rating=winner.goodreads_rating,
            goodreads_rating_count=winner.goodreads_rating_count,
            storygraph_url=winner.storygraph_url,
            storygraph_rating=winner.storygraph_rating,
            storygraph_rating_count=winner.storygraph_rating_count,
        )
        reported.append(winner)
        handled += 1

    logger.info("%d award winner(s) to report", len(reported))
    return reported, notes


def run(
    window_days: int = 90,
    min_rating: float = GOODREADS_MIN_RATING,
    min_rating_count: int = GOODREADS_MIN_COUNT,
    recipient: str | None = None,
    recheck: bool = False,
    skip_email: bool = False,
    backfill: bool = False,
    skip_storygraph: bool = False,
    sg_min_rating: float = STORYGRAPH_MIN_RATING,
    sg_min_count: int = STORYGRAPH_MIN_COUNT,
    google_books: bool = False,
    gb_min_rating: float = GOOGLE_BOOKS_MIN_RATING,
    gb_min_count: int = GOOGLE_BOOKS_MIN_COUNT,
    skip_awards: bool = False,
    db_path: Path | None = None,
) -> None:
    conn = get_conn(db_path) if db_path else get_conn()
    passed: list[Book] = []  # Initialize before try to avoid UnboundLocalError
    sg_passed: list[Book] = []
    gb_passed: list[Book] = []
    award_winners: list = []
    award_notes: list[str] = []
    scraper_alarm = False  # Set when a source returns nothing it plausibly could
    down_sources: list[str] = []  # Named in bold red on the first line of the email

    if backfill:
        try:
            backfill_genres(conn)
        finally:
            conn.close()
        return

    recipient = (recipient or os.environ.get("BOOK_RECIPIENT", "")).strip("'\" ")
    if not recipient:
        logger.error("No recipient configured. Set BOOK_RECIPIENT env var or use --recipient.")
        sys.exit(1)

    try:
        today = date.today()

        # --- Phase 1: Fetch new releases ---
        logger.info("Fetching new releases (trailing %d days)...", window_days)
        candidates = fetch_new_releases(window_days)
        logger.info("Found %d Goodreads candidates", len(candidates))
        # Canary: Goodreads is the primary source and its listing pages always
        # carry books over a 90-day window. Zero candidates means the markup
        # changed, we were blocked, or the URLs moved — not "a quiet week". Every
        # downstream phase then degrades to a silent no-op, the run exits 0, and
        # the weekly cron stays green while the feed is quietly dead. Flag it now
        # and fail the process at the very end (see `scraper_alarm`) so the
        # Actions run goes red and GitHub emails about it.
        if not candidates:
            logger.error(
                "Goodreads returned 0 candidates — the scraper is probably broken "
                "(markup change, Cloudflare block, or moved URL), not merely idle."
            )
            scraper_alarm = True
            down_sources.append("Goodreads")

        sg_candidates: list[Book] = []
        if not skip_storygraph:
            logger.info("Fetching StoryGraph new releases (trailing %d days)...", window_days)
            sg_candidates = fetch_storygraph_new_releases(window_days)
            logger.info("Found %d StoryGraph candidates", len(sg_candidates))
            if not sg_candidates:
                logger.error(
                    "StoryGraph returned 0 candidates — check the curl_cffi "
                    "impersonation profile (Cloudflare 403) before assuming no releases."
                )
                # Reported in the email but does NOT set scraper_alarm: StoryGraph's
                # browse coverage is genuinely patchy, so a red run every time it is
                # thin would train the failure signal to be ignored. The banner is
                # the alert; the other sources carry on.
                down_sources.append("The StoryGraph")

        gb_candidates: list[Book] = []
        if google_books:
            logger.info("Fetching Google Books science-fiction releases (trailing %d days)...", window_days)
            gb_candidates = fetch_google_books_new_releases(window_days)

        # --- Phase 2: Dedup ---
        unseen = [b for b in candidates if not is_seen(conn, b.isbn13, b.title, b.author)]
        logger.info("%d unseen Goodreads books after dedup", len(unseen))

        # StoryGraph dedup (union-aware). Drop a candidate only if it repeats
        # within the SG feed, or its title was already SHOWN / is genre-excluded
        # (_sg_name_suppressed). A Goodreads twin that merely failed its bar does
        # NOT suppress it here — the "prefer Goodreads when BOTH pass" case is
        # reconciled after both filters run (Phase 4e), so we don't need this
        # run's Goodreads pass/fail outcome (unknown until Phase 4) yet.
        sg_seen_names: set[tuple[str, str]] = set()
        sg_unseen: list[Book] = []
        for b in sg_candidates:
            name = _norm_name(b)
            if name in sg_seen_names:
                continue
            if _name_suppressed(conn, b):
                continue
            sg_seen_names.add(name)
            sg_unseen.append(b)
        if sg_candidates:
            logger.info("%d unseen StoryGraph books after dedup", len(sg_unseen))

        # Google Books dedup, same union-aware rules as StoryGraph: its volume
        # ISBNs are edition-specific, so is_seen() alone would re-surface a title
        # already shown via Goodreads under a different ISBN.
        gb_seen_names: set[tuple[str, str]] = set()
        gb_unseen: list[Book] = []
        for b in gb_candidates:
            name = _norm_name(b)
            if name in gb_seen_names:
                continue
            if is_seen(conn, b.isbn13, b.title, b.author) or _name_suppressed(conn, b):
                continue
            gb_seen_names.add(name)
            gb_unseen.append(b)
        if gb_candidates:
            logger.info("%d unseen Google Books volumes after dedup", len(gb_unseen))

        # --- Phase 3: Enrich ---
        logger.info("Enriching %d books with Goodreads details...", len(unseen))
        enriched = []
        for book in unseen:
            try:
                enriched.append(enrich_book(book))
            except Exception as e:
                logger.error("Enrichment crashed for %r (%s): %s", book.title, book.goodreads_url, e)
                continue

        # --- Phase 4: Filter ---
        for book in enriched:
            hit = passes_filter(book, min_rating, min_rating_count)
            log_book(
                conn,
                title=book.title,
                author=book.author,
                isbn13=book.isbn13,
                goodreads_id=book.goodreads_id,
                pub_date=book.pub_date,
                rating=book.rating,
                rating_count=book.rating_count,
                passed_filter=hit,
                genre_tags=", ".join(book.genre_tags) if book.genre_tags else None,
                goodreads_url=book.goodreads_url,
                description=book.description,
            )
            if hit:
                passed.append(book)

        logger.info("%d books passed the filter (rating >= %.1f, count >= %d)",
                    len(passed), min_rating, min_rating_count)

        # --- Phase 4a: Enrich passing books missing genres/pub_date ---
        # Apollo JSON from listing pages provides rating/author but not genres
        # or pub_date. Fetch detail pages only for books that passed the filter.
        needs_detail = [b for b in passed if not b.genre_tags or not b.pub_date]
        if needs_detail:
            logger.info("Fetching detail pages for %d passing books (genres/pub date)...", len(needs_detail))
            for book in needs_detail:
                try:
                    enrich_book(book, force=True)
                except Exception as e:
                    logger.error("Detail enrichment crashed for %r: %s", book.title, e)
                    continue
                # Write genres/pub_date/description back to DB — enrichment updates
                # the in-memory Book object but the DB row was logged before this pass.
                log_book(
                    conn,
                    title=book.title,
                    author=book.author,
                    isbn13=book.isbn13,
                    pub_date=book.pub_date,
                    rating=book.rating,
                    rating_count=book.rating_count,
                    passed_filter=True,
                    genre_tags=", ".join(book.genre_tags) if book.genre_tags else None,
                    goodreads_url=book.goodreads_url,
                    description=book.description,
                )

        # --- Phase 4b: Re-check previously failed books (quarterly) ---
        if recheck:
            recheck_candidates = get_books_for_recheck(conn)
            logger.info("Re-checking %d previously failed books...", len(recheck_candidates))
            for row in recheck_candidates:
                if row["goodreads_url"]:
                    b = Book(
                        title=row["title"],
                        author=row["author"],
                        goodreads_url=row["goodreads_url"],
                        isbn13=row["isbn13"],
                    )
                    b = enrich_book(b)
                    hit = passes_filter(b, min_rating, min_rating_count)
                    log_book(
                        conn,
                        title=b.title,
                        author=b.author,
                        isbn13=b.isbn13,
                        goodreads_id=b.goodreads_id,
                        pub_date=b.pub_date,
                        rating=b.rating,
                        rating_count=b.rating_count,
                        passed_filter=hit,
                        genre_tags=", ".join(b.genre_tags) if b.genre_tags else None,
                        goodreads_url=b.goodreads_url,
                    )
                    if hit:
                        passed.append(b)

        # --- Phase 4d: StoryGraph enrich + filter ---
        # Rating/count are not on the listing; fetch each un-seen book's
        # community_reviews fragment, then apply StoryGraph's own thresholds.
        # A book is logged (and thus permanently deduped) ONLY once enrichment
        # yields a rating. If the fetch failed (transient Cloudflare block) or the
        # book has no ratings yet, we leave it un-seen so it is retried next run —
        # StoryGraph has no --recheck path, so logging a transient failure would
        # discard a qualifying release forever.
        if sg_unseen:
            logger.info("Enriching %d StoryGraph books with community ratings...", len(sg_unseen))
            for book in sg_unseen:
                try:
                    enrich_storygraph_book(book)
                except Exception as e:
                    logger.error("StoryGraph enrichment crashed for %r: %s — retrying next run", book.title, e)
                    continue
                if book.rating is None:
                    logger.info("No rating yet for %r (fetch failed or too new) — leaving un-seen for retry", book.title)
                    continue
                hit = passes_storygraph_filter(book, sg_min_rating, sg_min_count)
                log_book(
                    conn,
                    title=book.title,
                    author=book.author,
                    isbn13=book.isbn13,
                    pub_date=book.pub_date,
                    rating=book.rating,
                    rating_count=book.rating_count,
                    passed_filter=hit,
                    genre_tags=", ".join(book.genre_tags) if book.genre_tags else None,
                    source="storygraph",
                    storygraph_url=book.storygraph_url,
                )
                if hit:
                    sg_passed.append(book)
            logger.info("%d StoryGraph books passed (rating > %.2f, count > %d)",
                        len(sg_passed), sg_min_rating, sg_min_count)

        # --- Phase 4f: Google Books filter ---
        # No enrichment pass: the volumes API already returns rating, count,
        # categories, and description in the listing response.
        for book in gb_unseen:
            hit = passes_google_books_filter(book, gb_min_rating, gb_min_count)
            log_book(
                conn,
                title=book.title,
                author=book.author,
                isbn13=book.isbn13,
                pub_date=book.pub_date,
                rating=book.rating,
                rating_count=book.rating_count,
                passed_filter=hit,
                genre_tags=", ".join(book.genre_tags) if book.genre_tags else None,
                description=book.description,
                source="google_books",
                google_books_url=book.google_books_url,
            )
            if hit:
                gb_passed.append(book)
        if gb_unseen:
            logger.info("%d Google Books volumes passed (rating >= %.2f, count >= %d)",
                        len(gb_passed), gb_min_rating, gb_min_count)

        # --- Phase 4c: Genre exclusion ---
        # The rating filter is genre-blind. Drop books tagged with an excluded
        # genre (e.g. Romance) so they leave the shortlist, catalog, and digest.
        # Books with no detected genres are kept (broad rating-based feed).
        # Excluded books are re-logged as passed_filter=False: they stay "seen"
        # (so they won't reappear weekly) but drop out of every passed_filter=1
        # query (catalog webpage + monthly digest). Applied to both sources.
        passed, excluded_count = _apply_genre_exclusion(conn, passed)
        sg_passed, sg_excluded_count = _apply_genre_exclusion(conn, sg_passed)
        gb_passed, gb_excluded_count = _apply_genre_exclusion(conn, gb_passed)
        if excluded_count or sg_excluded_count or gb_excluded_count:
            logger.info(
                "Genre exclusion removed %d Goodreads + %d StoryGraph + %d Google Books book(s)",
                excluded_count, sg_excluded_count, gb_excluded_count,
            )

        # --- Phase 4e: Prefer Goodreads when the SAME book passes SEVERAL sources ---
        # Now that every filter has run, drop any secondary-source pick whose title
        # is also shown via a higher-priority source this week, so a book qualifying
        # under two sources is listed once. Priority: Goodreads > StoryGraph >
        # Google Books. (The duplicate rows were already logged as passed in phases
        # 4d/4f; write_catalog collapses them to the preferred entry.)
        def _drop_already_shown(books: list[Book], shown: set[tuple[str, str]],
                                label: str) -> list[Book]:
            kept = [b for b in books if _norm_name(b) not in shown]
            if len(kept) < len(books):
                logger.info("Reconciled %d %s pick(s) already shown via a preferred source",
                            len(books) - len(kept), label)
            shown.update(_norm_name(b) for b in kept)
            return kept

        shown: set[tuple[str, str]] = {_norm_name(b) for b in passed}
        sg_passed = _drop_already_shown(sg_passed, shown, "StoryGraph")
        gb_passed = _drop_already_shown(gb_passed, shown, "Google Books")

        # --- Phase 6: Award winners (independent of the release pipeline) ---
        # Wrapped whole: a broken award source must never cost the user the
        # release feed, so this can log and move on but never propagate.
        if not skip_awards:
            try:
                award_winners, award_notes = collect_award_winners(conn)
            except Exception as e:
                logger.exception("Award scan failed — continuing without it: %s", e)
                award_notes = [f"award scan crashed: {e}"]

        # --- Phase 5: Output ---
        shortlist_path = write_shortlist(
            passed, today, storygraph_books=sg_passed, google_books=gb_passed,
            award_winners=award_winners, award_notes=award_notes,
            down_sources=down_sources,
        )
        logger.info("Shortlist: %s", shortlist_path)

        catalog_books = get_all_catalog_books(conn)
        write_catalog(catalog_books, Path(__file__).parent / "docs",
                      min_rating=min_rating, min_rating_count=min_rating_count)
        logger.info("Catalog updated (%d all-time books)", len(catalog_books))

        # An outage is itself worth an email even when nothing passed: otherwise a
        # week where Goodreads is dead and the other sources happen to be empty
        # sends nothing at all, which is indistinguishable from a quiet week and
        # is precisely the case the warning exists to surface.
        have_content = bool(passed or sg_passed or gb_passed or award_winners)
        if (have_content or down_sources) and not skip_email:
            if not send_email(passed, recipient, today, min_rating=min_rating,
                              min_rating_count=min_rating_count,
                              storygraph_books=sg_passed, google_books=gb_passed,
                              award_winners=award_winners, award_notes=award_notes,
                              down_sources=down_sources):
                logger.error("Email delivery failed — failing the run.")
                sys.exit(1)
        elif not have_content:
            logger.info("Nothing to report this week — no email sent.")

    finally:
        conn.close()

    logger.info("Done. %d Goodreads + %d StoryGraph + %d Google Books "
                "+ %d award winners recommended.",
                len(passed), len(sg_passed), len(gb_passed), len(award_winners))

    # Exit non-zero only after the shortlist, catalog, and email are all done, so
    # a broken Goodreads scraper still delivers whatever the other sources found
    # instead of costing a whole week's feed.
    if scraper_alarm:
        logger.error("Failing the run so the scraper breakage is not silent.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="New Release Book Filter")
    parser.add_argument("--window", type=int, default=90,
                        help="Trailing days to consider as 'new' (default: 90)")
    parser.add_argument("--min-rating", type=float, default=GOODREADS_MIN_RATING,
                        help=f"Minimum Goodreads rating (default: {GOODREADS_MIN_RATING})")
    parser.add_argument("--min-count", type=int, default=GOODREADS_MIN_COUNT,
                        help=f"Minimum number of ratings (default: {GOODREADS_MIN_COUNT})")
    parser.add_argument("--recipient", default=None,
                        help="Email recipient (default: BOOK_RECIPIENT env var)")
    parser.add_argument("--recheck", action="store_true",
                        help="Re-check previously failed books")
    parser.add_argument("--skip-email", action="store_true",
                        help="Skip email, only write markdown")
    parser.add_argument("--backfill-genres", action="store_true",
                        help="Re-enrich all passed books missing genres and exit")
    parser.add_argument("--skip-storygraph", action="store_true",
                        help="Skip the StoryGraph source (Goodreads only)")
    parser.add_argument("--sg-min-rating", type=float, default=STORYGRAPH_MIN_RATING,
                        help=f"StoryGraph min rating, exclusive (default: {STORYGRAPH_MIN_RATING})")
    parser.add_argument("--sg-min-count", type=int, default=STORYGRAPH_MIN_COUNT,
                        help=f"StoryGraph min ratings count, exclusive (default: {STORYGRAPH_MIN_COUNT})")
    parser.add_argument("--google-books", action="store_true",
                        help="Enable the Google Books science-fiction source (off by default; "
                             "its rating data is sparse and noisy)")
    parser.add_argument("--gb-min-rating", type=float, default=GOOGLE_BOOKS_MIN_RATING,
                        help=f"Google Books min rating (default: {GOOGLE_BOOKS_MIN_RATING})")
    parser.add_argument("--gb-min-count", type=int, default=GOOGLE_BOOKS_MIN_COUNT,
                        help=f"Google Books min ratings count (default: {GOOGLE_BOOKS_MIN_COUNT})")
    parser.add_argument("--skip-awards", action="store_true",
                        help="Skip the weekly book-award winner scan")
    parser.add_argument("--db-path", default=None,
                        help="Use an alternate SQLite file (for testing against a "
                             "copy instead of the tracked seen_books.db)")
    args = parser.parse_args()

    run(
        window_days=args.window,
        min_rating=args.min_rating,
        min_rating_count=args.min_count,
        recipient=args.recipient,
        recheck=args.recheck,
        skip_email=args.skip_email,
        backfill=args.backfill_genres,
        skip_storygraph=args.skip_storygraph,
        sg_min_rating=args.sg_min_rating,
        sg_min_count=args.sg_min_count,
        google_books=args.google_books,
        gb_min_rating=args.gb_min_rating,
        gb_min_count=args.gb_min_count,
        skip_awards=args.skip_awards,
        db_path=Path(args.db_path) if args.db_path else None,
    )


if __name__ == "__main__":
    main()
