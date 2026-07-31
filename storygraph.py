"""StoryGraph scraping for new releases and community ratings.

StoryGraph (app.thestorygraph.com) is a Goodreads alternative. It sits behind a
Cloudflare managed challenge that plain ``requests`` cannot pass, so this module
uses ``curl_cffi`` with a Chrome TLS-impersonation fingerprint.

Two facts shape the approach (see the project plan / spike notes):

1. Browse filtering & sorting are login-gated — anonymous requests ignore the
   sort/pub-year params. Pagination (``/browse?page=N``) does work, and the
   default order is popularity-ish, mixing publication years. So new releases are
   found by paging the browse and filtering client-side by publication date.
   Coverage is therefore popularity-biased, not an exhaustive new-release list.
2. A book's community average rating and rating count are NOT in the listing or
   the main book page; they load lazily from the fragment
   ``/books/<uuid>/community_reviews``. Every candidate must be enriched (one
   fragment request) before it can be filtered on rating/count.
"""

import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from scraper import Book

logger = logging.getLogger(__name__)

BASE_URL = "https://app.thestorygraph.com"
BROWSE_URL = f"{BASE_URL}/browse"
SEARCH_URL = f"{BASE_URL}/search"
ALLOWED_HOSTS = {"app.thestorygraph.com", "thestorygraph.com"}

# curl_cffi browser profile used to clear the Cloudflare challenge.
#
# This rots: Cloudflare eventually starts rejecting a given fingerprint and every
# fetch 403s, which looks exactly like "no new books this week". Verified
# 2026-07-29: every chrome/safari/android profile returned 403 "Just a moment...",
# firefox135 returned 200. When StoryGraph starts 403-ing again, try the newer
# profiles in `curl_cffi.requests.impersonate` before assuming the site changed.
IMPERSONATE = "firefox135"

REQUEST_DELAY = 2.0  # base seconds between requests, matching the Goodreads scraper
DEFAULT_MAX_PAGES = 10  # browse pages to scan per run (~10 books/page)

# StoryGraph tags mix genres with mood + pace labels. These vocabularies are
# stripped so only genre tags remain (for display and romance exclusion).
STORYGRAPH_MOODS = {
    "adventurous", "challenging", "dark", "emotional", "funny", "hopeful",
    "informative", "inspiring", "lighthearted", "mysterious", "reflective",
    "relaxing", "sad", "tense",
}

# Cloudflare/bot challenge signatures.
_CHALLENGE_SIGNATURES = [
    "just a moment", "attention required", "checking your browser",
    "cf-mitigated", "enable javascript",
]


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc in ALLOWED_HOSTS


def _get(url: str, params: dict | None = None) -> str | None:
    """Fetch a StoryGraph URL and return HTML text, or None on failure.

    Uses curl_cffi Chrome impersonation with a small manual retry/backoff for
    transient errors (curl_cffi has no urllib3-style Retry adapter).
    """
    if not _is_allowed_url(url):
        logger.warning("Refusing to fetch non-StoryGraph URL: %s", url)
        return None

    backoff = 3
    for attempt in range(3):
        try:
            resp = cffi_requests.get(
                url, params=params, impersonate=IMPERSONATE, timeout=25
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                logger.warning("StoryGraph %s returned %d (attempt %d)",
                               url, resp.status_code, attempt + 1)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 403:
                logger.error("StoryGraph %s returned 403 — Cloudflare block "
                             "(curl_cffi may need a newer browser profile)", url)
                return None
            resp.raise_for_status()
            text = resp.text
            low = text[:2000].lower()
            if any(sig in low for sig in _CHALLENGE_SIGNATURES):
                logger.error("Cloudflare/bot challenge detected at %s", url)
                return None
            return text
        except Exception as e:  # curl_cffi raises its own exception types
            logger.warning("Failed to fetch %s (attempt %d): %s", url, attempt + 1, e)
            time.sleep(backoff)
            backoff *= 2
    return None


def _polite_sleep() -> None:
    time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))


