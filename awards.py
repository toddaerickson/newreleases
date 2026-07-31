"""Book award winners: scan announcement sources, append ratings.

This is a *second list*, deliberately separate from the new-release pipeline. An
award is an announcement event, not a release: most prizes honour previous-year
books, so a winner is normally already a ``seen_books`` row (as shown or as
filtered-out) and sits outside the release window. Routing winners through the
book pipeline would silently suppress nearly all of them, so they get their own
table (``award_winners``), their own dataclass, and their own output section.

No rating threshold is applied — the jury is the quality signal. Goodreads and
StoryGraph ratings are appended when they can be resolved and left blank when
they cannot.

Two sources cover ~40 awards in two requests per week:

1. ``sfadb.com/<year>_Results`` — the Science Fiction Awards Database. ~30
   SFF/horror programmes in a very regular shape. Book-length winners are
   wrapped in ``<b>``; short fiction uses curly quotes and no ``<b>``, which
   makes the tag a reliable prose/person discriminator. It is NOT sufficient on
   its own, though: sfadb also bolds game-writing, screenplay, poetry, comic and
   art-book winners, so CATEGORY_SKIP does real work.
2. Wikipedia's ``<year> in literature`` awards table (via the API's rendered
   HTML, which pre-resolves the {{Sortname}} templates that make raw wikitext
   painful) — the literary, crime and nonfiction prizes.

Both sources are incomplete and lag. Measured 2026-07-29: Wikipedia's 2026 table
listed 22 awards and was still missing the March NBCC, April LA Times and
January Carnegie winners. Nothing here can fix that; the ledger means a late
winner is listed whenever it finally appears.
"""

from __future__ import annotations

import difflib
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from scraper import Book, _build_session, enrich_book
from storygraph import BASE_URL as SG_BASE_URL
from storygraph import enrich_storygraph_book, search_storygraph_candidates

logger = logging.getLogger(__name__)

SFADB_URL = "https://www.sfadb.com/{year}_Results"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_PAGE_URL = "https://en.wikipedia.org/wiki/{year}_in_literature"

ALLOWED_HOSTS = {"www.sfadb.com", "sfadb.com", "en.wikipedia.org"}

REQUEST_DELAY = 2.0

# Wall-clock ceiling on rating lookups. The binding constraint is not request
# count but retry latency: storygraph._get is 3 attempts x 25s timeout plus
# 3+6+12s backoff, ~96s per URL when the site times out. A big co-landing week
# with a degraded StoryGraph would otherwise run for hours.
RATING_BUDGET_SECONDS = 15 * 60

# Awards to collect, as case-insensitive substrings of the name the source
# prints, mapped to the display name. Matched longest-key-first, so "booker"
# cannot shadow "international booker".
#
# To add an award: add one line. To drop one: delete the line. The keys below
# were checked against what the two sources actually print on 2026-07-29 --
# sfadb writes "British SF Association Awards" (not "British Science Fiction"),
# and Wikipedia writes "Story Prize" (not "The Story Prize").
AWARD_ALLOWLIST: dict[str, str] = {
    # --- SFF / horror (sfadb) ---
    "hugo": "Hugo Awards",
    "nebula": "Nebula Awards",
    "locus": "Locus Awards",
    "world fantasy": "World Fantasy Awards",
    "bram stoker": "Bram Stoker Awards",
    "shirley jackson": "Shirley Jackson Awards",
    "arthur c. clarke": "Arthur C. Clarke Award",
    "philip k. dick": "Philip K. Dick Award",
    "le guin": "Ursula K. Le Guin Prize",
    "british fantasy": "British Fantasy Awards",
    "british sf association": "BSFA Awards",
    "ignyte": "Ignyte Awards",
    "sidewise": "Sidewise Awards",
    "otherwise": "Otherwise Award",
    "compton crook": "Compton Crook Award",
    "crawford": "Crawford Award",
    "aurealis": "Aurealis Awards",
    "seiun": "Seiun Awards",
    # --- Literary / crime / nonfiction (Wikipedia) ---
    "pulitzer": "Pulitzer Prize",
    "national book award": "National Book Award",
    "international booker": "International Booker Prize",
    "booker": "Booker Prize",
    "national book critics circle": "NBCC Award",
    "pen/faulkner": "PEN/Faulkner Award",
    "pen/hemingway": "PEN/Hemingway Award",
    "los angeles times": "LA Times Book Prize",
    "dublin literary": "Dublin Literary Award",
    "aspen words": "Aspen Words Literary Prize",
    "giller": "Giller Prize",
    "governor general": "Governor General's Award",
    "edgar": "Edgar Awards",
    "anisfield-wolf": "Anisfield-Wolf Book Award",
    "story prize": "Story Prize",
    "british book award": "British Book Awards",
    "carol shields": "Carol Shields Prize",
    "dylan thomas": "Dylan Thomas Prize",
    "gotham book": "Gotham Book Prize",
    "mark lynton": "Mark Lynton History Prize",
    "j. anthony lukas book": "J. Anthony Lukas Book Prize",
    "windham": "Windham-Campbell Prize",
}

