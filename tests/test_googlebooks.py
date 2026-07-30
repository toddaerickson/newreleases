"""Google Books response parsing, against a fixed fake payload.

Unlike the HTML scrapers, the volumes API is a documented JSON contract, so
pinning the parse is worthwhile: these assertions stay meaningful over time.
"""

from datetime import date, timedelta

import pytest

import googlebooks


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def fake_api(monkeypatch):
    """Serve a canned volumes payload instead of calling Google."""
    def install(items):
        monkeypatch.setattr(
            googlebooks.requests, "get",
            lambda *a, **kw: FakeResponse({"items": items}),
        )
    return install


def volume(**info) -> dict:
    base = {
        "title": "A Novel",
        "authors": ["An Author"],
        "publishedDate": (date.today() - timedelta(days=10)).isoformat(),
        "categories": ["Fiction / Science Fiction / General"],
    }
    return {"id": info.pop("id", "vol-1"), "volumeInfo": {**base, **info}}


class TestCategoryFiltering:
    def test_keeps_bisac_style_category_paths(self, fake_api):
        # Regression: an exact set-membership test rejected every categorised
        # volume, because Google returns full BISAC paths, not bare labels.
        fake_api([volume(categories=["Fiction / Science Fiction / Space Opera"])])
        assert len(googlebooks.fetch_google_books_new_releases()) == 1

    def test_keeps_uncategorised_volumes(self, fake_api):
        fake_api([volume(categories=[])])
        assert len(googlebooks.fetch_google_books_new_releases()) == 1

    def test_drops_off_topic_categories(self, fake_api):
        fake_api([volume(categories=["Business & Economics / Management"])])
        assert googlebooks.fetch_google_books_new_releases() == []


class TestWindowFiltering:
    def test_drops_volumes_older_than_the_window(self, fake_api):
        fake_api([volume(publishedDate=(date.today() - timedelta(days=200)).isoformat())])
        assert googlebooks.fetch_google_books_new_releases(window_days=90) == []

    def test_drops_future_dated_volumes(self, fake_api):
        fake_api([volume(publishedDate=(date.today() + timedelta(days=30)).isoformat())])
        assert googlebooks.fetch_google_books_new_releases() == []

    def test_accepts_year_only_dates_by_truncation(self, fake_api):
        # Google often returns just "2026"; date.fromisoformat("2026"[:10]) fails,
        # so such volumes are skipped rather than crashing the fetch.
        fake_api([volume(publishedDate="2026")])
        assert googlebooks.fetch_google_books_new_releases() == []


class TestRecordShape:
    def test_extracts_isbn13_and_link(self, fake_api):
        fake_api([volume(
            industryIdentifiers=[
                {"type": "ISBN_10", "identifier": "0441013590"},
                {"type": "ISBN_13", "identifier": "9780441013593"},
            ],
            infoLink="https://books.google.example/v",
            averageRating=4.5,
            ratingsCount=42,
        )])
        book = googlebooks.fetch_google_books_new_releases()[0]
        assert book.isbn13 == "9780441013593"
        assert book.google_books_url == "https://books.google.example/v"
        assert book.source == "google_books"
        assert book.rating == 4.5
        assert book.rating_count == 42

    def test_missing_isbn_is_none_not_a_crash(self, fake_api):
        fake_api([volume(industryIdentifiers=[])])
        assert googlebooks.fetch_google_books_new_releases()[0].isbn13 is None

    def test_skips_volumes_without_a_title_or_author(self, fake_api):
        fake_api([volume(id="a", title=""), volume(id="b", authors=[])])
        assert googlebooks.fetch_google_books_new_releases() == []

    def test_dedups_repeated_volume_ids(self, fake_api):
        fake_api([volume(id="same"), volume(id="same")])
        assert len(googlebooks.fetch_google_books_new_releases()) == 1


class TestFailureHandling:
    def test_request_failure_returns_empty_list(self, monkeypatch):
        def boom(*a, **kw):
            raise googlebooks.requests.RequestException("network down")
        monkeypatch.setattr(googlebooks.requests, "get", boom)
        # Best-effort: a dead API must not take down the whole weekly run.
        assert googlebooks.fetch_google_books_new_releases() == []

    def test_malformed_json_returns_empty_list(self, monkeypatch):
        class BadResponse:
            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(googlebooks.requests, "get", lambda *a, **kw: BadResponse())
        assert googlebooks.fetch_google_books_new_releases() == []
