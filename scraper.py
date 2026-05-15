"""Goodreads scraping for new releases and book details."""

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.goodreads.com/",
    "Connection": "keep-alive",
}

REQUEST_DELAY = 2.0  # base seconds between requests to avoid rate-limiting

GOODREADS_NEW_RELEASES_URL = "https://www.goodreads.com/book/popular_by_date/{year}/{month}"
GOODREADS_SEARCH_URL = "https://www.goodreads.com/search"

ALLOWED_HOSTS = {"www.goodreads.com", "goodreads.com"}

# Cloudflare/bot challenge signatures in page titles or body
_CHALLENGE_SIGNATURES = [
    "just a moment",
    "access denied",
    "attention required",
    "checking your browser",
    "enable javascript",
]


@dataclass
class Book:
    title: str
    author: str
    isbn13: str | None = None
    goodreads_id: str | None = None
    goodreads_url: str | None = None
    pub_date: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    genre_tags: list[str] = field(default_factory=list)
    description: str | None = None


def _build_session() -> requests.Session:
    """Build a requests.Session with retry/backoff for transient errors."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=3,        # 3s, 6s, 12s
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


# Module-level session for connection pooling and cookie persistence
_session = _build_session()


def _is_allowed_url(url: str) -> bool:
    """Validate that the URL points to an allowed Goodreads domain."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc in ALLOWED_HOSTS


def _is_challenge_page(soup: BeautifulSoup) -> bool:
    """Detect Cloudflare or bot challenge pages."""
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True).lower()
        for sig in _CHALLENGE_SIGNATURES:
            if sig in title_text:
                return True
    return False


def _get(url: str, params: dict | None = None) -> BeautifulSoup | None:
    """Fetch a URL and return parsed soup, or None on failure.

    Uses a shared session with automatic retry/backoff for 429 and 5xx.
    """
    if not _is_allowed_url(url):
        logger.warning("Refusing to fetch non-Goodreads URL: %s", url)
        return None
    try:
        resp = _session.get(url, params=params, timeout=(5, 15))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        if _is_challenge_page(soup):
            logger.error("Cloudflare/bot challenge detected at %s — scraper may be blocked", url)
            return None

        return soup
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


def _polite_sleep() -> None:
    """Sleep with jitter to avoid bot fingerprinting."""
    time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))


