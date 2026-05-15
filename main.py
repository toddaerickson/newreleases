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

from db import get_conn, is_seen, log_book, get_books_for_recheck, get_all_catalog_books, get_books_missing_genres
from scraper import Book, fetch_new_releases, enrich_book
from filters import passes_filter
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


def run(
    window_days: int = 90,
    min_rating: float = 4.3,
    min_rating_count: int = 500,
    recipient: str | None = None,
    recheck: bool = False,
    skip_email: bool = False,
    backfill: bool = False,
) -> None:
    conn = get_conn()
    passed: list[Book] = []  # Initialize before try to avoid UnboundLocalError

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
        logger.info("Found %d candidates", len(candidates))

        # --- Phase 2: Dedup ---
        unseen = [b for b in candidates if not is_seen(conn, b.isbn13, b.title, b.author)]
        logger.info("%d unseen books after dedup", len(unseen))

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

        # --- Phase 5: Output ---
        shortlist_path = write_shortlist(passed, today)
        logger.info("Shortlist: %s", shortlist_path)

        catalog_books = get_all_catalog_books(conn)
        write_catalog(catalog_books, Path(__file__).parent / "docs")
        logger.info("Catalog updated (%d all-time books)", len(catalog_books))

        if passed and not skip_email:
            if not send_email(passed, recipient, today, min_rating=min_rating):
                logger.error("Email delivery failed — failing the run.")
                sys.exit(1)
        elif not passed:
            logger.info("No books passed the filter — no email sent.")

    finally:
        conn.close()

    logger.info("Done. %d books recommended.", len(passed))


def main():
    parser = argparse.ArgumentParser(description="New Release Book Filter")
    parser.add_argument("--window", type=int, default=90,
                        help="Trailing days to consider as 'new' (default: 90)")
    parser.add_argument("--min-rating", type=float, default=4.3,
                        help="Minimum Goodreads rating (default: 4.3)")
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
    args = parser.parse_args()

    run(
        window_days=args.window,
        min_rating=args.min_rating,
        min_rating_count=args.min_count,
        recipient=args.recipient,
        recheck=args.recheck,
        skip_email=args.skip_email,
        backfill=args.backfill_genres,
    )


if __name__ == "__main__":
    main()
