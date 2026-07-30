"""Award-source parsers, the rating matcher, and the report-once ledger.

The parsers run against committed fixtures of the real pages, because every
assertion here encodes something that was actually observed and would otherwise
be re-broken silently:

  - sfadb bolds game-writing, screenplay and poetry winners, so the <b> test
    alone is not a book filter.
  - Wikipedia puts rowspan on the category and ref columns as well as the award
    column, and one cell carries rowspan and colspan together.
  - A plain substring match files the Mark Lynton History Prize under "Story
    Prize", because "history prize" contains "story prize".
  - Goodreads autocomplete ranks a SuperSummary study guide and two phantom
    editions above the real book.

Refresh the fixtures with:
    python -c "import requests,pathlib; pathlib.Path('tests/fixtures/sfadb_2026_results.html').write_text(requests.get('https://www.sfadb.com/2026_Results').text, encoding='utf-8')"
    python -c "import requests,json,pathlib; pathlib.Path('tests/fixtures/wikipedia_2026_in_literature.json').write_text(json.dumps(requests.get('https://en.wikipedia.org/w/api.php', params={'action':'parse','page':'2026 in literature','prop':'text','format':'json','formatversion':2}).json()), encoding='utf-8')"
"""

import json
from pathlib import Path

import pytest

from awards import (
    AwardWinner,
    author_matches,
    category_allowed,
    choose_match,
    match_award_name,
    normalize_title,
    parse_sfadb,
    parse_wikipedia,
    years_to_scan,
)
from db import award_seen, count_award_rows, get_conn, log_award_winner

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sfadb_winners():
    html = (FIXTURES / "sfadb_2026_results.html").read_text(encoding="utf-8")
    winners, note = parse_sfadb(html, 2026)
    return winners, note


@pytest.fixture(scope="module")
def wiki_winners():
    payload = json.loads(
        (FIXTURES / "wikipedia_2026_in_literature.json").read_text(encoding="utf-8")
    )
    winners, note = parse_wikipedia(payload, 2026)
    return winners, note


class TestSfadbParser:
    def test_finds_the_nebula_novel_winner(self, sfadb_winners):
        winners, _ = sfadb_winners
        nebula = [w for w in winners
                  if w.award_name == "Nebula Awards" and w.category == "novel"]
        assert len(nebula) == 1
        assert nebula[0].title == "The Buffalo Hunter Hunter"
        assert nebula[0].author == "Stephen Graham Jones"

    def test_unbolded_short_fiction_is_excluded(self, sfadb_winners):
        """Novelettes and short stories use curly quotes and no <b>."""
        winners, _ = sfadb_winners
        titles = {w.title for w in winners}
        assert "Uncertain Sons" not in titles
        assert not any("Laser Eyes" in t for t in titles)

    def test_bolded_non_books_are_excluded_by_category(self, sfadb_winners):
        """The <b> tag is not enough: these winners ARE bolded on the page."""
        winners, _ = sfadb_winners
        titles = {w.title for w in winners}
        assert "Clair Obscur: Expedition 33" not in titles  # game writing
        assert "Sinners" not in titles                       # screenplay
        assert "Everything Endless" not in titles            # poetry
        categories = " ".join(w.category for w in winners).lower()
        for banned in ("game", "screenplay", "poetry", "comic", "graphic", "art book"):
            assert banned not in categories

    def test_not_yet_announced_awards_are_skipped(self, sfadb_winners):
        """The 2026 Hugos render as 'winner(s) to be announced'."""
        winners, _ = sfadb_winners
        assert not any(w.award_name == "Hugo Awards" for w in winners)

    def test_person_awards_produce_nothing(self, sfadb_winners):
        winners, _ = sfadb_winners
        assert not any("Grand Master" in w.award_name for w in winners)
        assert not any(w.author == "" and not w.title for w in winners)

    def test_ya_awards_are_dropped_at_award_level(self, sfadb_winners):
        """Andre Norton is YA and has no category text to filter on."""
        winners, _ = sfadb_winners
        assert "Into the Wild Magic" not in {w.title for w in winners}

    def test_unlabelled_single_winner_keeps_empty_category(self, sfadb_winners):
        winners, _ = sfadb_winners
        pkd = [w for w in winners if w.award_name == "Philip K. Dick Award"]
        assert pkd and pkd[0].category == ""
        assert pkd[0].title == "Outlaw Planet"

    def test_every_winner_has_a_title(self, sfadb_winners):
        winners, _ = sfadb_winners
        assert winners
        assert all(w.title.strip() for w in winners)

    def test_note_reports_block_and_winner_counts(self, sfadb_winners):
        _, note = sfadb_winners
        assert "blocks ->" in note and "winners" in note

    def test_no_bold_titles_is_flagged(self):
        html = """<div class="chronowinsblock"><a href="x">Nebula Awards</a>
                  <ul><li>novel : Some Title, <a href="y">An Author</a></li></ul></div>"""
        winners, note = parse_sfadb(html * 9, 2026)
        assert winners == []


