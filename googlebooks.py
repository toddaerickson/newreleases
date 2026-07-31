"""Google Books discovery for recent science-fiction releases."""

import logging
from datetime import date, timedelta

import requests

from scraper import Book

logger = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/books/v1/volumes"
SUBJECT_QUERY = "subject:science fiction"
MAX_RESULTS = 40
# Google Books returns BISAC-style category paths ("Fiction / Science Fiction /
# Space Opera"), not bare labels, so these are matched as case-insensitive
# SUBSTRINGS. An exact set-membership test would reject every categorised volume
# and keep only the uncategorised ones — the exact inverse of the intent.
ALLOWED_GENRES = ("science fiction", "fiction")


def fetch_google_books_new_releases(window_days: int = 90) -> list[Book]:
    """Return recent science-fiction volumes with Google Books ratings."""
    cutoff = date.today() - timedelta(days=window_days)
    try:
        response = requests.get(
            API_URL,
            params={
                "q": SUBJECT_QUERY,
                "orderBy": "newest",
                "maxResults": MAX_RESULTS,
                "printType": "books",
                "startIndex": 0,
            },
            headers={"Accept": "application/json"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Google Books request failed: %s", exc)
        return []

    books: list[Book] = []
    seen_ids: set[str] = set()
    for item in payload.get("items", []):
        volume = item.get("volumeInfo", {})
        volume_id = item.get("id")
        if not volume_id or volume_id in seen_ids:
            continue

        categories = [
            category.strip().lower()
            for category in volume.get("categories", [])
            if category
        ]
        if categories and not any(
            allowed in category
            for category in categories
            for allowed in ALLOWED_GENRES
        ):
            continue

        pub_date = volume.get("publishedDate", "")
        try:
            published = date.fromisoformat(pub_date[:10])
        except (TypeError, ValueError):
            continue
        if published < cutoff or published > date.today():
            continue

        title = volume.get("title", "").strip()
        authors = volume.get("authors", [])
        if not title or not authors:
            continue

        seen_ids.add(volume_id)
        identifiers = volume.get("industryIdentifiers", [])
        isbn13 = next(
            (
                identifier.get("identifier")
                for identifier in identifiers
                if identifier.get("type") == "ISBN_13"
            ),
            None,
        )
        books.append(
            Book(
                title=title,
                author=", ".join(authors),
                isbn13=isbn13,
                pub_date=published.isoformat(),
                rating=volume.get("averageRating"),
                rating_count=volume.get("ratingsCount"),
                genre_tags=volume.get("categories", []),
                description=volume.get("description"),
                source="google_books",
                google_books_id=volume_id,
                google_books_url=volume.get(
                    "infoLink",
                    f"https://books.google.com/books?id={volume_id}",
                ),
            )
        )

    logger.info(
        "Found %d Google Books science-fiction releases in trailing %d days",
        len(books),
        window_days,
    )
    return books