# Checked and deliberately left out, so this does not get re-litigated:
#   Women's Prize, Kirkus Prize, Carnegie Medal, Baillie Gifford
#       -- absent from BOTH the 2025 and 2026 Wikipedia tables, so a key for
#          them would never match anything. No usable source found.
#   Andre Norton, Mythopoeic, Lodestar   -- YA / middle-grade.
#   Lambda                              -- has Romance categories.
#   Goodreads Choice                    -- popularity poll, heavy romance.
#   Prometheus                          -- ideological niche, not a genre signal.
#   Rhysling, Chesley, Jack Gaughan     -- poetry / art, not book-length prose.
#   SFWA Grand Master, Hall of Fame, Ray Bradbury, Heinlein, Skylark
#       -- person/lifetime awards. Also auto-skipped: they carry no <b> title on
#          sfadb and no Title cell on Wikipedia.
#   Trillium, Leacock, Edna Staebler, Amazon.ca First Novel, Writers' Trust
#       -- regional Canadian prizes.
#   Bookseller/Diagram Oddest Title     -- a joke award.
#   Costa                               -- discontinued in 2022.

# Category substrings that are never a book worth reading. Case-insensitive.
#
# Load-bearing on BOTH sources, for different reasons:
#   sfadb  -- the <b> test already drops short fiction and person awards, but it
#             still bolds game writing ("Clair Obscur: Expedition 33"), screenplay
#             ("Sinners"), poetry, comic, graphic novel and illustrated art book.
#   Wikipedia -- there is no <b> equivalent, so short-fiction categories have to be
#             named here or Edgar's Best Short Story winner lands in the feed.
# "novella" is deliberately absent -- novellas ship as standalone books.
CATEGORY_SKIP: tuple[str, ...] = (
    "short story", "short fiction", "novelette", "audiobook", "audio book",
    "game", "screenplay", "dramatic", "comic", "graphic", "art book", "illustrat",
    "poetry", "poem", "verse", "drama", "young adult", "middle grade", "juvenile",
    "children", "picture book", "younger reader", "series", "editor", "artist",
    "magazine", "periodical", "publisher", "fanzine", "fancast", "podcast",
    "semiprozine", "grand master", "hall of fame", "lifetime", "special award",
    "special citation", "service", "translator", "bookseller", "blog", "cover",
    "narrat",
    # The user's genre exclusions, applied before any network call. Kept in step
    # with filters.EXCLUDED_GENRES, which gates again on the resolved genres.
    "romance", "romantic", "romantasy", "rom com", "erotica",
)

# Substrings that mark a study guide / summary rather than the book itself.
JUNK_MARKERS: tuple[str, ...] = (
    "study guide", "workbook", "summary of", "sparknotes", "cliffsnotes",
    "conversation starters",
)

TITLE_SIMILARITY_FLOOR = 0.82

# Longest key first, so "booker" cannot shadow "international booker". Each key is
# matched on word boundaries: a plain substring test makes "story prize" match
# inside "Mark Lynton History Prize", which silently files a history winner under
# the wrong award.
_AWARD_MATCHERS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{re.escape(key)}", re.I), AWARD_ALLOWLIST[key])
    for key in sorted(AWARD_ALLOWLIST, key=len, reverse=True)
]