class TestWikipediaParser:
    def test_rowspan_award_is_carried_across_its_rows(self, wiki_winners):
        winners, _ = wiki_winners
        anisfield = [w for w in winners if w.award_name == "Anisfield-Wolf Book Award"]
        # rowspan=4 on the award cell; the Poetry row is dropped by CATEGORY_SKIP.
        assert len(anisfield) == 3
        assert {w.category for w in anisfield} == {
            "Fiction", "Memoir/Autobiography", "Nonfiction"
        }

    def test_colspan_on_award_means_no_category(self, wiki_winners):
        """Aspen Words uses colspan=2, so the category column is not a category."""
        winners, _ = wiki_winners
        aspen = [w for w in winners if w.award_name == "Aspen Words Literary Prize"]
        assert len(aspen) == 1
        assert aspen[0].category == ""
        assert aspen[0].title == "Endling"
        assert aspen[0].author == "Maria Reva"

    def test_empty_title_cell_skips_person_awards(self, wiki_winners):
        winners, _ = wiki_winners
        assert all(w.title.strip() for w in winners)
        # "Author of the Year" and "Illustrator of the Year" have no Title cell.
        assert not any("of the Year" in w.category for w in winners
                       if w.category in ("Author of the Year", "Illustrator of the Year"))

    def test_italic_run_is_preferred_as_the_title(self, wiki_winners):
        """One cell reads '<i>I Regret Almost Everything</i> by Keith McNally'."""
        winners, _ = wiki_winners
        gotham = [w for w in winners if w.award_name == "Gotham Book Prize"]
        assert gotham and gotham[0].title == "I Regret Almost Everything"

    def test_references_are_stripped(self, wiki_winners):
        winners, _ = wiki_winners
        for w in winners:
            assert "[" not in w.title
            assert not w.title.endswith("]")

    def test_quoted_titles_are_short_fiction(self, wiki_winners):
        winners, _ = wiki_winners
        assert not any(w.title.startswith(('"', "“")) for w in winners)

    def test_history_prize_is_not_filed_as_story_prize(self, wiki_winners):
        """'history prize' contains 'story prize' — must match on word boundaries."""
        winners, _ = wiki_winners
        story = [w for w in winners if w.award_name == "Story Prize"]
        assert all("Golden Road" not in w.title for w in story)
        lynton = [w for w in winners if w.award_name == "Mark Lynton History Prize"]
        assert lynton and "Golden Road" in lynton[0].title

    def test_pulitzer_categories_are_kept(self, wiki_winners):
        winners, _ = wiki_winners
        pulitzer = {w.category: w.title for w in winners
                    if w.award_name == "Pulitzer Prize"}
        assert pulitzer.get("Fiction") == "Angel Down"
        assert "History" in pulitzer

    def test_header_shape_change_yields_nothing(self):
        payload = {"parse": {"text": (
            "<table class='wikitable'><tr><th>Prize</th><th>Recipient</th></tr>"
            "<tr><td>Some Award</td><td>Someone</td></tr></table>"
        )}}
        winners, note = parse_wikipedia(payload, 2026)
        assert winners == []
        assert "NOT FOUND" in note

    def test_empty_payload_is_survivable(self):
        assert parse_wikipedia({}, 2026)[0] == []
        assert parse_wikipedia(None, 2026)[0] == []


