"""Markdown output and email notification."""

import logging
import os
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from scraper import Book

logger = logging.getLogger(__name__)

SHORTLISTS_DIR = Path(__file__).parent / "shortlists"


def _format_book_entry(i: int, book: Book, markdown: bool = True) -> str:
    """Format a single book entry for markdown or plaintext."""
    genres = ", ".join(book.genre_tags) if book.genre_tags else "—"
    rating_str = f"{book.rating:.2f}" if book.rating else "N/A"
    count_str = f"{book.rating_count:,}" if book.rating_count else "N/A"
    pub_str = book.pub_date or "Unknown"

    if markdown:
        lines = [
            f"### {i}. {book.title} — {book.author}\n",
            f"- **Rating:** {rating_str} ({count_str} ratings)",
            f"- **Published:** {pub_str}",
            f"- **Genres:** {genres}",
        ]
        if book.goodreads_url:
            lines.append(f"- **Goodreads:** {book.goodreads_url}")
        lines.append("")
    else:
        lines = [
            f"{i}. {book.title} — {book.author}",
            f"   Rating: {rating_str} ({count_str} ratings)",
            f"   Published: {pub_str}",
            f"   Genres: {genres}",
        ]
        if book.goodreads_url:
            lines.append(f"   Goodreads: {book.goodreads_url}")
        lines.append("")

    return "\n".join(lines)


def write_shortlist(books: list[Book], run_date: date | None = None) -> Path:
    """Write a markdown shortlist file. Returns the file path."""
    run_date = run_date or date.today()
    SHORTLISTS_DIR.mkdir(exist_ok=True)
    filepath = SHORTLISTS_DIR / f"shortlist_{run_date.isoformat()}.md"

    lines = [f"# New book shortlist — {run_date.isoformat()}\n"]

    if not books:
        lines.append("No new books passed the filter this week.\n")
    else:
        lines.append(f"## New this week ({len(books)})\n")
        for i, book in enumerate(books, 1):
            lines.append(_format_book_entry(i, book, markdown=True))

    filepath.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote shortlist to %s", filepath)
    return filepath


def send_email(books: list[Book], recipient: str, run_date: date | None = None) -> bool:
    """Send the shortlist as an email. Returns True on success."""
    run_date = run_date or date.today()

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not all([smtp_host, smtp_user, smtp_pass]):
        logger.warning("SMTP not configured — skipping email. Set SMTP_HOST, SMTP_USER, SMTP_PASS.")
        return False

    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        logger.error("SMTP_PORT must be an integer; got %r", os.environ.get("SMTP_PORT"))
        return False

    count = len(books)
    subject = f"[Books] {count} new candidate{'s' if count != 1 else ''} over 4.3 — {run_date.isoformat()}"

    body_lines = [f"# New book shortlist — {run_date.isoformat()}\n"]
    if not books:
        body_lines.append("No new books passed the filter this week.")
    else:
        for i, book in enumerate(books, 1):
            body_lines.append(_format_book_entry(i, book, markdown=False))

    body = "\n".join(body_lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ctx)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        logger.info("Email sent to %s", recipient)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check SMTP_USER and SMTP_PASS.")
        return False
    except smtplib.SMTPException:
        logger.exception("SMTP error sending email to %s", recipient)
        return False
    except OSError:
        logger.exception("Network error sending email to %s", recipient)
        return False