def _extract_books_from_apollo(soup: BeautifulSoup) -> list[Book]:
    """Extract books from Goodreads' embedded Apollo/Next.js JSON state.

    Goodreads uses React with Apollo GraphQL. The full book data (title,
    author, rating, URL) is embedded in a <script id="__NEXT_DATA__"> tag
    as JSON. This is far more reliable than CSS selectors on JS-rendered HTML.
    """
    script_tag = soup.select_one("script#__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return []

    try:
        data = json.loads(script_tag.string)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse __NEXT_DATA__ JSON")
        return []

    apollo = data.get("props", {}).get("pageProps", {}).get("apolloState", {})
    if not apollo:
        return []

    # Index Contributor entities by their ref ID
    contributors: dict[str, str] = {}
    for key, val in apollo.items():
        if key.startswith("Contributor:") and isinstance(val, dict):
            contributors[key] = val.get("name", "")

    # Index Work entities (contain rating stats) by their ref ID
    works: dict[str, dict] = {}
    for key, val in apollo.items():
        if key.startswith("Work:") and isinstance(val, dict):
            works[key] = val.get("stats", {})

    # Extract Book entities
    books: list[Book] = []
    for key, val in apollo.items():
        if not key.startswith("Book:") or not isinstance(val, dict):
            continue

        title = val.get("title") or val.get("titleComplete", "")
        if not title:
            continue

        web_url = val.get("webUrl", "")
        legacy_id = val.get("legacyId")
        gr_id = str(legacy_id) if legacy_id else None

        # Resolve author from contributor ref
        author = ""
        contrib_edge = val.get("primaryContributorEdge", {})
        if isinstance(contrib_edge, dict):
            contrib_node = contrib_edge.get("node", {})
            if isinstance(contrib_node, dict):
                ref = contrib_node.get("__ref", "")
                author = contributors.get(ref, "")

        # Resolve rating from work ref
        rating = None
        rating_count = None
        work_ref = val.get("work", {})
        if isinstance(work_ref, dict):
            ref = work_ref.get("__ref", "")
            stats = works.get(ref, {})
            if isinstance(stats, dict):
                avg = stats.get("averageRating")
                cnt = stats.get("ratingsCount")
                if avg is not None:
                    rating = float(avg)
                if cnt is not None:
                    rating_count = int(cnt)

        books.append(Book(
            title=title,
            author=author,
            goodreads_id=gr_id,
            goodreads_url=web_url,
            rating=rating,
            rating_count=rating_count,
        ))

    return books


def _extract_books_from_html(soup: BeautifulSoup) -> list[Book]:
    """Fallback: extract books from HTML anchors (old Goodreads layout)."""
    books: list[Book] = []
    seen_urls: set[str] = set()

    for anchor in soup.select("a.bookTitle, a[class*='BookCard'], a[href*='/book/show/']"):
        href = anchor.get("href", "")
        if not isinstance(href, str):
            continue
        if "/book/show/" not in href:
            continue
        full_url = f"https://www.goodreads.com{href}" if href.startswith("/") else href
        full_url = full_url.split("?")[0]
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Try anchor text, then image alt text as title fallback
        title_text = anchor.get_text(strip=True)
        if not title_text:
            img = anchor.select_one("img[alt]")
            if img:
                alt = img.get("alt", "")
                if isinstance(alt, str):
                    title_text = alt.replace(" Book Cover", "").strip()
        if not title_text:
            continue

        gr_id_match = re.search(r"/book/show/(\d+)", full_url)
        gr_id = gr_id_match.group(1) if gr_id_match else None

        books.append(Book(
            title=title_text,
            author="",  # filled during enrichment
            goodreads_id=gr_id,
            goodreads_url=full_url,
        ))

    return books


def fetch_new_releases(window_days: int = 90) -> list[Book]:
    """Scrape Goodreads popular-by-date pages for the trailing window.

    Primary strategy: parse the embedded Apollo/Next.js JSON state, which
    contains title, author, rating, and URL for all books on the page.
    Fallback: extract from HTML anchors if JSON is unavailable.
    """
    books: list[Book] = []
    today = date.today()
    seen_urls: set[str] = set()
    cutoff = today - timedelta(days=window_days)

    # Cover each calendar month in the window
    months_to_check: set[tuple[int, int]] = set()
    year, month = today.year, today.month
    while date(year, month, 1) >= date(cutoff.year, cutoff.month, 1):
        months_to_check.add((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1

    for yr, mo in sorted(months_to_check):
        url = GOODREADS_NEW_RELEASES_URL.format(year=yr, month=mo)
        logger.info("Fetching new releases: %s", url)
        soup = _get(url)
        if not soup:
            continue

        # Try Apollo JSON first (reliable), then fall back to HTML selectors
        page_books = _extract_books_from_apollo(soup)
        if page_books:
            logger.info("Extracted %d books from Apollo JSON on %s", len(page_books), url)
        else:
            page_books = _extract_books_from_html(soup)
            if page_books:
                logger.info("Extracted %d books from HTML on %s", len(page_books), url)
            else:
                logger.warning("Zero books found on %s — page may be blocked or layout changed", url)

        for book in page_books:
            book_url = book.goodreads_url
            if book_url and book_url not in seen_urls:
                seen_urls.add(book_url)
                books.append(book)

        _polite_sleep()

    logger.info("Found %d candidate books from new-release pages", len(books))
    return books


def _enrich_from_apollo(soup: BeautifulSoup, book: Book) -> bool:
    """Try to enrich book from Apollo JSON on the detail page. Returns True if successful."""
    script_tag = soup.select_one("script#__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return False

    try:
        data = json.loads(script_tag.string)
    except (json.JSONDecodeError, TypeError):
        return False

    apollo = data.get("props", {}).get("pageProps", {}).get("apolloState", {})
    if not apollo:
        return False

    # Find the Book entity matching our ID
    target_id = book.goodreads_id
    book_data = None
    for key, val in apollo.items():
        if not key.startswith("Book:") or not isinstance(val, dict):
            continue
        if str(val.get("legacyId")) == target_id:
            book_data = val
            break

    if not book_data:
        # If only one Book entity, use it
        book_entries = {k: v for k, v in apollo.items() if k.startswith("Book:") and isinstance(v, dict)}
        if len(book_entries) == 1:
            book_data = next(iter(book_entries.values()))

    if not book_data:
        return False

    # Title
    title = book_data.get("titleComplete") or book_data.get("title")
    if title:
        book.title = title

    # Description (Apollo stores raw HTML; strip tags for plain text)
    if not book.description:
        raw_desc = book_data.get("description", "")
        if raw_desc and isinstance(raw_desc, str):
            book.description = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True)

    # Author
    contrib_edge = book_data.get("primaryContributorEdge", {})
    if isinstance(contrib_edge, dict):
        contrib_node = contrib_edge.get("node", {})
        if isinstance(contrib_node, dict):
            ref = contrib_node.get("__ref", "")
            contrib = apollo.get(ref, {})
            if isinstance(contrib, dict) and contrib.get("name"):
                book.author = contrib["name"]

    # Rating from Work stats
    work_ref = book_data.get("work", {})
    if isinstance(work_ref, dict):
        ref = work_ref.get("__ref", "")
        work = apollo.get(ref, {})
        if isinstance(work, dict):
            stats = work.get("stats", {})
            if isinstance(stats, dict):
                avg = stats.get("averageRating")
                cnt = stats.get("ratingsCount")
                if avg is not None:
                    book.rating = float(avg)
                if cnt is not None:
                    book.rating_count = int(cnt)

    # Genre tags: follow the book entity's own bookGenres edges first.
    # Chain: book_data.bookGenres[] -> BookGenre entity -> Genre entity -> name
    for genre_ref in book_data.get("bookGenres", []):
        if not isinstance(genre_ref, dict):
            continue
        bg_key = genre_ref.get("__ref", "")
        bg_entity = apollo.get(bg_key, {})
        if not isinstance(bg_entity, dict):
            continue
        genre_ptr = bg_entity.get("genre", {})
        g_key = genre_ptr.get("__ref", "") if isinstance(genre_ptr, dict) else ""
        g_entity = apollo.get(g_key, {})
        name = g_entity.get("name") if isinstance(g_entity, dict) else None
        if name and name not in book.genre_tags and len(book.genre_tags) < 8:
            book.genre_tags.append(name)

    # Fallback: scan all BookGenre/Genre entities on the page (catches alternate layouts)
    if not book.genre_tags:
        for key, val in apollo.items():
            if not isinstance(val, dict):
                continue
            if key.startswith("BookGenre:"):
                genre_ptr = val.get("genre", {})
                g_key = genre_ptr.get("__ref", "") if isinstance(genre_ptr, dict) else ""
                g_entity = apollo.get(g_key, {})
                name = g_entity.get("name") if isinstance(g_entity, dict) else None
            elif key.startswith("Genre:"):
                name = val.get("name")
            else:
                continue
            if name and name not in book.genre_tags and len(book.genre_tags) < 8:
                book.genre_tags.append(name)

    return book.author != "" or book.rating is not None


def enrich_book(book: Book, force: bool = False) -> Book:
    """Fetch the book's Goodreads page and populate rating, author, genres.

    Primary strategy: parse Apollo JSON state from the detail page.
    Fallback: extract from HTML with CSS selectors.
    Set force=True to fetch detail page even if basic data exists (for genres/pub_date).
    """
    if not book.goodreads_url:
        return book

    # Skip enrichment if we already have rating data from the listing page
    if not force and book.rating is not None and book.rating_count is not None and book.author:
        logger.debug("Skipping enrichment for %r — already have data from listing", book.title)
        return book

    soup = _get(book.goodreads_url)
    if not soup:
        logger.warning("Enrichment failed for %r (%s) — skipping", book.title, book.goodreads_url)
        return book

    # Try Apollo JSON first for rating/author
    if _enrich_from_apollo(soup, book):
        logger.debug("Enriched %r from Apollo JSON", book.title)

    # Always try CSS selectors for fields Apollo doesn't provide
    # (genres, pub_date, and as fallback for rating/author)
    if not book.genre_tags or not book.pub_date or not book.author or book.rating is None:
        _enrich_from_html(soup, book)

    # ISBN (only available in HTML meta tags)
    isbn_el = soup.select_one("meta[property='books:isbn']")
    if isbn_el:
        val = isbn_el.get("content")
        if isinstance(val, str):
            book.isbn13 = val

    # Warn if enrichment produced no useful data
    if book.author == "" and book.rating is None:
        logger.warning("Enrichment returned no author or rating for %r — selectors may be broken", book.title)

    _polite_sleep()
    return book


def _enrich_from_html(soup: BeautifulSoup, book: Book) -> None:
    """Enrich book from HTML CSS selectors. Only fills missing fields."""
    # Author (only if missing)
    if not book.author:
        author_el = soup.select_one(
            "span.ContributorLink__name, "
            "a.authorName span, "
            "span[data-testid='name']"
        )
        if author_el:
            book.author = author_el.get_text(strip=True)

    # Rating (only if missing)
    if book.rating is None:
        rating_el = soup.select_one(
            "div.RatingStatistics__rating, "
            "span[itemprop='ratingValue'], "
            "div[class*='RatingStatistics'] div[class*='rating']"
        )
        if rating_el:
            raw = rating_el.get_text(strip=True)
            cleaned = re.sub(r"[^\d.]", "", raw)
            try:
                book.rating = float(cleaned)
            except ValueError:
                logger.debug("Could not parse rating %r for %s", raw, book.goodreads_url)

    # Rating count (only if missing)
    if book.rating_count is None:
        count_el = soup.select_one(
            "span[data-testid='ratingsCount'], "
            "meta[itemprop='ratingCount'], "
            "span[class*='ratingsCount']"
        )
        if count_el:
            raw_content = count_el.get("content")
            count_text = (raw_content if isinstance(raw_content, str) else None) or count_el.get_text(strip=True)
            count_text = re.sub(r"[^\d]", "", count_text)
            if count_text:
                book.rating_count = int(count_text)

    # Publication date (only if missing)
    if not book.pub_date:
        pub_el = soup.select_one(
            "p[data-testid='publicationInfo'], "
            "div.FeaturedDetails p, "
            "div#details div.row"
        )
        if pub_el:
            pub_text = pub_el.get_text(strip=True)
            date_match = re.search(
                r"(?:Published|First published|Expected publication)[:\s]+"
                r"(\w+\s+\d{1,2},?\s+\d{4})",
                pub_text,
            )
            if date_match:
                raw_date = date_match.group(1).replace(",", "")
                try:
                    parsed = datetime.strptime(raw_date, "%B %d %Y")
                    book.pub_date = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    logger.debug("Could not parse pub date %r for %s", raw_date, book.goodreads_url)

    # Genre tags (only if missing)
    if not book.genre_tags:
        genre_els = soup.select(
            "span.BookPageMetadataSection__genreButton a, "
            "a.actionLinkLite.bookPageGenreLink, "
            "span[class*='GenreButton'] a"
        )
        book.genre_tags = [g.get_text(strip=True) for g in genre_els[:8]]
        if not book.genre_tags:
            logger.warning("No genre tags found for %r (%s) — selectors may be stale",
                           book.title, book.goodreads_url)

    # Description (only if missing)
    if not book.description:
        desc_el = soup.select_one(
            "div.BookPageMetadataSection__description span.Formatted, "
            "div[class*='BookPageMetadataSection__description'] span, "
            "div#description span"
        )
        if desc_el:
            book.description = desc_el.get_text(" ", strip=True)


def search_and_enrich(title: str, author: str) -> Book | None:
    """Search Goodreads by title+author and return enriched book if found."""
    query = f"{title} {author}"
    soup = _get(GOODREADS_SEARCH_URL, params={"q": query})
    if not soup:
        return None

    first_result = soup.select_one("a.bookTitle, tr[itemtype*='Book'] a")
    if not first_result:
        return None

    href = first_result.get("href", "")
    if not isinstance(href, str):
        return None
    full_url = f"https://www.goodreads.com{href}" if href.startswith("/") else href
    full_url = full_url.split("?")[0]

    gr_id_match = re.search(r"/book/show/(\d+)", full_url)
    book = Book(
        title=title,
        author=author,
        goodreads_id=gr_id_match.group(1) if gr_id_match else None,
        goodreads_url=full_url,
    )
    return enrich_book(book)
