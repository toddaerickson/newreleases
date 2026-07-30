"""End-to-end smoke test of run() with every network call stubbed out.

This is the test that earns its keep. The pipeline's failure mode is not a subtly
wrong rating — it is an undefined name, a renamed kwarg, or a log line with the
wrong number of placeholders, none of which show up until the weekly cron fires
against live sites. Exercising run() in-process with fake fetchers catches all
three in under a second.
"""

import logging

import pytest

import main
from scraper import Book


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Run the orchestrator against a temp DB with no network and no email."""
    from db import get_conn

    monkeypatch.setattr(main, "get_conn", lambda: get_conn(tmp_path / "test.db"))
    monkeypatch.setattr("notify.SHORTLISTS_DIR", tmp_path / "shortlists")
    # write_catalog's docs dir is hardcoded to the repo; stub it so the test does
    # not overwrite the published catalog.
    catalog_calls: list[list[dict]] = []
    monkeypatch.setattr(main, "write_catalog",
                        lambda books, docs_dir, **kw: catalog_calls.append(books))

    state: dict = {"goodreads": [], "storygraph": [], "google_books": [],
                   "catalog_calls": catalog_calls}

    monkeypatch.setattr(main, "fetch_new_releases", lambda days: list(state["goodreads"]))
    monkeypatch.setattr(main, "fetch_storygraph_new_releases",
                        lambda days: list(state["storygraph"]))
    monkeypatch.setattr(main, "fetch_google_books_new_releases",
                        lambda days: list(state["google_books"]))
    # Listing pages already carry rating/count in these fixtures, so enrichment
    # is a no-op that returns the book unchanged.
    monkeypatch.setattr(main, "enrich_book", lambda book, force=False: book)
    monkeypatch.setattr(main, "enrich_storygraph_book", lambda book: book)
    # Award sources are live HTTP; stub the scan so the pipeline tests stay offline.
    state["awards"] = []
    state["award_notes"] = ["sfadb 0 blocks -> 0 winners"]
    monkeypatch.setattr(main.awards_mod, "fetch_award_winners",
                        lambda years=None: (list(state["awards"]), state["award_notes"]))
    monkeypatch.setattr(main.awards_mod, "append_ratings",
                        lambda winner, memo=None: winner)
    return state


def run_pipeline(**kwargs) -> int:
    """Run the orchestrator, returning its exit code (0 = clean).

    run() calls sys.exit(1) when a source looks broken, so tests that feed it an
    empty Goodreads fixture must tolerate that rather than treat it as a crash.
    """
    try:
        main.run(recipient="nobody@example.com", skip_email=True, **kwargs)
    except SystemExit as exit_signal:
        return exit_signal.code or 0
    return 0


def test_runs_clean_with_no_candidates(pipeline):
    # An empty Goodreads fetch is treated as breakage, not a quiet week, but the
    # orchestrator must still reach the end rather than raise something else.
    assert run_pipeline() == 1


def test_goodreads_only_run_shortlists_a_passing_book(pipeline, tmp_path):
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
        Book(title="Low Rated", author="B", rating=3.2, rating_count=900, isbn13="979"),
    ]
    run_pipeline(skip_storygraph=True)
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "Good Book" in text
    assert "Low Rated" not in text


def test_google_books_source_is_off_unless_requested(pipeline, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(main, "fetch_google_books_new_releases", called.append)
    assert run_pipeline(skip_storygraph=True) == 1  # empty Goodreads -> alarm
    assert called == []


def test_google_books_run_completes_and_shortlists(pipeline, tmp_path):
    # Regression: this path previously died three times over — NameError on an
    # undefined skip_google_books, then TypeError on write_shortlist(google_books=),
    # then TypeError on send_email(google_books=).
    pipeline["goodreads"] = [
        Book(title="GR Book", author="B", rating=4.5, rating_count=900, isbn13="979"),
    ]
    pipeline["google_books"] = [
        Book(title="SF Volume", author="A", rating=4.5, rating_count=50,
             source="google_books", google_books_url="https://books.google.example/1",
             isbn13="978", genre_tags=["Fiction / Science Fiction"]),
    ]
    assert run_pipeline(skip_storygraph=True, google_books=True) == 0
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "SF Volume" in text
    assert "## From Google Books (1)" in text


def test_prefers_goodreads_when_the_same_book_passes_two_sources(pipeline, tmp_path):
    pipeline["goodreads"] = [
        Book(title="Dune", author="Herbert", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/dune"),
    ]
    pipeline["storygraph"] = [
        Book(title="dune", author="HERBERT", rating=4.5, rating_count=900,
             source="storygraph", storygraph_url="https://storygraph.example/dune"),
    ]
    run_pipeline()
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    # Listed once, under Goodreads.
    assert "## From StoryGraph" not in text
    assert text.count("Dune") == 1


def test_genre_excluded_book_never_reaches_the_shortlist(pipeline, tmp_path):
    pipeline["goodreads"] = [
        Book(title="Romantasy Thing", author="A", rating=4.8, rating_count=9000,
             isbn13="978", genre_tags=["Romantasy", "Fantasy"]),
    ]
    run_pipeline(skip_storygraph=True)
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "Romantasy Thing" not in text


def test_empty_goodreads_fetch_alarms_and_fails_the_run(pipeline, caplog):
    # A scraper broken by a markup change returns zero books and every downstream
    # phase no-ops quietly. Without this the weekly cron stays green forever.
    with caplog.at_level(logging.ERROR):
        assert run_pipeline(skip_storygraph=True) == 1
    assert any("0 candidates" in r.message for r in caplog.records)


def test_alarm_still_delivers_what_other_sources_found(pipeline, tmp_path):
    # Goodreads broken, StoryGraph fine: the run must fail loudly *after* writing
    # the shortlist, so a Goodreads outage does not cost a whole week's feed.
    pipeline["storygraph"] = [
        Book(title="SG Pick", author="A", rating=4.5, rating_count=900,
             source="storygraph", storygraph_url="https://storygraph.example/1"),
    ]
    assert run_pipeline() == 1
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "SG Pick" in text


def test_second_run_dedups_a_previously_shown_book(pipeline, tmp_path):
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
    ]
    run_pipeline(skip_storygraph=True)
    run_pipeline(skip_storygraph=True)
    text = (tmp_path / "shortlists" / "shortlist_"
            f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "Good Book" not in text


def _today_shortlist(tmp_path) -> str:
    from datetime import date
    return (tmp_path / "shortlists" / f"shortlist_{date.today().isoformat()}.md").read_text(
        encoding="utf-8"
    )


def test_a_broken_award_scan_never_breaks_the_feed(pipeline, tmp_path, monkeypatch):
    """The load-bearing invariant: awards are a bonus, the release feed is the product."""
    def explode(years=None):
        raise RuntimeError("sfadb went away")

    monkeypatch.setattr(main.awards_mod, "fetch_award_winners", explode)
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
    ]
    assert run_pipeline(skip_storygraph=True) == 0
    assert "Good Book" in _today_shortlist(tmp_path)


def test_first_award_run_seeds_without_reporting(pipeline, tmp_path):
    """Otherwise the first run dumps a year of stale winners into one email."""
    from awards import AwardWinner
    pipeline["awards"] = [
        AwardWinner(award_name="Nebula Awards", award_year=2026, category="novel",
                    title="The Buffalo Hunter Hunter", author="Stephen Graham Jones"),
    ]
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
    ]
    run_pipeline(skip_storygraph=True)
    text = _today_shortlist(tmp_path)
    assert "Award winners" not in text
    assert "Buffalo" not in text


def test_new_winner_is_reported_once_then_never_again(pipeline, tmp_path):
    from awards import AwardWinner
    seed = AwardWinner(award_name="Locus Awards", award_year=2026,
                       category="sf novel", title="Seed Book", author="Someone")
    pipeline["awards"] = [seed]
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
    ]
    run_pipeline(skip_storygraph=True)  # seeds the ledger

    fresh = AwardWinner(award_name="Nebula Awards", award_year=2026, category="novel",
                        title="The Buffalo Hunter Hunter", author="Stephen Graham Jones")
    pipeline["awards"] = [seed, fresh]
    run_pipeline(skip_storygraph=True)
    assert "The Buffalo Hunter Hunter" in _today_shortlist(tmp_path)

    # Same static source, scanned again: it must not be reported a second time.
    run_pipeline(skip_storygraph=True)
    assert "The Buffalo Hunter Hunter" not in _today_shortlist(tmp_path)


def test_genre_excluded_winner_is_recorded_but_not_shown(pipeline, tmp_path):
    from awards import AwardWinner
    pipeline["awards"] = [
        AwardWinner(award_name="Locus Awards", award_year=2026, category="sf novel",
                    title="Seed", author="S"),
    ]
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900,
             isbn13="978", goodreads_url="https://goodreads.example/1"),
    ]
    run_pipeline(skip_storygraph=True)  # seed

    romance = AwardWinner(award_name="Nebula Awards", award_year=2026, category="novel",
                          title="A Romantic Winner", author="B",
                          genre_tags=["Fantasy", "Romance"])
    pipeline["awards"] = [romance]
    run_pipeline(skip_storygraph=True)
    assert "A Romantic Winner" not in _today_shortlist(tmp_path)


def test_skip_awards_bypasses_the_scan(pipeline, monkeypatch):
    called: list = []
    monkeypatch.setattr(main.awards_mod, "fetch_award_winners",
                        lambda years=None: called.append(1) or ([], []))
    pipeline["goodreads"] = [
        Book(title="Good Book", author="A", rating=4.5, rating_count=900, isbn13="978"),
    ]
    run_pipeline(skip_storygraph=True, skip_awards=True)
    assert called == []
