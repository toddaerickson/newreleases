"""Markdown output and email notification."""

import html
import json
import logging
import os
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from scraper import Book

CATALOG_URL = "https://toddaerickson.github.io/newreleases/"

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


def write_catalog(books: list[dict], docs_dir: Path) -> None:
    """Write docs/books.json and docs/index.html from all-time passed books."""
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Compute delta and build serialisable records
    records = []
    for b in books:
        first = b.get("first_rating")
        last = b.get("last_rating")
        delta = round(last - first, 3) if (first is not None and last is not None) else None
        records.append({
            "title": b["title"],
            "author": b["author"],
            "genre": b.get("genre_tags") or "",
            "first_seen": b.get("first_seen_date") or "",
            "rating_first": first,
            "rating_count_first": b.get("first_rating_count"),
            "rating_last": last,
            "rating_count_last": b.get("last_rating_count"),
            "delta": delta,
            "goodreads_url": b.get("goodreads_url") or "",
        })

    (docs_dir / "books.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8"
    )

    rows_html = []
    for r in records:
        title_cell = (
            f'<a href="{html.escape(r["goodreads_url"])}">{html.escape(r["title"])}</a>'
            if r["goodreads_url"]
            else html.escape(r["title"])
        )
        first_str = (
            f"{r['rating_first']:.2f} ({r['rating_count_first']:,})"
            if r["rating_first"] is not None else "—"
        )
        last_str = (
            f"{r['rating_last']:.2f} ({r['rating_count_last']:,})"
            if r["rating_last"] is not None else "—"
        )
        if r["delta"] is None:
            delta_cell = "<td>—</td>"
        elif r["delta"] > 0:
            delta_cell = f'<td class="up">+{r["delta"]:.3f}</td>'
        elif r["delta"] < 0:
            delta_cell = f'<td class="down">{r["delta"]:.3f}</td>'
        else:
            delta_cell = "<td>0.000</td>"

        rows_html.append(
            f"<tr>"
            f"<td>{title_cell}</td>"
            f"<td>{html.escape(r['author'])}</td>"
            f"<td>{html.escape(r['genre'])}</td>"
            f"<td>{html.escape(r['first_seen'])}</td>"
            f"<td>{first_str}</td>"
            f"<td>{last_str}</td>"
            f"{delta_cell}"
            f"</tr>"
        )

    updated = date.today().isoformat()
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Top Book Releases</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  p.meta {{ font-size: 0.85rem; color: #666; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
  th {{ background: #f0f0f0; cursor: pointer; user-select: none; white-space: nowrap; }}
  th:hover {{ background: #e0e0e0; }}
  th, td {{ padding: 0.4rem 0.6rem; border: 1px solid #ddd; text-align: left; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  td.up {{ color: #1a7f37; font-weight: 600; }}
  td.down {{ color: #cf222e; font-weight: 600; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Top Book Releases</h1>
<p class="meta">Books rated ≥4.3 with ≥500 ratings. Updated weekly. Last updated: {updated}. &nbsp;
<a href="books.json">books.json</a></p>
<table id="t">
<thead><tr>
  <th onclick="sort(0)">Title ▲▼</th>
  <th onclick="sort(1)">Author ▲▼</th>
  <th onclick="sort(2)">Genre ▲▼</th>
  <th onclick="sort(3)">First Seen ▲▼</th>
  <th onclick="sort(4)">Rating (first) ▲▼</th>
  <th onclick="sort(5)">Rating (now) ▲▼</th>
  <th onclick="sort(6)">Δ ▲▼</th>
</tr></thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
<script>
let dir = {{}};
function sort(col) {{
  const tb = document.querySelector('#t tbody');
  const rows = Array.from(tb.rows);
  dir[col] = !dir[col];
  rows.sort((a, b) => {{
    const av = a.cells[col].textContent.trim();
    const bv = b.cells[col].textContent.trim();
    const an = parseFloat(av.replace(/[^0-9.+-]/g, ''));
    const bn = parseFloat(bv.replace(/[^0-9.+-]/g, ''));
    const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
    return dir[col] ? cmp : -cmp;
  }});
  rows.forEach(r => tb.appendChild(r));
}}
</script>
</body>
</html>"""

    (docs_dir / "index.html").write_text(page, encoding="utf-8")
    logger.info("Wrote catalog to %s ({} books)", docs_dir, len(records))


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

    # Plain-text body
    plain_lines = [
        f"New book shortlist — {run_date.isoformat()}",
        f"Full running list: {CATALOG_URL}",
        "",
    ]
    if not books:
        plain_lines.append("No new books passed the filter this week.")
    else:
        for i, book in enumerate(books, 1):
            plain_lines.append(_format_book_entry(i, book, markdown=False))
    plain_body = "\n".join(plain_lines)

    # HTML body
    html_rows = []
    for i, book in enumerate(books, 1):
        genres = html.escape(", ".join(book.genre_tags) if book.genre_tags else "—")
        rating_str = f"{book.rating:.2f}" if book.rating is not None else "N/A"
        count_str = f"{book.rating_count:,}" if book.rating_count is not None else "N/A"
        title_cell = (
            f'<a href="{html.escape(book.goodreads_url)}">{html.escape(book.title)}</a>'
            if book.goodreads_url else html.escape(book.title)
        )
        html_rows.append(
            f"<tr><td>{i}</td><td>{title_cell}</td>"
            f"<td>{html.escape(book.author)}</td>"
            f"<td>{genres}</td>"
            f"<td>{rating_str} ({count_str})</td>"
            f"<td>{html.escape(book.pub_date or 'Unknown')}</td></tr>"
        )

    if books:
        table_html = (
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:0.9em'>"
            "<thead><tr><th>#</th><th>Title</th><th>Author</th><th>Genre</th>"
            "<th>Rating</th><th>Published</th></tr></thead>"
            "<tbody>" + "".join(html_rows) + "</tbody></table>"
        )
    else:
        table_html = "<p>No new books passed the filter this week.</p>"

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;max-width:700px;margin:0 auto;padding:1rem">
<h2>New book shortlist — {run_date.isoformat()}</h2>
<p><a href="{CATALOG_URL}">View the full running list with rating history →</a></p>
{table_html}
<p style="font-size:0.8em;color:#666">Filtered: ≥{min_rating} rating, ≥500 ratings</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_host)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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


def send_digest_email(books: list[Book], recipient: str, month_label: str) -> bool:
    """Send a monthly digest email. Returns True on success.

    Args:
        books: Books that passed the filter in the digest period.
        recipient: Email address.
        month_label: Human-readable label, e.g. "April 2026".
    """
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

    if "\r" in recipient or "\n" in recipient:
        logger.error("Recipient contains newline characters — possible header injection")
        return False

    if not smtp_user.endswith("@gmail.com"):
        logger.info("Sending from non-Gmail address %s — ensure SPF/DKIM are configured on the domain", smtp_user)

    count = len(books)
    subject = f"[Books] Monthly digest: {count} book{'s' if count != 1 else ''} — {month_label}"

    body_lines = [f"# Monthly book digest — {month_label}\n"]
    if not books:
        body_lines.append("No books passed the filter this month.\n")
    else:
        body_lines.append(f"{count} book{'s' if count != 1 else ''} passed the filter:\n")
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
        logger.info("Digest email sent to %s", recipient)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check SMTP_USER and SMTP_PASS (must be a Gmail App Password).")
        return False
    except smtplib.SMTPException:
        logger.exception("SMTP error sending digest email to %s", recipient)
        return False
    except OSError:
        logger.exception("Network error sending digest email to %s", recipient)
        return False
