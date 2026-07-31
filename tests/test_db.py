"""Dedup keying and the cross-source write guards.

Dedup is permanent in this project — a wrong key or a clobbered row silently
loses a book forever, with no error anywhere. These run against a temp DB.
"""

import pytest

from db import (
    _title_author_hash,
    get_all_catalog_books,
    get_conn,
    is_seen,
    is_seen_by_name,
    log_book,
)


@pytest.fixture
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    yield c
    c.close()


class TestKeying:
    def test_hash_is_normalized(self):
        # Whitespace and case must not produce two keys for one book.
        assert _title_author_hash("  Dune ", "Frank Herbert") == _title_author_hash(
            "dune", "  FRANK HERBERT"
        )

    def test_different_books_get_different_keys(self):
        assert _title_author_hash("Dune", "Herbert") != _title_author_hash(
            "Dune", "Someone Else"
        )


class TestIsSeen:
    def test_isbn_roundtrip(self, conn):
        assert not is_seen(conn, "9780441013593", "Dune", "Frank Herbert")
        log_book(conn, title="Dune", author="Frank Herbert", isbn13="9780441013593")
        assert is_seen(conn, "9780441013593", "Dune", "Frank Herbert")

    def test_falls_back_to_name_hash_without_isbn(self, conn):
        log_book(conn, title="Dune", author="Frank Herbert")
        assert is_seen(conn, None, "Dune", "Frank Herbert")
        # Same book, different edition ISBN -> the ISBN key misses it...
        assert not is_seen(conn, "9780441013593", "Dune", "Frank Herbert")
        # ...which is exactly why the name-based check exists.
        assert is_seen_by_name(conn, "dune", "FRANK HERBERT")

    def test_name_lookup_ignores_surrounding_whitespace(self, conn):
        log_book(conn, title="  Dune  ", author=" Frank Herbert ")
        assert is_seen_by_name(conn, "Dune", "Frank Herbert")


class TestCrossSourceWriteGuard:
    def test_storygraph_cannot_unpass_a_passing_goodreads_row(self, conn):
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=4.5,
                 rating_count=900, passed_filter=True, source="goodreads",
                 goodreads_url="https://goodreads.example/dune")
        # A StoryGraph write on the same key must be refused, or the book drops
        # out of the catalog and its rating history is overwritten.
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=3.9,
                 rating_count=40, passed_filter=False, source="storygraph",
                 storygraph_url="https://storygraph.example/dune")
        row = conn.execute("SELECT * FROM seen_books WHERE isbn13 = '978'").fetchone()
        assert row["passed_filter"] == 1
        assert row["source"] == "goodreads"
        assert row["last_rating"] == 4.5

    def test_storygraph_may_overwrite_a_failed_goodreads_row(self, conn):
        # The union means a book can fail Goodreads' bar and still clear
        # StoryGraph's, so this write must be allowed through.
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=4.0,
                 rating_count=10, passed_filter=False, source="goodreads")
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=4.4,
                 rating_count=90, passed_filter=True, source="storygraph",
                 storygraph_url="https://storygraph.example/dune")
        row = conn.execute("SELECT * FROM seen_books WHERE isbn13 = '978'").fetchone()
        assert row["passed_filter"] == 1
        assert row["source"] == "storygraph"

    def test_first_rating_is_preserved_across_updates(self, conn):
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=4.2,
                 rating_count=600, passed_filter=True)
        log_book(conn, title="Dune", author="Herbert", isbn13="978", rating=4.4,
                 rating_count=3000, passed_filter=True)
        row = conn.execute("SELECT * FROM seen_books WHERE isbn13 = '978'").fetchone()
        assert row["first_rating"] == 4.2
        assert row["first_rating_count"] == 600
        assert row["last_rating"] == 4.4


class TestCatalogQuery:
    def test_returns_google_books_url(self, conn):
        # Regression: the catalog needs this column to build a link for a
        # Google Books row, which has no goodreads_url to fall back on.
        log_book(conn, title="Volume", author="Author", isbn13="978",
                 passed_filter=True, source="google_books",
                 google_books_url="https://books.google.example/v")
        rows = get_all_catalog_books(conn)
        assert rows[0]["google_books_url"] == "https://books.google.example/v"

    def test_excludes_failed_books(self, conn):
        log_book(conn, title="Bad", author="A", isbn13="1", passed_filter=False)
        assert get_all_catalog_books(conn) == []
