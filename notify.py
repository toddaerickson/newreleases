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

from scraper import Book, book_link

CATALOG_URL = "https://toddaerickson.github.io/newreleases/"

logger = logging.getLogger(__name__)

SHORTLISTS_DIR = Path(__file__).parent / "shortlists"


def _format_book_entry(i: int, book: Book, markdown: bool = True) -> str:
    """Format a single book entry for markdown or plaintext."""
    genres = ", ".join(book.genre_tags) if book.genre_tags else "—"
    rating_str = f"{book.rating:.2f}" if book.rating is not None else "N/A"
    count_str = f"{book.rating_count:,}" if book.rating_count is not None else "N/A"
    pub_str = book.pub_date or "Unknown"

    link = book_link(book)
    link_label = "StoryGraph" if book.source == "storygraph" else "Goodreads"

    if markdown:
        lines = [
            f"### {i}. {book.title} — {book.author}\n",
            f"- **Rating:** {rating_str} ({count_str} ratings)",
            f"- **Published:** {pub_str}",
            f"- **Genres:** {genres}",
        ]
        if link:
            lines.append(f"- **{link_label}:** {link}")
        lines.append("")
    else:
        lines = [
            f"{i}. {book.title} — {book.author}",
            f"   Rating: {rating_str} ({count_str} ratings)",
            f"   Published: {pub_str}",
            f"   Genres: {genres}",
        ]
        if link:
            lines.append(f"   {link_label}: {link}")
        lines.append("")

    return "\n".join(lines)


