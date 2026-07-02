#!/usr/bin/env python3
"""Monthly digest — query the DB for recent passing books and email a summary."""

import argparse
import logging
import os
import sys
from datetime import date

from db import get_conn, get_digest_books
from scraper import Book
from notify import send_digest_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _dict_to_book(row: dict) -> Book:
    """Convert a DB row dict to a Book dataclass for formatting."""
    genre_str = row.get("genre_tags") or ""
    genres = [g.strip() for g in genre_str.split(",") if g.strip()]
    return Book(
        title=row["title"],
        author=row["author"],
        isbn13=row.get("isbn13"),
        goodreads_id=row.get("goodreads_id"),
        goodreads_url=row.get("goodreads_url"),
        pub_date=row.get("pub_date"),
        rating=row.get("rating"),
        rating_count=row.get("rating_count"),
        genre_tags=genres,
        source=row.get("source") or "goodreads",
        storygraph_url=row.get("storygraph_url"),
    )


def run(days: int = 30, recipient: str | None = None, skip_email: bool = False) -> None:
    recipient = (recipient or os.environ.get("BOOK_RECIPIENT", "")).strip("'\" ")
    if not recipient:
        logger.error("No recipient configured. Set BOOK_RECIPIENT env var or use --recipient.")
        sys.exit(1)

    conn = get_conn()
    try:
        rows = get_digest_books(conn, days=days)
        logger.info("Found %d books passing filter in the last %d days", len(rows), days)

        books = [_dict_to_book(r) for r in rows]

        if not books:
            logger.info("No books to digest — no email sent.")
            return

        if skip_email:
            logger.info("--skip-email set; printing %d books to stdout.", len(books))
            for i, book in enumerate(books, 1):
                print(f"{i}. {book.title} — {book.author} ({book.rating}, {book.rating_count} ratings)")
            return

        today = date.today()
        month_label = today.strftime("%B %Y")

        if send_digest_email(books, recipient, month_label):
            logger.info("Digest email sent successfully (%d books).", len(books))
        else:
            logger.warning("Digest email delivery failed — check SMTP config.")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Monthly Book Digest")
    parser.add_argument("--days", type=int, default=30,
                        help="Lookback window in days (default: 30)")
    parser.add_argument("--recipient", default=None,
                        help="Email recipient (default: BOOK_RECIPIENT env var)")
    parser.add_argument("--skip-email", action="store_true",
                        help="Print digest to stdout instead of emailing")
    args = parser.parse_args()

    run(days=args.days, recipient=args.recipient, skip_email=args.skip_email)


if __name__ == "__main__":
    main()