def _parse_pub(pane_text: str) -> tuple[date | None, int | None]:
    """Extract (edition_pub_date, original_pub_year) from a book-pane's text."""
    edition_date = None
    m = re.search(r"Edition Pub Date:\s*(\d{1,2}\s+\w{3}\s+\d{4})", pane_text)
    if m:
        try:
            edition_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except ValueError:
            pass
    year = None
    m = re.search(r"Original Pub Year:\s*(\d{4})", pane_text)
    if m:
        year = int(m.group(1))
    return edition_date, year


def _extract_genres(pane) -> list[str]:
    """Genre tags from the pane's tag section, minus mood/pace labels."""
    tag_section = pane.select_one("div.book-pane-tag-section")
    if not tag_section:
        return []
    raw = [t.get_text(strip=True).lower()
           for t in tag_section.select("a, span")
           if t.get_text(strip=True)]
    genres: list[str] = []
    for tag in raw:
        if tag in genres:
            continue
        if tag in STORYGRAPH_MOODS or tag.endswith("-paced"):
            continue
        genres.append(tag)
    # No [:N] truncation: the full genre set must reach is_excluded_by_genre so a
    # 'romance'/'erotica' tag can never be truncated away before the hard genre
    # exclusion runs. (StoryGraph panes surface only a few genres after mood/pace
    # stripping, so there is no display bloat.)
    return genres


