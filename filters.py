"""Filter logic for book candidates."""

from scraper import Book


def passes_filter(
    book: Book,
    min_rating: float = 4.3,
    min_rating_count: int = 500,
) -> bool:
    """Return True if the book meets the rating thresholds."""
    if book.rating is None or book.rating_count is None:
        return False
    return book.rating >= min_rating and book.rating_count >= min_rating_count