def write_shortlist(
    books: list[Book],
    run_date: date | None = None,
    storygraph_books: list[Book] | None = None,
) -> Path:
    """Write a markdown shortlist file. Returns the file path.

    Goodreads and StoryGraph picks are written as separate sections.
    """
    run_date = run_date or date.today()
    storygraph_books = storygraph_books or []
    SHORTLISTS_DIR.mkdir(exist_ok=True)
    filepath = SHORTLISTS_DIR / f"shortlist_{run_date.isoformat()}.md"

    lines = [f"# New book shortlist — {run_date.isoformat()}\n"]

    if not books and not storygraph_books:
        lines.append("No new books passed the filter this week.\n")
    else:
        lines.append(f"## From Goodreads ({len(books)})\n")
        if books:
            for i, book in enumerate(books, 1):
                lines.append(_format_book_entry(i, book, markdown=True))
        else:
            lines.append("No new Goodreads books passed the filter this week.\n")

        if storygraph_books:
            lines.append(f"## From StoryGraph ({len(storygraph_books)})\n")
            for i, book in enumerate(storygraph_books, 1):
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
        source = b.get("source") or "goodreads"
        link = b.get("storygraph_url") if source == "storygraph" else b.get("goodreads_url")
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
            "source": "StoryGraph" if source == "storygraph" else "Goodreads",
            "link": link or "",
            # Keep the legacy key for backward compatibility with any external
            # consumer of the published books.json (Goodreads rows only).
            "goodreads_url": b.get("goodreads_url") or "",
            "description": b.get("description") or "",
        })

    # Collapse cross-source duplicates: the same book can be logged under a
    # StoryGraph hash key and later a Goodreads ISBN key, producing two rows for
    # one title. Keep a single catalog entry per normalized title+author,
    # preferring the Goodreads row (richer data; "prefer Goodreads on overlap").
    by_name: dict[tuple[str, str], dict] = {}
    for r in records:
        k = (r["title"].strip().lower(), r["author"].strip().lower())
        existing = by_name.get(k)
        if existing is None or (existing["source"] == "StoryGraph" and r["source"] == "Goodreads"):
            by_name[k] = r
    records = list(by_name.values())

    (docs_dir / "books.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8"
    )

    rows_html = []
    for r in records:
        desc = r.get("description") or ""
        desc_snippet = (desc[:160] + "…") if len(desc) > 160 else desc
        title_link = (
            f'<a href="{html.escape(r["link"])}" title="{html.escape(desc_snippet)}">'
            f'{html.escape(r["title"])}</a>'
            if r["link"]
            else f'<span title="{html.escape(desc_snippet)}">{html.escape(r["title"])}</span>'
        )
        title_cell = (
            f'{title_link}<br><small style="color:#666;font-weight:normal">{html.escape(desc_snippet)}</small>'
            if desc_snippet else title_link
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
            delta_cell = '<td title="Rating has not been updated since first recorded">—</td>'
        elif r["delta"] > 0:
            delta_cell = f'<td class="up">+{r["delta"]:.3f}</td>'
        elif r["delta"] < 0:
            delta_cell = f'<td class="down">{r["delta"]:.3f}</td>'
        else:
            delta_cell = '<td title="No change since first recorded">—</td>'

        rows_html.append(
            f"<tr>"
            f"<td>{title_cell}</td>"
            f"<td>{html.escape(r['author'])}</td>"
            f"<td>{html.escape(r['genre'])}</td>"
            f"<td>{html.escape(r['first_seen'])}</td>"
            f"<td>{first_str}</td>"
            f"<td>{last_str}</td>"
            f"{delta_cell}"
            f"<td>{html.escape(r['source'])}</td>"
            f"</tr>"
        )

    # Collect genres for the filter dropdown — only those in 2+ books so the
    # list stays manageable; single-book tags are still stored and searchable.
    from collections import Counter
    genre_counts = Counter(
        g.strip()
        for r in records
        for g in r["genre"].split(",")
        if g.strip()
    )
    all_genres: list[str] = sorted(g for g, n in genre_counts.items() if n >= 2)
    genre_options = "\n".join(
        f'<option value="{html.escape(g)}">{html.escape(g)}</option>'
        for g in all_genres
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
  .toolbar {{ margin: 0.75rem 0; display: flex; align-items: center; gap: 0.5rem; }}
  .toolbar label {{ font-size: 0.88rem; color: #444; }}
  .toolbar select {{ font-size: 0.88rem; padding: 0.25rem 0.4rem; border: 1px solid #ccc; border-radius: 4px; }}
  .toolbar button {{ font-size: 0.82rem; padding: 0.2rem 0.5rem; border: 1px solid #ccc; border-radius: 4px; background: #f5f5f5; cursor: pointer; }}
  .toolbar button:hover {{ background: #e8e8e8; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
  th {{ background: #f0f0f0; cursor: pointer; user-select: none; white-space: nowrap; }}
  th:hover {{ background: #e0e0e0; }}
  th, td {{ padding: 0.4rem 0.6rem; border: 1px solid #ddd; text-align: left; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  tr.hidden {{ display: none; }}
  td.up {{ color: #1a7f37; font-weight: 600; }}
  td.down {{ color: #cf222e; font-weight: 600; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Top Book Releases</h1>
<p class="meta">Goodreads books rated ≥4.1 with ≥500 ratings, or StoryGraph books rated &gt;4.0 with &gt;70 ratings. Updated weekly. Last updated: {updated}.</p>
<div class="toolbar">
  <label for="gf">Filter by genre:</label>
  <select id="gf" onchange="filterGenre()">
    <option value="">All genres</option>
    {genre_options}
  </select>
  <button onclick="document.getElementById('gf').value='';filterGenre();">Clear</button>
  <span id="count" style="font-size:0.82rem;color:#666"></span>
</div>
<table id="t">
<thead><tr>
  <th onclick="sort(0)">Title ▲▼</th>
  <th onclick="sort(1)">Author ▲▼</th>
  <th onclick="sort(2)">Genre ▲▼</th>
  <th onclick="sort(3)">First Seen ▲▼</th>
  <th onclick="sort(4)">Rating (first) ▲▼</th>
  <th onclick="sort(5)">Rating (now) ▲▼</th>
  <th onclick="sort(6)">Δ ▲▼</th>
  <th onclick="sort(7)">Source ▲▼</th>
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
function filterGenre() {{
  const val = document.getElementById('gf').value.toLowerCase();
  let visible = 0;
  Array.from(document.querySelectorAll('#t tbody tr')).forEach(row => {{
    const genre = row.cells[2].textContent.toLowerCase();
    const show = !val || genre.split(',').map(g => g.trim()).includes(val);
    row.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  const total = document.querySelectorAll('#t tbody tr').length;
  document.getElementById('count').textContent = val ? visible + ' of ' + total + ' shown' : '';
}}
</script>
</body>
</html>"""

    (docs_dir / "index.html").write_text(page, encoding="utf-8")
    logger.info("Wrote catalog to %s (%d books)", docs_dir, len(records))


def send_email(
    books: list[Book],
    recipient: str,
    run_date: date | None = None,
    min_rating: float = 4.3,
    storygraph_books: list[Book] | None = None,
) -> bool:
    """Send the shortlist as an email. Returns True on success.

    Goodreads and StoryGraph picks are shown as separate sections.

    Uses Gmail SMTP with TLS (port 587). Requires a Gmail App Password
    (not a regular password) — 2FA must be enabled on the Google account.
    """
    run_date = run_date or date.today()
    storygraph_books = storygraph_books or []

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

    count = len(books) + len(storygraph_books)
    # Don't claim a single "over X" bar: the union mixes Goodreads (>=min_rating)
    # and StoryGraph (>4.0) picks, so a counted book may sit below min_rating.
    subject = (
        f"[Books] {count} new candidate{'s' if count != 1 else ''} "
        f"— {run_date.isoformat()}"
    )

    # Plain-text body — one section per source
    plain_lines = [
        f"New book shortlist — {run_date.isoformat()}",
        f"Full running list: {CATALOG_URL}",
        "",
        f"From Goodreads ({len(books)}):",
        "",
    ]
    if books:
        for i, book in enumerate(books, 1):
            plain_lines.append(_format_book_entry(i, book, markdown=False))
    else:
        plain_lines.append("No new Goodreads books passed the filter this week.")
    if storygraph_books:
        plain_lines += ["", f"From StoryGraph ({len(storygraph_books)}):", ""]
        for i, book in enumerate(storygraph_books, 1):
            plain_lines.append(_format_book_entry(i, book, markdown=False))
    plain_body = "\n".join(plain_lines)

    # HTML body — a helper builds one table per source
    def _table(book_list: list[Book]) -> str:
        if not book_list:
            return "<p>None this week.</p>"
        rows = []
        for i, book in enumerate(book_list, 1):
            genres = html.escape(", ".join(book.genre_tags) if book.genre_tags else "—")
            rating_str = f"{book.rating:.2f}" if book.rating is not None else "N/A"
            count_str = f"{book.rating_count:,}" if book.rating_count is not None else "N/A"
            link = book_link(book)
            title_link = (
                f'<a href="{html.escape(link)}">{html.escape(book.title)}</a>'
                if link else html.escape(book.title)
            )
            desc = book.description or ""
            desc_snippet = (desc[:160] + "…") if len(desc) > 160 else desc
            title_cell = (
                f'{title_link}<br><span style="font-size:0.85em;color:#666">{html.escape(desc_snippet)}</span>'
                if desc_snippet else title_link
            )
            rows.append(
                f"<tr><td>{i}</td><td>{title_cell}</td>"
                f"<td>{html.escape(book.author)}</td>"
                f"<td>{genres}</td>"
                f"<td>{rating_str} ({count_str})</td>"
                f"<td>{html.escape(book.pub_date or 'Unknown')}</td></tr>"
            )
        return (
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:0.9em'>"
            "<thead><tr><th>#</th><th>Title</th><th>Author</th><th>Genre</th>"
            "<th>Rating</th><th>Published</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        )

    storygraph_section = (
        f"<h3>From StoryGraph ({len(storygraph_books)})</h3>{_table(storygraph_books)}"
        if storygraph_books else ""
    )

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;max-width:700px;margin:0 auto;padding:1rem">
<h2>New book shortlist — {run_date.isoformat()}</h2>
<p><a href="{CATALOG_URL}">View the full running list with rating history →</a></p>
<h3>From Goodreads ({len(books)})</h3>
{_table(books)}
{storygraph_section}
<p style="font-size:0.8em;color:#666">Goodreads filter: ≥{min_rating} rating, ≥500 ratings · StoryGraph filter: &gt;4.0 rating, &gt;70 ratings</p>
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
