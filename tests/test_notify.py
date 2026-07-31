"""Output assembly: shortlist sections, catalog links, and cross-source collapse.

No SMTP here — send_email's network half is untestable without a server, but the
kwarg surface it exposes to main.py is exactly what broke, so that is covered.
"""

import inspect
import json

from notify import (
    _outage_banner_html,
    _outage_banner_text,
    _outage_message,
    send_email,
    write_catalog,
    write_shortlist,
)
from scraper import Book, book_link


def gb(**kwargs) -> Book:
    defaults = {"title": "Volume", "author": "Author", "source": "google_books"}
    return Book(**{**defaults, **kwargs})


class TestBookLink:
    def test_link_per_source(self):
        assert book_link(Book(title="T", author="A", goodreads_url="gr")) == "gr"
        assert book_link(
            Book(title="T", author="A", source="storygraph", storygraph_url="sg")
        ) == "sg"
        assert book_link(gb(google_books_url="gbk")) == "gbk"


class TestOutageBanner:
    """A dead source looks exactly like a quiet week, so it has to be stated."""

    def test_no_banner_when_everything_is_up(self):
        assert _outage_message([]) == ""
        assert _outage_banner_html([]) == ""
        assert _outage_banner_text([]) == ""

    def test_singular_verb_for_one_source(self):
        assert "Goodreads returned no results — it is likely down" in _outage_message(["Goodreads"])

    def test_plural_verb_for_two_sources(self):
        message = _outage_message(["Goodreads", "The StoryGraph"])
        assert "Goodreads and The StoryGraph" in message
        assert "they are likely down" in message

    def test_oxford_comma_for_three_or_more(self):
        message = _outage_message(["A", "B", "C"])
        assert "A, B, and C" in message

    def test_html_banner_is_bold_and_red(self):
        banner = _outage_banner_html(["Goodreads"])
        assert "color:#cf222e" in banner
        assert "font-weight:bold" in banner

    def test_html_banner_escapes_its_input(self):
        # Source names are internal constants today, but this renders into an
        # email body — no path from a name to raw markup.
        assert "<script>" not in _outage_banner_html(["<script>x</script>"])

    def test_shortlist_leads_with_the_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist(
            [Book(title="GR", author="A")], down_sources=["The StoryGraph"],
        ).read_text(encoding="utf-8")
        body = text.split("\n")
        warning_line = next(i for i, ln in enumerate(body) if "⚠" in ln)
        first_section = next(i for i, ln in enumerate(body) if ln.startswith("## "))
        assert warning_line < first_section
        assert "The StoryGraph" in text

    def test_shortlist_has_no_warning_when_all_sources_are_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist([Book(title="GR", author="A")]).read_text(encoding="utf-8")
        assert "⚠" not in text