@dataclass
class AwardWinner:
    """One award-winning book. Not a Book: it carries two ratings side by side."""

    award_name: str
    award_year: int
    category: str
    title: str
    author: str = ""
    goodreads_url: str | None = None
    goodreads_rating: float | None = None
    goodreads_rating_count: int | None = None
    storygraph_url: str | None = None
    storygraph_rating: float | None = None
    storygraph_rating_count: int | None = None
    genre_tags: list[str] = field(default_factory=list)

    @property
    def award_key(self) -> str:
        """Stable identity: award + year + title.

        No source component, so a winner listed by both sources collides
        harmlessly instead of being reported twice. The title is part of the key
        because a category is not unique within an award (Bram Stoker 2026 has
        two "long fiction" rows) and ties/co-winners are real.

        Must be computed from the SOURCE title and never recomputed after
        enrichment: _enrich_from_apollo rewrites Book.title to the fuller
        titleComplete form ("Endling" -> "Endling: A Novel"), which would mint a
        new key and re-list the winner every week.
        """
        return f"{_slug(self.award_name)}|{self.award_year}|{_slug(self.title)}"

    @property
    def award_label(self) -> str:
        """e.g. 'Nebula Awards 2026 — novel'."""
        base = f"{self.award_name} {self.award_year}"
        return f"{base} — {self.category}" if self.category else base


