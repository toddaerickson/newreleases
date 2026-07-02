"""Filter logic for book candidates."""

from scraper import Book

# Genres excluded regardless of rating (case-insensitive substring match).
# Substring match means "romance" also catches "Paranormal Romance",
# "Historical Romance", "Dark Romance", etc.
EXCLUDED_GENRES: tuple[str, ...] = (
    "romance", "romantasy", "erotica", "rom com", "romantic comedy",
)


def passes_filter(
    book: Book,
    min_rating: float,
    min_rating_count: int,
) -> bool:
    """Return True if the book meets the rating thresholds."""
    if book.rating is None or book.rating_count is None:
        return False
    return book.rating >= min_rating and book.rating_count >= min_rating_count


def is_excluded_by_genre(
    book: Book,
    excluded: tuple[str, ...] = EXCLUDED_GENRES,
) -> bool:
    """Return True if any of the book's genre tags matches an excluded genre.

    Case-insensitive substring match. Books with no detected genres are never
    excluded — this preserves the broad, rating-based feed and avoids dropping
    good titles just because genre enrichment failed.
    """
    if not book.genre_tags:
        return False
    needles = [e.lower() for e in excluded]
    return any(
        needle in tag.lower()
        for tag in book.genre_tags
        for needle in needles
    )