class TestWriteShortlist:
    def test_writes_a_section_per_contributing_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        path = write_shortlist(
            [Book(title="GR Book", author="A", goodreads_url="gr")],
            storygraph_books=[Book(title="SG Book", author="B", source="storygraph")],
            google_books=[gb(title="GB Book")],
        )
        text = path.read_text(encoding="utf-8")
        assert "## From Goodreads (1)" in text
        assert "## From StoryGraph (1)" in text
        assert "## From Google Books (1)" in text

    def test_omits_empty_secondary_sections(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist([Book(title="GR", author="A")]).read_text(encoding="utf-8")
        assert "StoryGraph" not in text
        assert "Google Books" not in text

    def test_empty_week_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist([]).read_text(encoding="utf-8")
        assert "No new books passed the filter this week." in text


class TestAwardSection:
    """Awards follow their own rules, so they must survive an empty book week."""

    def _winner(self, **kwargs):
        from awards import AwardWinner
        defaults = {
            "award_name": "Nebula Awards", "award_year": 2026, "category": "novel",
            "title": "The Buffalo Hunter Hunter", "author": "Stephen Graham Jones",
        }
        return AwardWinner(**{**defaults, **kwargs})

    def test_section_appears_when_no_books_passed(self, tmp_path, monkeypatch):
        """8 of 18 committed shortlists are empty-book weeks — the common case."""
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist([], award_winners=[self._winner()]).read_text(encoding="utf-8")
        assert "## Award winners announced recently (1)" in text
        assert "The Buffalo Hunter Hunter" in text
        assert "Nebula Awards 2026 — novel" in text

    def test_renders_both_ratings(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        w = self._winner(
            goodreads_rating=3.9, goodreads_rating_count=339042,
            goodreads_url="https://gr/x",
            storygraph_rating=3.88, storygraph_rating_count=40944,
            storygraph_url="https://sg/x",
        )
        text = write_shortlist([], award_winners=[w]).read_text(encoding="utf-8")
        assert "3.90 (339,042)" in text
        assert "3.88 (40,944)" in text

    def test_renders_one_rating_and_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        one = self._winner(title="Endling", goodreads_rating=3.94,
                           goodreads_rating_count=5112)
        neither = self._winner(title="Make Your Way Home")
        text = write_shortlist([], award_winners=[one, neither]).read_text(encoding="utf-8")
        assert "3.94 (5,112)" in text
        # A winner with no ratings at all is still listed — the award is the signal.
        assert "Make Your Way Home" in text
        assert "**StoryGraph:** not found" in text

    def test_resolved_book_with_no_rating_still_links(self, tmp_path, monkeypatch):
        """A 403 on the rating fetch must not render "— — <url>"."""
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        w = self._winner(storygraph_url="https://sg/x")  # url resolved, rating did not
        text = write_shortlist([], award_winners=[w]).read_text(encoding="utf-8")
        assert "**StoryGraph:** not found — https://sg/x" in text
        assert "— —" not in text

    def test_no_award_section_when_there_are_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist([Book(title="GR", author="A")]).read_text(encoding="utf-8")
        assert "Award winners" not in text

    def test_source_notes_are_always_written(self, tmp_path, monkeypatch):
        """A healthy counts line every week is what proves the scan still runs."""
        monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path)
        text = write_shortlist(
            [], award_winners=[], award_notes=["sfadb 32 blocks -> 0 winners"],
        ).read_text(encoding="utf-8")
        assert "> Sources: sfadb 32 blocks -> 0 winners" in text


class TestWriteCatalog:
    def _records(self, tmp_path) -> list[dict]:
        return json.loads((tmp_path / "books.json").read_text(encoding="utf-8"))

    def test_labels_and_links_each_source(self, tmp_path):
        write_catalog(
            [
                {"title": "A", "author": "X", "goodreads_url": "gr-url"},
                {"title": "B", "author": "Y", "source": "storygraph",
                 "storygraph_url": "sg-url"},
                {"title": "C", "author": "Z", "source": "google_books",
                 "google_books_url": "gb-url"},
            ],
            tmp_path,
        )
        by_title = {r["title"]: r for r in self._records(tmp_path)}
        assert by_title["A"]["source"] == "Goodreads"
        assert by_title["B"]["source"] == "StoryGraph"
        # Regression: this used to fall back to the (null) goodreads_url and
        # render an unlinked title labelled "Goodreads".
        assert by_title["C"]["source"] == "Google Books"
        assert by_title["C"]["link"] == "gb-url"

    def test_collapses_cross_source_duplicates_preferring_goodreads(self, tmp_path):
        write_catalog(
            [
                {"title": "Dune", "author": "Herbert", "source": "google_books",
                 "google_books_url": "gb-url"},
                {"title": "dune", "author": "HERBERT", "source": "storygraph",
                 "storygraph_url": "sg-url"},
                {"title": "Dune ", "author": "Herbert", "goodreads_url": "gr-url"},
            ],
            tmp_path,
        )
        records = self._records(tmp_path)
        assert len(records) == 1
        assert records[0]["source"] == "Goodreads"

    def test_thresholds_in_the_page_come_from_the_arguments(self, tmp_path):
        # The generated page must state the filter actually applied, not a
        # hardcoded number that drifts when --min-rating changes.
        write_catalog([], tmp_path, min_rating=4.4, min_rating_count=250)
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "≥4.4" in page
        assert "≥250" in page

    def test_survives_a_rating_with_no_count(self, tmp_path):
        # Regression: formatting a None count with :, raised TypeError and killed
        # the run at output time, after all the scraping was already done.
        write_catalog(
            [{"title": "A", "author": "X", "first_rating": 4.2,
              "first_rating_count": None, "last_rating": 4.2,
              "last_rating_count": None}],
            tmp_path,
        )
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "4.20" in page

    def test_computes_rating_delta(self, tmp_path):
        write_catalog(
            [{"title": "A", "author": "X", "first_rating": 4.1, "last_rating": 4.35}],
            tmp_path,
        )
        assert self._records(tmp_path)[0]["delta"] == 0.25


class TestSendEmailSignature:
    def test_accepts_every_kwarg_main_passes(self):
        # main.py calls send_email(..., google_books=..., min_rating_count=...).
        # A missing kwarg here is a TypeError that only surfaces on a live run
        # with mail configured, i.e. once a week in CI.
        params = inspect.signature(send_email).parameters
        for name in ("storygraph_books", "google_books", "min_rating",
                     "min_rating_count", "award_winners", "award_notes",
                     "down_sources"):
            assert name in params, name

    def test_write_shortlist_accepts_every_kwarg_main_passes(self):
        params = inspect.signature(write_shortlist).parameters
        for name in ("storygraph_books", "google_books", "award_winners",
                     "award_notes", "down_sources"):
            assert name in params, name