# --- text helpers ------------------------------------------------------------


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize_title(title: str) -> str:
    """Fold a title for comparison: accents, quotes, ampersands, subtitles, articles."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = text.lower().replace("&", " and ")
    # Drop subtitles and series/edition suffixes: "Endling: A Novel",
    # "The Buffalo Hunter Hunter (Signed Edition)".
    text = re.split(r"[:(\[]", text, maxsplit=1)[0]
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_author(author: str) -> str:
    text = unicodedata.normalize("NFKD", author or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def author_matches(want: str, candidate: str) -> bool:
    """Surname containment.

    Rejects "SuperSummary" for a James McBride book while accepting
    "Stephen G. Jones" for "Stephen Graham Jones" and "James   McBride" (the
    doubled internal spaces Goodreads really returns).
    """
    want_norm, cand_norm = normalize_author(want), normalize_author(candidate)
    if not want_norm or not cand_norm:
        return False
    surname = want_norm.split()[-1]
    if len(surname) < 2:
        return False
    return surname in cand_norm.split()


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_junk(candidate_title: str, want_title: str) -> bool:
    cand_low, want_low = candidate_title.lower(), want_title.lower()
    return any(m in cand_low and m not in want_low for m in JUNK_MARKERS)


def match_award_name(raw_name: str) -> str | None:
    """Map a printed award name onto its display name, or None if not wanted."""
    if not raw_name:
        return None
    for pattern, display in _AWARD_MATCHERS:
        if pattern.search(raw_name):
            return display
    return None


def category_allowed(category: str) -> bool:
    low = (category or "").lower()
    return not any(skip in low for skip in CATEGORY_SKIP)


def choose_match(want_title: str, want_author: str, candidates: list[dict]) -> dict | None:
    """Pick the candidate that is really this book, or None.

    Three gates, each earning its place against the observed junk for
    "The Heaven & Earth Grocery Store James McBride", where the real book ranked
    fifth behind a SuperSummary study guide, a workbook and two phantom editions:

    1. zero rating   -- phantom editions report exactly 0. Essential on its own:
                        one phantom carried the correct author, so the author
                        gate alone would have let it through.
    2. author match  -- kills the study guide and workbook, whose "authors" are
                        the summary mills.
    3. title floor   -- a sanity check on what survives.

    Prefers leaving a rating blank over showing the wrong book's rating.
    """
    if not candidates:
        return None
    if not want_author.strip():
        # Both sources normally supply an author; without one there is no strong
        # guard left, and a wrong rating is worse than a blank one.
        logger.debug("No author for %r — skipping rating lookup", want_title)
        return None

    want_norm = normalize_title(want_title)
    survivors: list[tuple[float, int, dict]] = []
    for cand in candidates:
        cand_title = cand.get("title") or ""
        if _is_junk(cand_title, want_title):
            logger.debug("Rejected %r: junk marker", cand_title)
            continue
        rating = cand.get("rating")
        if rating is not None and rating <= 0:
            logger.debug("Rejected %r: zero rating (phantom edition)", cand_title)
            continue
        if cand.get("rating_count") == 0:
            logger.debug("Rejected %r: zero ratings count", cand_title)
            continue
        if not author_matches(want_author, cand.get("author") or ""):
            logger.debug("Rejected %r: author %r != %r",
                         cand_title, cand.get("author"), want_author)
            continue
        score = title_similarity(want_norm, normalize_title(cand_title))
        if score < TITLE_SIMILARITY_FLOOR:
            logger.debug("Rejected %r: title similarity %.2f", cand_title, score)
            continue
        survivors.append((score, cand.get("rating_count") or 0, cand))

    if not survivors:
        logger.info("No Goodreads/StoryGraph match for %r by %r", want_title, want_author)
        return None
    survivors.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return survivors[0][2]


# --- HTTP --------------------------------------------------------------------

_session = _build_session()
# scraper's HEADERS set a Goodreads Referer and a browser UA; neither is right
# for these hosts, and the Wikipedia API asks callers to identify themselves.
_session.headers["User-Agent"] = (
    "NewReleaseBookFilter/1.0 (+https://github.com/toddaerickson/newreleases)"
)
_session.headers.pop("Referer", None)


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc in ALLOWED_HOSTS


def _polite_sleep() -> None:
    time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))


def _get(url: str, params: dict | None = None) -> requests.Response | None:
    if not _is_allowed_url(url):
        logger.warning("Refusing to fetch non-award-source URL: %s", url)
        return None
    try:
        resp = _session.get(url, params=params, timeout=(5, 20))
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


# --- sfadb -------------------------------------------------------------------


def parse_sfadb(html: str, year: int) -> tuple[list[AwardWinner], str]:
    """Parse an sfadb <year>_Results page. Pure: no network.

    Shape (verified): one div.chronowinsblock per award, award name in the
    block's first <a>, one <li> per category holding
    ``category : <b>Title</b>, <a>Author</a>``.
    """
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select("div.chronowinsblock")
    winners: list[AwardWinner] = []
    bold_titles = 0

    for block in blocks:
        name_el = block.find("a")
        if not name_el:
            continue
        raw_name = name_el.get_text(" ", strip=True)
        display = match_award_name(raw_name)
        block_text = block.get_text(" ", strip=True).lower()
        if "to be announced" in block_text:
            continue

        for li in block.select("li"):
            title_el = li.find("b")
            if not title_el:
                # Short fiction (curly quotes) and person awards are unbolded.
                continue
            bold_titles += 1
            if not display:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title:
                continue
            raw = li.get_text(" ", strip=True)
            # Category is the text before the <b>, not raw.partition(":") --
            # titles contain colons, and unlabelled single-winner blocks
            # (Andre Norton, Otherwise) have no colon at all.
            idx = raw.find(title)
            category = raw[:idx].strip().rstrip(":,").strip() if idx > 0 else ""
            category = re.sub(r"\s+", " ", category)
            if len(category) > 60:
                category = ""
            if not category_allowed(category):
                continue
            author = ""
            for anchor in li.find_all("a"):
                text = anchor.get_text(" ", strip=True)
                if text and text != title:
                    author = text
                    break
            winners.append(AwardWinner(
                award_name=display,
                award_year=year,
                category=category,
                title=title,
                author=author,
            ))

    note = f"sfadb {len(blocks)} blocks -> {len(winners)} winners"
    if blocks and not bold_titles:
        note += " (no bolded titles — markup may have changed)"
        logger.warning("sfadb %d: %d blocks but zero bolded titles — check markup", year, len(blocks))
    return winners, note


def scan_sfadb(year: int) -> tuple[list[AwardWinner], str]:
    url = SFADB_URL.format(year=year)
    resp = _get(url)
    _polite_sleep()
    if not resp:
        return [], f"sfadb {year} fetch FAILED"
    return parse_sfadb(resp.text, year)


# --- Wikipedia ---------------------------------------------------------------


@dataclass
class Cell:
    """One resolved grid position."""

    text: str
    italic: str = ""  # Wikipedia italicises book titles; "" when there is no <i>
    from_colspan: bool = False


def _expand_table(table, ncols: int) -> list[list[Cell]]:
    """Expand a wikitable into a rectangular ncols-wide grid, resolving spans.

    A general expander is required rather than a positional heuristic: on the 2026
    page rowspan appears on the award column, the category column AND the ref
    column, one cell carries rowspan=2 together with colspan=2, and rows hold 3, 4
    or 5 cells depending on what is being carried into them.

    `from_colspan` marks positions that exist only because a neighbour spanned
    into them, which is how "the award cell spans the category column" (so there
    is no category) is told apart from a real category value.
    """
    rows = table.find_all("tr", recursive=False)
    if not rows:
        body = table.find("tbody")
        rows = body.find_all("tr", recursive=False) if body else []

    grid: list[list[Cell]] = []
    carry: dict[int, list] = {}  # col -> [Cell, rows_remaining]

    for tr in rows:
        cell_queue = tr.find_all(["td", "th"], recursive=False)
        queue_pos = 0
        row: list[Cell] = []
        col = 0
        while col < ncols:
            active = carry.get(col)
            if active and active[1] > 0:
                row.append(active[0])
                active[1] -= 1
                col += 1
                continue
            if queue_pos >= len(cell_queue):
                row.append(Cell(""))
                col += 1
                continue
            cell = cell_queue[queue_pos]
            queue_pos += 1
            text = cell.get_text(" ", strip=True)
            italic_el = cell.find("i")
            italic = italic_el.get_text(" ", strip=True) if italic_el else ""
            try:
                rowspan = max(1, int(cell.get("rowspan") or 1))
            except (TypeError, ValueError):
                rowspan = 1
            try:
                colspan = max(1, int(cell.get("colspan") or 1))
            except (TypeError, ValueError):
                colspan = 1
            for offset in range(colspan):
                if col >= ncols:
                    break
                resolved = Cell(text, italic, from_colspan=offset > 0)
                row.append(resolved)
                if rowspan > 1:
                    carry[col] = [resolved, rowspan - 1]
                col += 1
        grid.append(row[:ncols])

    return grid


def parse_wikipedia(payload: dict, year: int) -> tuple[list[AwardWinner], str]:
    """Parse the '<year> in literature' awards table from rendered HTML. Pure."""
    html = (payload or {}).get("parse", {}).get("text", "")
    if not html:
        return [], f"Wikipedia {year} EMPTY response"

    soup = BeautifulSoup(html, "lxml")
    for sup in soup.select("sup"):
        sup.decompose()

    table = None
    header: list[str] = []
    for candidate in soup.select("table.wikitable"):
        rows = candidate.find_all("tr")
        if not rows:
            continue
        cols = [th.get_text(" ", strip=True).lower() for th in rows[0].find_all("th")]
        if "award" in cols and "title" in cols:
            table, header = candidate, cols
            break

    if table is None:
        logger.warning("Wikipedia %d: no awards table with an Award/Title header", year)
        return [], f"Wikipedia {year} table NOT FOUND"

    # Column positions by name, so an inserted or reordered column is absorbed.
    idx_award = header.index("award")
    idx_title = header.index("title")
    idx_category = header.index("category") if "category" in header else None
    idx_author = header.index("author") if "author" in header else None

    grid = _expand_table(table, len(header))
    winners: list[AwardWinner] = []
    data_rows = 0
    blank = Cell("")

    for row in grid[1:]:
        if not row:
            continue
        data_rows += 1

        def cell(index: int | None, current_row: list[Cell] = row) -> Cell:
            if index is None or index >= len(current_row):
                return blank
            return current_row[index]

        title_cell = cell(idx_title)
        # Wikipedia italicises book titles, and some cells carry trailing prose
        # ("<i>I Regret Almost Everything</i> by Keith McNally"), so the italic
        # run is the title when there is one.
        title = "" if title_cell.from_colspan else (title_cell.italic or title_cell.text)
        if not title.strip():
            # Person award (Author of the Year, Astrid Lindgren, Nobel): one rule
            # instead of a list of award names to exclude.
            continue
        if title.strip().startswith(('"', "“")):
            continue  # quoted title = short fiction, not a book
        display = match_award_name(cell(idx_award).text)
        if not display:
            continue
        category_cell = cell(idx_category)
        # from_colspan here means the award cell spanned across the category
        # column, i.e. this award has no categories at all.
        category = "" if category_cell.from_colspan else category_cell.text
        author_cell = cell(idx_author)
        author = "" if author_cell.from_colspan else author_cell.text
        if not category_allowed(category):
            continue
        winners.append(AwardWinner(
            award_name=display,
            award_year=year,
            category=category.strip(),
            title=title.strip(),
            author=author.strip(),
        ))

    return winners, f"Wikipedia {data_rows} rows -> {len(winners)} winners"


def scan_wikipedia(year: int) -> tuple[list[AwardWinner], str]:
    resp = _get(WIKIPEDIA_API, params={
        "action": "parse",
        "page": f"{year} in literature",
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    })
    _polite_sleep()
    if not resp:
        return [], f"Wikipedia {year} fetch FAILED"
    try:
        payload = resp.json()
    except ValueError:
        logger.warning("Wikipedia %d: response was not JSON", year)
        return [], f"Wikipedia {year} bad JSON"
    return parse_wikipedia(payload, year)


def years_to_scan(today: date | None = None) -> list[int]:
    """Current year, plus the previous one early in the year.

    Awards announced in November/December would otherwise be missed forever once
    the calendar rolls over, since the new year's pages start nearly empty.
    """
    today = today or date.today()
    return [today.year] if today.month > 3 else [today.year, today.year - 1]


def fetch_award_winners(years: list[int] | None = None) -> tuple[list[AwardWinner], list[str]]:
    """Scan every source for every year. Never raises: a dead source yields a note.

    Winners are de-duplicated on award_key, so a book honoured by both sources
    (or listed twice on one page) is returned once.
    """
    years = years or years_to_scan()
    winners: list[AwardWinner] = []
    notes: list[str] = []
    seen: set[str] = set()

    for year in years:
        for label, scan in (("sfadb", scan_sfadb), ("Wikipedia", scan_wikipedia)):
            try:
                found, note = scan(year)
            except Exception as e:  # a broken source must not stop the other
                logger.error("%s %d scan crashed: %s", label, year, e)
                notes.append(f"{label} {year} scan crashed: {e}")
                continue
            notes.append(note)
            for winner in found:
                if winner.award_key in seen:
                    continue
                seen.add(winner.award_key)
                winners.append(winner)

    return winners, notes


# --- rating lookup -----------------------------------------------------------


def _lookup_goodreads(winner: AwardWinner) -> None:
    from scraper import search_goodreads_candidates

    hit = choose_match(winner.title, winner.author, search_goodreads_candidates(
        winner.title, winner.author))
    if not hit:
        return
    book = Book(
        title=winner.title,
        author=winner.author,
        goodreads_id=hit.get("goodreads_id"),
        goodreads_url=hit.get("goodreads_url"),
    )
    # force=True: the listing gave us no rating, and _enrich_from_apollo needs the
    # detail page. goodreads_id must already be set -- it matches on legacyId, and
    # a page carrying several editions otherwise yields no match at all.
    enrich_book(book, force=True)
    winner.goodreads_url = book.goodreads_url
    winner.goodreads_rating = book.rating
    winner.goodreads_rating_count = book.rating_count
    if book.genre_tags:
        winner.genre_tags = book.genre_tags


def _lookup_storygraph(winner: AwardWinner) -> None:
    hit = choose_match(winner.title, winner.author,
                       search_storygraph_candidates(winner.title))
    if not hit:
        return
    book = Book(
        title=winner.title,
        author=winner.author,
        source="storygraph",
        storygraph_id=hit.get("storygraph_id"),
        storygraph_url=hit.get("storygraph_url"),
    )
    # storygraph_id, not just the url: enrich_storygraph_book opens with
    # `if not book.storygraph_id: return book` and would silently no-op.
    enrich_storygraph_book(book)
    winner.storygraph_url = book.storygraph_url or f"{SG_BASE_URL}/books/{book.storygraph_id}"
    winner.storygraph_rating = book.rating
    winner.storygraph_rating_count = book.rating_count


def append_ratings(winner: AwardWinner, memo: dict | None = None) -> AwardWinner:
    """Fill in Goodreads and StoryGraph ratings, best-effort.

    Each side is isolated: a failure leaves that rating blank rather than losing
    the winner. `memo` caches by normalised title+author within a run, which
    matters because one book routinely wins several prizes in the same week --
    The Buffalo Hunter Hunter took the 2026 Nebula, Bram Stoker and Locus horror
    novel awards, appearing three times on one sfadb page.
    """
    key = (normalize_title(winner.title), normalize_author(winner.author))
    if memo is not None and key in memo:
        cached = memo[key]
        winner.goodreads_url = cached.goodreads_url
        winner.goodreads_rating = cached.goodreads_rating
        winner.goodreads_rating_count = cached.goodreads_rating_count
        winner.storygraph_url = cached.storygraph_url
        winner.storygraph_rating = cached.storygraph_rating
        winner.storygraph_rating_count = cached.storygraph_rating_count
        winner.genre_tags = list(cached.genre_tags)
        logger.debug("Reused in-run rating lookup for %r", winner.title)
        return winner

    try:
        _lookup_goodreads(winner)
    except Exception as e:
        logger.error("Goodreads rating lookup failed for %r: %s", winner.title, e)
    try:
        _lookup_storygraph(winner)
    except Exception as e:
        logger.error("StoryGraph rating lookup failed for %r: %s", winner.title, e)

    if memo is not None:
        memo[key] = winner
    return winner
