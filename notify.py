"""Markdown output and email notification."""

import logging
import os
import smtplib
import ssl
from datetime import date
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from scraper import Book

logger = logging.getLogger(__name__)

SHORTLISTS_DIR = Path(__file__).parent / "shortlists"


def _format_book_entry(i: int, book: Book, markdown: bool = True) -> str:
    """Format a single book entry for markdown or plaintext."""
    genres = ", ".join(book.genre_tags) if book.genre_tags else "—"
    rating_str = f"{book.rating:.2f}" if book.rating is not None else "N/A"
    count_str = f"{book.rating_count:,}" if book.rating_count is not None else "N/A"
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


def send_email(
    books: list[Book],
    recipient: str,
    run_date: date | None = None,
    min_rating: float = 4.3,
) -> bool:
    """Send the shortlist as an email. Returns True on success.

    Uses Gmail SMTP with TLS (port 587). Requires a Gmail App Password
    (not a regular password) — 2FA must be enabled on the Google account.
    """
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

    if smtp_port == 465:
        logger.warning("SMTP_PORT 465 requires SMTP_SSL, but this code uses STARTTLS. Use port 587.")
        return False

    # Guard against header injection in recipient
    if "\r" in recipient or "\n" in recipient:
        logger.error("Recipient contains newline characters — possible header injection")
        return False

    # Warn about SPF/DKIM for non-Gmail domains
    if not smtp_user.endswith("@gmail.com"):
        logger.info("Sending from non-Gmail address %s — ensure SPF/DKIM are configured on the domain", smtp_user)

    count = len(books)
    subject = (
        f"[Books] {count} new candidate{'s' if count != 1 else ''} "
        f"over {min_rating} — {run_date.isoformat()}"
    )

    body_lines = [f"# New book shortlist — {run_date.isoformat()}\n"]
    if not books:
        body_lines.append("No new books passed the filter this week.")
    else:
        for i, book in enumerate(books, 1):
            body_lines.append(_format_book_entry(i, book, markdown=False))

    body = "\n".join(body_lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_host)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        logger.info("Email sent to %s", recipient)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check SMTP_USER and SMTP_PASS (must be a Gmail App Password).")
        return False
    except smtplib.SMTPException:
        logger.exception("SMTP error sending email to %s", recipient)
        return False
    except OSError:
        logger.exception("Network error sending email to %s", recipient)
        return False