def _parse_pane(pane, cutoff: date) -> Book | None:
    """Build a Book from one browse ``div.book-pane``, or None if not a recent
    release / not parseable."""
    book_id = pane.get("data-book-id")
    if not book_id:
        return None

    # Title: first /books/<uuid> link with text that isn't the "editions" link.
    title = ""
    for a in pane.select("a[href^='/books/']"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if text and "/editions" not in href:
            title = text
            break
    if not title:
        return None

    # Author: first /authors/ link.
    author = ""
    author_a = pane.select_one("a[href^='/authors/']")
    if author_a:
        author = author_a.get_text(strip=True)

    pane_text = pane.get_text(" ", strip=True)

    # Recency gate. Skip reprints of older works (original year in the past), and
    # respect the trailing window when a precise edition date is available.
    edition_date, year = _parse_pub(pane_text)
    if year is not None and year < cutoff.year:
        return None
    if edition_date is not None:
        if edition_date < cutoff:
            return None
        pub_date = edition_date.isoformat()
    elif year is not None:
        if year < cutoff.year:
            return None
        pub_date = str(year)
    else:
        return None  # no date signal — cannot confirm it is a new release

    # ISBN-13 (13 digits) when present in the "ISBN/UID:" field.
    isbn13 = None
    m = re.search(r"ISBN/UID:\s*(\w+)", pane_text)
    if m and re.fullmatch(r"\d{13}", m.group(1)):
        isbn13 = m.group(1)

    return Book(
        title=title,
        author=author,
        isbn13=isbn13,
        pub_date=pub_date,
        genre_tags=_extract_genres(pane),
        source="storygraph",
        storygraph_id=book_id,
        storygraph_url=f"{BASE_URL}/books/{book_id}",
    )


def fetch_storygraph_new_releases(
    window_days: int = 90, max_pages: int = DEFAULT_MAX_PAGES
) -> list[Book]:
    """Scrape StoryGraph's browse pages and return recent releases.

    Rating/count are left unset here — they require a per-book detail fetch and
    are populated later by :func:`enrich_storygraph_book` (for un-seen books
    only, to keep request volume down).
    """
    from bs4 import BeautifulSoup

    today = date.today()
    cutoff = today - timedelta(days=window_days)
    books: list[Book] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        params = {"page": str(page)} if page > 1 else None
        html = _get(BROWSE_URL, params=params)
        if not html:
            logger.warning("StoryGraph browse page %d failed — stopping", page)
            break

        soup = BeautifulSoup(html, "html.parser")
        panes = soup.select("div.book-pane")
        if not panes:
            logger.info("No book panes on StoryGraph browse page %d — stopping", page)
            break

        page_new = 0
        for pane in panes:
            book = _parse_pane(pane, cutoff)
            if not book or not book.storygraph_id:
                continue
            if book.storygraph_id in seen_ids:
                continue
            seen_ids.add(book.storygraph_id)
            books.append(book)
            page_new += 1

        logger.info("StoryGraph browse page %d: %d recent releases (of %d panes)",
                    page, page_new, len(panes))
        _polite_sleep()

    logger.info("Found %d StoryGraph candidate releases in trailing %d days",
                len(books), window_days)
    return books


_UUID_RE = re.compile(
    r"^/books/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _parse_search_item(anchor) -> tuple[str, str]:
    """Return (title, author) for one search hit, or ("", "") if unreadable.

    A hit renders as::

        <li class="book-list-item">
          <h1 class="sr-only">Title by Author</h1>
          <a href="/books/<uuid>">
            <img alt="Title by Author">
            <h1><span class="list-option-text">Title</span></h1>   (twice: md/mobile)
            <h2 class="list-option-text">Author</h2>
          </a>
        </li>

    so a plain get_text() on the anchor yields the title two or three times over.
    There is no /authors/ link to lean on. Falls back to splitting the screen-
    reader heading on " by " when the classes change.

    An empty author is meaningful: callers must not guess a match without one.
    """
    title_el = anchor.select_one("h1 span.list-option-text, h1 span")
    author_el = anchor.select_one("h2.list-option-text, h2")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    author = author_el.get_text(" ", strip=True) if author_el else ""
    if title:
        return title, author

    parent = anchor.find_parent(["li", "div"])
    heading = parent.select_one("h1.sr-only") if parent else None
    label = heading.get_text(" ", strip=True) if heading else (anchor.get("title") or "")
    if not label:
        img = anchor.find("img")
        label = (img.get("alt") or "") if img else ""
    label = re.sub(r"\s+", " ", label).strip()
    if " by " in label:
        head, _, tail = label.rpartition(" by ")
        return head.strip(), tail.strip()
    return label, ""


def search_storygraph_candidates(title: str, max_results: int = 8) -> list[dict]:
    """Return StoryGraph search hits for a title as {title, author, storygraph_id}.

    Used by the award-winner lookup, which starts from a title+author string and
    has no StoryGraph uuid. Deliberately does NOT reuse _parse_pane: that applies
    a publication-date recency gate, and award winners are usually previous-year
    books, so every one of them would be discarded.
    """
    from bs4 import BeautifulSoup

    html = _get(SEARCH_URL, params={"search_term": title})
    _polite_sleep()
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict] = []
    seen_ids: set[str] = set()

    for anchor in soup.select("a[href^='/books/']"):
        href = anchor.get("href") or ""
        m = _UUID_RE.match(href)
        if not m:
            continue  # skips /editions, /reviews, and other sub-pages
        book_id = m.group(1)
        if book_id in seen_ids:
            continue
        cand_title, author = _parse_search_item(anchor)
        if not cand_title:
            continue
        seen_ids.add(book_id)
        candidates.append({
            "title": cand_title,
            "author": author,
            "storygraph_id": book_id,
            "storygraph_url": f"{BASE_URL}/books/{book_id}",
        })
        if len(candidates) >= max_results:
            break

    return candidates


def enrich_storygraph_book(book: Book) -> Book:
    """Populate a StoryGraph book's community rating + count from the
    ``community_reviews`` fragment. Best-effort: leaves fields None on failure."""
    from bs4 import BeautifulSoup

    if not book.storygraph_id:
        return book

    url = f"{BASE_URL}/books/{book.storygraph_id}/community_reviews"
    html = _get(url)
    if not html:
        logger.warning("StoryGraph enrichment failed for %r (%s)", book.title, url)
        return book

    soup = BeautifulSoup(html, "html.parser")

    avg_el = soup.select_one("span.average-star-rating")
    if avg_el:
        try:
            book.rating = float(avg_el.get_text(strip=True))
        except ValueError:
            logger.debug("Could not parse StoryGraph rating for %r", book.title)

    m = re.search(r"based on\s+([\d,]+)\s+review", soup.get_text(" ", strip=True), re.I)
    if m:
        book.rating_count = int(m.group(1).replace(",", ""))

    if book.rating is None and book.rating_count is None:
        logger.warning("No rating found for StoryGraph book %r — likely too new", book.title)

    _polite_sleep()
    return book
