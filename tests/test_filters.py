"""Threshold and genre-exclusion logic.

These are the rules that decide what lands in the inbox, and they are pure
functions — worth pinning exactly, especially the boundary behaviour (Goodreads
is inclusive, StoryGraph is strictly-greater).
"""

from filters import (
    EXCLUDED_GENRES,
    GOODREADS_MIN_COUNT,
    GOODREADS_MIN_RATING,
    STORYGRAPH_MIN_COUNT,
    STORYGRAPH_MIN_RATING,
    genres_excluded,
    is_excluded_by_genre,
    passes_filter,
    passes_google_books_filter,
    passes_storygraph_filter,
)
from scraper import Book


def book(**kwargs) -> Book:
    defaults = {"title": "T", "author": "A"}
    return Book(**{**defaults, **kwargs})


class TestGoodreadsFilter:
    def test_accepts_book_exactly_on_both_thresholds(self):
        # Inclusive bounds: a book sitting exactly on the bar must pass.
        assert passes_filter(
            book(rating=GOODREADS_MIN_RATING, rating_count=GOODREADS_MIN_COUNT)
        )

    def test_rejects_rating_one_step_below(self):
        assert not passes_filter(book(rating=4.09, rating_count=10_000))

    def test_rejects_count_one_below(self):
        assert not passes_filter(book(rating=4.9, rating_count=GOODREADS_MIN_COUNT - 1))

    def test_rejects_missing_rating(self):
        # An un-enriched book must never pass on absent data.
        assert not passes_filter(book(rating=None, rating_count=10_000))

    def test_rejects_missing_count(self):
        assert not passes_filter(book(rating=4.9, rating_count=None))


class TestStoryGraphFilter:
    def test_rejects_book_exactly_on_threshold(self):
        # Strictly-greater semantics: exactly 4.0 / exactly 70 must NOT pass.
        assert not passes_storygraph_filter(
            book(rating=STORYGRAPH_MIN_RATING, rating_count=STORYGRAPH_MIN_COUNT)
        )

    def test_accepts_just_above_threshold(self):
        assert passes_storygraph_filter(
            book(rating=STORYGRAPH_MIN_RATING + 0.01,
                 rating_count=STORYGRAPH_MIN_COUNT + 1)
        )

    def test_rejects_missing_data(self):
        assert not passes_storygraph_filter(book(rating=None, rating_count=500))


class TestGoogleBooksFilter:
    def test_uses_its_own_lower_count_bar(self):
        # A volume with 12 ratings passes Google Books but not Goodreads. This is
        # the whole reason the source has separate thresholds.
        b = book(rating=4.5, rating_count=12)
        assert passes_google_books_filter(b)
        assert not passes_filter(b)

    def test_rejects_missing_data(self):
        # Most Google Books volumes carry no ratingsCount at all.
        assert not passes_google_books_filter(book(rating=None, rating_count=None))


class TestGenreExclusion:
    def test_substring_match_catches_romance_subgenres(self):
        assert is_excluded_by_genre(book(genre_tags=["Paranormal Romance"]))
        assert is_excluded_by_genre(book(genre_tags=["Dark Romance", "Fantasy"]))

    def test_case_insensitive(self):
        assert is_excluded_by_genre(book(genre_tags=["ROMANTASY"]))

    def test_keeps_untagged_books(self):
        # Deliberate: genre enrichment fails often, and this is a rating-based
        # feed. No tags must never mean "drop it".
        assert not is_excluded_by_genre(book(genre_tags=[]))
        assert not genres_excluded(None)

    def test_keeps_unrelated_genres(self):
        assert not is_excluded_by_genre(book(genre_tags=["Historical Fiction", "Mystery"]))

    def test_every_configured_genre_actually_excludes(self):
        # Guards against a typo in EXCLUDED_GENRES silently disabling an entry.
        for genre in EXCLUDED_GENRES:
            assert is_excluded_by_genre(book(genre_tags=[genre.title()])), genre
