#!/usr/bin/env python3
"""
New Release Book Filter — Orchestrator

Scrapes Goodreads new releases, filters by rating >= 4.3 with >= 500 ratings,
deduplicates against a persistent SQLite log, and outputs a markdown shortlist
plus optional email notification.
"""

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from db import (
    get_conn, is_seen, get_seen_rows_by_name, log_book, get_books_for_recheck,
    get_all_catalog_books, get_books_missing_genres,
)
from scraper import Book, fetch_new_releases, enrich_book
from storygraph import fetch_storygraph_new_releases, enrich_storygraph_book
from filters import (
    passes_filter, passes_storygraph_filter, is_excluded_by_genre, genres_excluded,
    STORYGRAPH_MIN_RATING, STORYGRAPH_MIN_COUNT,
)
from notify import write_shortlist, send_email, write_catalog

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


def _sg_name_suppressed(conn, book: Book) -> bool:
    """Union-aware cross-run dedup for a StoryGraph candidate.

    Suppress ONLY if this title was already SHOWN in a feed (a passed_filter=1
    row exists, either source) or is a genre-excluded title (romance, etc.).
    A Goodreads twin that merely FAILED its rating bar does NOT suppress the
    StoryGraph pick — that is the whole point of the OR union.
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
            )
        else:
            kept.append(book)
    return kept, excluded


def run(
    window_days: int = 90,
    min_rating: float = 4.1,
    min_rating_count: int = 500,
    recipient: str | None = None,
    recheck: bool = False,
    skip_email: bool = False,
    backfill: bool = False,
    skip_storygraph: bool = False,
    sg_min_rating: float = STORYGRAPH_MIN_RATING,
    sg_min_count: int = STORYGRAPH_MIN_COUNT,
) -> None:
    conn = get_conn()
    passed: list[Book] = []  # Initialize before try to avoid UnboundLocalError
    sg_passed: list[Book] = []

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

        sg_candidates: list[Book] = []
        if not skip_storygraph:
            logger.info("Fetching StoryGraph new releases (trailing %d days)...", window_days)
            sg_candidates = fetch_storygraph_new_releases(window_days)
            logger.info("Found %d StoryGraph candidates", len(sg_candidates))

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
            if _sg_name_suppressed(conn, b):
                continue
            sg_seen_names.add(name)
            sg_unseen.append(b)
        if sg_candidates:
            logger.info("%d unseen StoryGraph books after dedup", len(sg_unseen))

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

        # --- Phase 4c: Genre exclusion ---
        # The rating filter is genre-blind. Drop books tagged with an excluded
        # genre (e.g. Romance) so they leave the shortlist, catalog, and digest.
        # Books with no detected genres are kept (broad rating-based feed).
        # Excluded books are re-logged as passed_filter=False: they stay "seen"
        # (so they won't reappear weekly) but drop out of every passed_filter=1
        # query (catalog webpage + monthly digest). Applied to both sources.
        passed, excluded_count = _apply_genre_exclusion(conn, passed)
        sg_passed, sg_excluded_count = _apply_genre_exclusion(conn, sg_passed)
        if excluded_count or sg_excluded_count:
            logger.info("Genre exclusion removed %d Goodreads + %d StoryGraph book(s)",
                        excluded_count, sg_excluded_count)

        # --- Phase 4e: Prefer Goodreads when the SAME book passes BOTH this run ---
        # Now that both filters have run, drop any StoryGraph pick whose title is
        # also shown via Goodreads this week, so a book qualifying under both is
        # listed once (as Goodreads). (Its StoryGraph row was logged passed in
        # Phase 4d; the catalog collapses the duplicate to the Goodreads entry.)
        if sg_passed and passed:
            gr_shown = {_norm_name(b) for b in passed}
            deduped = [b for b in sg_passed if _norm_name(b) not in gr_shown]
            if len(deduped) < len(sg_passed):
                logger.info("Reconciled %d StoryGraph pick(s) also shown via Goodreads",
                            len(sg_passed) - len(deduped))
            sg_passed = deduped

        # --- Phase 5: Output ---
        shortlist_path = write_shortlist(passed, today, storygraph_books=sg_passed)
        logger.info("Shortlist: %s", shortlist_path)

        catalog_books = get_all_catalog_books(conn)
        write_catalog(catalog_books, Path(__file__).parent / "docs")
        logger.info("Catalog updated (%d all-time books)", len(catalog_books))

        if (passed or sg_passed) and not skip_email:
            if not send_email(passed, recipient, today, min_rating=min_rating,
                              storygraph_books=sg_passed):
                logger.error("Email delivery failed — failing the run.")
                sys.exit(1)
        elif not passed and not sg_passed:
            logger.info("No books passed the filter — no email sent.")

    finally:
        conn.close()

    logger.info("Done. %d Goodreads + %d StoryGraph books recommended.",
                len(passed), len(sg_passed))


def main():
    parser = argparse.ArgumentParser(description="New Release Book Filter")
    parser.add_argument("--window", type=int, default=90,
                        help="Trailing days to consider as 'new' (default: 90)")
    parser.add_argument("--min-rating", type=float, default=4.1,
                        help="Minimum Goodreads rating (default: 4.1)")
    parser.add_argument("--min-count", type=int, default=500,
                        help="Minimum number of ratings (default: 500)")
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
    )


if __name__ == "__main__":
    main()
