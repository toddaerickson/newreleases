"""Markdown output and email notification."""

import logging
import smtplib
import os
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from scraper import Book

logger = logging.getLogger(__name__)

SHORTLISTS_DIR = Path(__file__).parent / "shortlists"


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
            genres = ", ".join(book.genre_tags) if book.genre_tags else "—"
            rating_str = f"{book.rating:.2f}" if book.rating else "N/A"
            count_str = f"{book.rating_count:,}" if book.rating_count else "N/A"
            pub_str = book.pub_date or "Unknown"

            lines.append(f"### {i}. {book.title} — {book.author}\n")
            lines.append(f"- **Rating:** {rating_str} ({count_str} ratings)")
            lines.append(f"- **Published:** {pub_str}")
            lines.append(f"- **Genres:** {genres}")
            if book.goodreads_url:
                lines.append(f"- **Goodreads:** {book.goodreads_url}")
            lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote shortlist to %s", filepath)
    return filepath


def send_email(books: list[Book], recipient: str, run_date: date | None = None) -> bool:
    """Send the shortlist as an email. Returns True on success."""
    run_date = run_date or date.today()

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not all([smtp_host, smtp_user, smtp_pass]):
        logger.warning("SMTP not configured — skipping email. Set SMTP_HOST, SMTP_USER, SMTP_PASS.")
        return False

    subject = f"[Books] {len(books)} new candidate{'s' if len(books) != 1 else ''} over 4.3 — {run_date.isoformat()}"

    # Build body from the same data
    body_lines = [f"# New book shortlist — {run_date.isoformat()}\n"]
    if not books:
        body_lines.append("No new books passed the filter this week.")
    else:
        for i, book in enumerate(books, 1):
            genres = ", ".join(book.genre_tags) if book.genre_tags else "—"
            rating_str = f"{book.rating:.2f}" if book.rating else "N/A"
            count_str = f"{book.rating_count:,}" if book.rating_count else "N/A"
            body_lines.append(f"{i}. {book.title} — {book.author}")
            body_lines.append(f"   Rating: {rating_str} ({count_str} ratings)")
            body_lines.append(f"   Published: {book.pub_date or 'Unknown'}")
            body_lines.append(f"   Genres: {genres}")
            if book.goodreads_url:
                body_lines.append(f"   Goodreads: {book.goodreads_url}")
            body_lines.append("")

    body = "\n".join(body_lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        logger.info("Email sent to %s", recipient)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False