class TestAwardNameMatching:
    def test_word_boundary_prevents_substring_collisions(self):
        assert match_award_name("Mark Lynton History Prize") == "Mark Lynton History Prize"
        assert match_award_name("Story Prize") == "Story Prize"

    def test_longest_key_wins(self):
        assert match_award_name("International Booker Prize") == "International Booker Prize"
        assert match_award_name("Booker Prize") == "Booker Prize"

    def test_sfadb_bsfa_spelling_matches(self):
        """sfadb prints 'British SF Association Awards', not 'British Science Fiction'."""
        assert match_award_name("British SF Association Awards") == "BSFA Awards"

    def test_unwanted_awards_return_none(self):
        assert match_award_name("Andre Norton Award") is None
        assert match_award_name("Lambda Award") is None
        assert match_award_name("Trillium Book Award") is None
        assert match_award_name("") is None

    def test_genre_and_format_categories_are_skipped(self):
        for bad in ("Best Short Story", "audiobook fiction", "Best Graphic Novel",
                    "young adult novel", "Romance", "poetry", "Best Editor"):
            assert not category_allowed(bad), bad

    def test_novella_and_prose_categories_are_kept(self):
        for good in ("novel", "novella", "first novel", "non-fiction", "", "Fiction"):
            assert category_allowed(good), good


class TestMatcher:
    """The observed Goodreads autocomplete result set for the verified query."""

    CANDIDATES = [
        {"title": "Study Guide: The Heaven & Earth Grocery Store by James McBride",
         "author": "SuperSummary", "rating": 4.18, "rating_count": 51,
         "goodreads_id": "204398799", "goodreads_url": "https://x/1"},
        {"title": "Workbook for The Heaven & Earth Grocery Store by James McBride",
         "author": "Memories Prints", "rating": 0.0, "rating_count": 0,
         "goodreads_id": "203179125", "goodreads_url": "https://x/2"},
        {"title": "The Heaven & Earth Grocery Store - James McBride",
         "author": "James McBride", "rating": 0.0, "rating_count": 0,
         "goodreads_id": "245015376", "goodreads_url": "https://x/3"},
        {"title": "The Heaven & Earth Grocery Store - James McBride",
         "author": "Unknown Author", "rating": 0.0, "rating_count": 0,
         "goodreads_id": "245778189", "goodreads_url": "https://x/4"},
        {"title": "The Heaven & Earth Grocery Store",
         "author": "James   McBride", "rating": 3.9, "rating_count": 339059,
         "goodreads_id": "65678550", "goodreads_url": "https://x/5"},
    ]

    def test_picks_the_real_book_ranked_fifth(self):
        hit = choose_match("The Heaven & Earth Grocery Store", "James McBride",
                           self.CANDIDATES)
        assert hit is not None
        assert hit["goodreads_id"] == "65678550"

    def test_phantom_edition_with_correct_author_is_rejected(self):
        """Candidate 3 has the right title AND author — only the 0.0 rating saves us."""
        hit = choose_match("The Heaven & Earth Grocery Store", "James McBride",
                           [self.CANDIDATES[2]])
        assert hit is None

    def test_study_guide_is_rejected(self):
        hit = choose_match("The Heaven & Earth Grocery Store", "James McBride",
                           [self.CANDIDATES[0]])
        assert hit is None

    def test_missing_author_skips_the_lookup(self):
        assert choose_match("Endling", "", self.CANDIDATES) is None

    def test_no_candidates_is_none(self):
        assert choose_match("Endling", "Maria Reva", []) is None

    def test_middle_name_and_initial_forms_match(self):
        assert author_matches("Stephen Graham Jones", "Stephen G. Jones")
        assert author_matches("James McBride", "James   McBride")
        assert author_matches("André Alexis", "Andre Alexis")

    def test_different_author_does_not_match(self):
        assert not author_matches("James McBride", "SuperSummary")
        assert not author_matches("James McBride", "")

    def test_title_normalisation(self):
        assert normalize_title("The Buffalo Hunter Hunter") == "buffalo hunter hunter"
        assert normalize_title("Endling: A Novel") == "endling"
        assert normalize_title("Heaven & Earth") == normalize_title("Heaven and Earth")
        assert normalize_title("Crown Me Dead (Duet, #1)") == "crown me dead"

    def test_subtitle_only_difference_still_matches(self):
        hit = choose_match("Endling", "Maria Reva", [
            {"title": "Endling: A Novel", "author": "Maria Reva",
             "rating": 3.94, "rating_count": 5112, "goodreads_url": "https://x/9"},
        ])
        assert hit is not None


