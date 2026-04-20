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

from db import get_conn, is_seen, log_book, get_books_for_recheck
from scraper import Book, fetch_new_releases, enrich_book
from filters import passes_filter
from notify import write_shortlist, send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run(
    window_days: int = 90,
    min_rating: float = 4.3,
    min_rating_count: int = 500,
    recipient: str | None = None,
    recheck: bool = False,
    skip_email: bool = False,
) -> None:
    recipient = recipient or os.environ.get("BOOK_RECIPIENT")
    if not recipient:
        logger.error("No recipient configured. Set BOOK_RECIPIENT env var or use --recipient.")
        sys.exit(1)

    conn = get_conn()
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
        passed = []
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
            )
            if hit:
                passed.append(book)

        logger.info("%d books passed the filter (rating >= %.1f, count >= %d)",
                    len(passed), min_rating, min_rating_count)

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

        if passed and not skip_email:
            if not send_email(passed, recipient, today):
                logger.warning("Email delivery failed for %d books — check SMTP config.", len(passed))
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
    args = parser.parse_args()

    run(
        window_days=args.window,
        min_rating=args.min_rating,
        min_rating_count=args.min_count,
        recipient=args.recipient,
        recheck=args.recheck,
        skip_email=args.skip_email,
    )


if __name__ == "__main__":
    main()
