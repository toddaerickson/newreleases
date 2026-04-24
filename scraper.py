"""Goodreads scraping for new releases and book details."""

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


def fetch_new_releases(window_days: int = 90) -> list[Book]:
    """Scrape Goodreads popular-by-date pages for the trailing window."""
    books: list[Book] = []
    today = date.today()
    seen_urls: set[str] = set()
    cutoff = today - timedelta(days=window_days)

    # Cover each calendar month in the window (not 28-day steps)
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

        page_books_found = 0
        for anchor in soup.select("a.bookTitle, a[class*='BookCard'] a, a[href*='/book/show/']"):
            href = anchor.get("href", "")
            if not isinstance(href, str):
                continue
            if "/book/show/" not in href:
                continue
            full_url = f"https://www.goodreads.com{href}" if href.startswith("/") else href
            # Normalize URL (strip query params)
            full_url = full_url.split("?")[0]
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            title_text = anchor.get_text(strip=True)
            if not title_text:
                continue

            # Extract goodreads ID from URL
            gr_id_match = re.search(r"/book/show/(\d+)", full_url)
            gr_id = gr_id_match.group(1) if gr_id_match else None

            books.append(Book(
                title=title_text,
                author="",  # filled during enrichment
                goodreads_id=gr_id,
                goodreads_url=full_url,
            ))
            page_books_found += 1

        if page_books_found == 0:
            logger.warning("Zero books found on %s — selectors may be broken or page is JS-rendered", url)

        _polite_sleep()

    logger.info("Found %d candidate books from new-release pages", len(books))
    return books


def enrich_book(book: Book) -> Book:
    """Fetch the book's Goodreads page and populate rating, author, genres."""
    if not book.goodreads_url:
        return book

    soup = _get(book.goodreads_url)
    if not soup:
        logger.warning("Enrichment failed for %r (%s) — skipping", book.title, book.goodreads_url)
        return book

    # Author
    author_el = soup.select_one(
        "span.ContributorLink__name, "
        "a.authorName span, "
        "span[data-testid='name']"
    )
    if author_el:
        book.author = author_el.get_text(strip=True)

    # Rating
    rating_el = soup.select_one(
        "div.RatingStatistics__rating, "
        "span[itemprop='ratingValue'], "
        "div[class*='RatingStatistics'] div[class*='rating']"
    )
    if rating_el:
        raw = rating_el.get_text(strip=True)
        # Strip non-numeric content except decimal point
        cleaned = re.sub(r"[^\d.]", "", raw)
        try:
            book.rating = float(cleaned)
        except ValueError:
            logger.debug("Could not parse rating %r for %s", raw, book.goodreads_url)

    # Rating count
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

    # Publication date
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

    # Genre tags (top shelves)
    genre_els = soup.select(
        "span.BookPageMetadataSection__genreButton a, "
        "a.actionLinkLite.bookPageGenreLink, "
        "span[class*='GenreButton'] a"
    )
    book.genre_tags = [g.get_text(strip=True) for g in genre_els[:8]]

    # ISBN
    isbn_el = soup.select_one("meta[property='books:isbn']")
    if isbn_el:
        val = isbn_el.get("content")
        if isinstance(val, str):
            book.isbn13 = val

    # Title refinement — use the page's canonical title
    title_el = soup.select_one(
        "h1[data-testid='bookTitle'], "
        "h1#bookTitle, "
        "h1.Text__title1"
    )
    if title_el:
        book.title = title_el.get_text(strip=True)

    # Warn if enrichment produced no useful data (selector breakage signal)
    if book.author == "" and book.rating is None:
        logger.warning("Enrichment returned no author or rating for %r — selectors may be broken", book.title)

    _polite_sleep()
    return book


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