class TestStoryGraphSearchParse:
    """A search hit repeats its title 2-3 times and has no /authors/ link.

    Getting this wrong yields an empty author, which the matcher (correctly)
    refuses to guess on — so every StoryGraph rating silently comes back blank.
    """

    # staticmethod, not classmethod: a class-scoped fixture declared as an
    # instance method is deprecated and becomes an error in pytest 10, and this
    # one needs no cls — it just parses a fixture file once per class.
    @pytest.fixture(scope="class")
    @staticmethod
    def items():
        from bs4 import BeautifulSoup
        from storygraph import _UUID_RE, _parse_search_item
        html = (FIXTURES / "storygraph_search_buffalo.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        return [
            _parse_search_item(a)
            for a in soup.select("a[href^='/books/']")
            if _UUID_RE.match(a.get("href") or "")
        ]

    def test_title_is_not_repeated(self, items):
        assert items
        assert items[0][0] == "The Buffalo Hunter Hunter"

    def test_author_is_extracted(self, items):
        assert items[0][1] == "Stephen Graham Jones"

    def test_every_hit_has_a_title_and_author(self, items):
        assert all(title and author for title, author in items)

    def test_matcher_picks_the_right_hit(self, items):
        candidates = [{"title": t, "author": a} for t, a in items]
        hit = choose_match("The Buffalo Hunter Hunter", "Stephen Graham Jones", candidates)
        assert hit is not None
        assert hit["title"] == "The Buffalo Hunter Hunter"

    def test_similar_titles_are_not_confused(self, items):
        """'The Buffalo Hunter' and 'The Last Buffalo Hunter' are different books."""
        candidates = [{"title": t, "author": a} for t, a in items]
        assert choose_match("The Buffalo Hunter", "Peter Straub", candidates)["title"] == (
            "The Buffalo Hunter"
        )

    def test_fallback_reads_the_screen_reader_heading(self):
        from bs4 import BeautifulSoup
        from storygraph import _parse_search_item
        soup = BeautifulSoup(
            '<li><h1 class="sr-only">Some Title by Some Author</h1>'
            '<a href="/books/x"><img alt="Some Title by Some Author"></a></li>',
            "html.parser",
        )
        assert _parse_search_item(soup.find("a")) == ("Some Title", "Some Author")


class TestLedger:
    @pytest.fixture
    def conn(self, tmp_path):
        c = get_conn(tmp_path / "awards.db")
        yield c
        c.close()

    def _winner(self, title="The Buffalo Hunter Hunter"):
        return AwardWinner(award_name="Nebula Awards", award_year=2026,
                           category="novel", title=title,
                           author="Stephen Graham Jones")

    def test_key_is_award_year_title(self):
        assert self._winner().award_key == "nebula-awards|2026|the-buffalo-hunter-hunter"

    def test_same_book_different_awards_are_separate_keys(self):
        a = self._winner()
        b = AwardWinner(award_name="Locus Awards", award_year=2026,
                        category="horror novel", title=a.title, author=a.author)
        assert a.award_key != b.award_key

    def test_relisting_is_blocked_and_listed_date_is_stable(self, conn):
        w = self._winner()
        log_award_winner(conn, award_key=w.award_key, award_name=w.award_name,
                         award_year=w.award_year, category=w.category,
                         title=w.title, author=w.author, goodreads_rating=3.9)
        assert award_seen(conn, w.award_key)
        first = conn.execute(
            "SELECT listed_date FROM award_winners WHERE award_key = ?", (w.award_key,)
        ).fetchone()[0]

        # A static source re-serves the same winner on every run of the year.
        log_award_winner(conn, award_key=w.award_key, award_name="CLOBBER",
                         award_year=1999, category="x", title="CLOBBERED", author="")
        row = conn.execute(
            "SELECT title, listed_date, goodreads_rating FROM award_winners "
            "WHERE award_key = ?", (w.award_key,)
        ).fetchone()
        assert row["title"] == w.title
        assert row["listed_date"] == first
        assert row["goodreads_rating"] == 3.9
        assert count_award_rows(conn) == 1

    def test_unseen_key_is_not_seen(self, conn):
        assert not award_seen(conn, "nope|2026|nothing")
        assert count_award_rows(conn) == 0

    def test_award_rows_never_reach_the_catalog(self, conn):
        """The catalog and digest read seen_books only."""
        from db import get_all_catalog_books, get_digest_books
        w = self._winner()
        log_award_winner(conn, award_key=w.award_key, award_name=w.award_name,
                         award_year=w.award_year, category=w.category,
                         title=w.title, author=w.author)
        assert get_all_catalog_books(conn) == []
        assert get_digest_books(conn) == []


class TestYearsToScan:
    def test_only_current_year_after_march(self):
        from datetime import date
        assert years_to_scan(date(2026, 7, 30)) == [2026]

    def test_previous_year_included_early(self):
        from datetime import date
        assert years_to_scan(date(2026, 2, 1)) == [2026, 2025]
