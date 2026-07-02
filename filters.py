"""Filter logic for book candidates."""

from scraper import Book

# Genres excluded regardless of rating (case-insensitive substring match).
# Substring match means "romance" also catches "Paranormal Romance",
# "Historical Romance", "Dark Romance", etc.
EXCLUDED_GENRES: tuple[str, ...] = (
    "romance", "romantasy", "erotica", "rom com", "romantic comedy",
)

# StoryGraph's community is much smaller than Goodreads', so it uses its own
# (lower) thresholds. The final feed is the UNION: a book qualifies if it passes
# the Goodreads thresholds OR these. Strictly-greater semantics per requirement
# ("rating >4.00 and count >70").
STORYGRAPH_MIN_RATING = 4.0
STORYGRAPH_MIN_COUNT = 70


def passes_filter(
    book: Book,
    min_rating: float,
    min_rating_count: int,
) -> bool:
    """Return True if the book meets the rating thresholds."""
    if book.rating is None or book.rating_count is None:
        return False
    return book.rating >= min_rating and book.rating_count >= min_rating_count


def passes_storygraph_filter(
    book: Book,
    min_rating: float = STORYGRAPH_MIN_RATING,
    min_rating_count: int = STORYGRAPH_MIN_COUNT,
) -> bool:
    """Return True if a StoryGraph book clears its (strictly-greater) thresholds."""
    if book.rating is None or book.rating_count is None:
        return False
    return book.rating > min_rating and book.rating_count > min_rating_count


def genres_excluded(
    genre_tags: list[str] | None,
    excluded: tuple[str, ...] = EXCLUDED_GENRES,
) -> bool:
    """Return True if any tag in the list matches an excluded genre (substring,
    case-insensitive). Empty/None tag lists are never excluded."""
    if not genre_tags:
        return False
    needles = [e.lower() for e in excluded]
    return any(
        needle in tag.lower()
        for tag in genre_tags
        for needle in needles
    )


def is_excluded_by_genre(
    book: Book,
    excluded: tuple[str, ...] = EXCLUDED_GENRES,
) -> bool:
    """Return True if any of the book's genre tags matches an excluded genre.

    Case-insensitive substring match. Books with no detected genres are never
    excluded — this preserves the broad, rating-based feed and avoids dropping
    good titles just because genre enrichment failed.
    """
    return genres_excluded(book.genre_tags, excluded)
