"""SQLite persistence for seen books and deduplication."""

import hashlib
import logging
import sqlite3
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "seen_books.db"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS seen_books (
    isbn13          TEXT PRIMARY KEY,
    goodreads_id    TEXT,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    pub_date        DATE,
    first_seen_date DATE NOT NULL,
    last_checked_date DATE,
    first_rating    REAL,
    first_rating_count INTEGER,
    last_rating     REAL,
    last_rating_count INTEGER,
    passed_filter   BOOLEAN,
    genre_tags      TEXT,
    goodreads_url   TEXT,
    description     TEXT,
    source          TEXT DEFAULT 'goodreads',
    storygraph_url  TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_title_author
    ON seen_books(LOWER(title), LOWER(author));
"""

# Migrations for existing databases (safe to re-run; errors are swallowed)
MIGRATIONS = [
    "ALTER TABLE seen_books ADD COLUMN description TEXT",
    "ALTER TABLE seen_books ADD COLUMN source TEXT DEFAULT 'goodreads'",
    "ALTER TABLE seen_books ADD COLUMN storygraph_url TEXT",
]


def _title_author_hash(title: str, author: str) -> str:
    """Fallback key when ISBN is missing."""
    norm = f"{title.strip().lower()}|{author.strip().lower()}"
    return f"hash:{hashlib.sha256(norm.encode()).hexdigest()[:16]}"


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Use individual execute() calls instead of executescript() to avoid
        # implicit COMMIT behavior that executescript() triggers.
        for statement in SCHEMA.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        conn.commit()
        # Apply migrations; ignore errors (column may already exist)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass
    except sqlite3.Error as e:
        conn.close()
        raise RuntimeError(f"Failed to initialize DB at {db_path}") from e
    return conn


def is_seen(conn: sqlite3.Connection, isbn13: str | None, title: str, author: str) -> bool:
    """Return True if this book has already been logged."""
    key = isbn13 or _title_author_hash(title, author)
    row = conn.execute("SELECT 1 FROM seen_books WHERE isbn13 = ?", (key,)).fetchone()
    return row is not None


def is_seen_by_name(conn: sqlite3.Connection, title: str, author: str) -> bool:
    """Return True if any logged book has this title+author (case-insensitive).

    Cross-source dedup: the ISBN/hash key in ``is_seen`` cannot catch a
    StoryGraph book that duplicates a Goodreads book stored under a real ISBN,
    because the two sources give the same book different ISBNs. Matching on
    normalized title+author (via the existing idx_title_author index) does.
    """
    row = conn.execute(
        "SELECT 1 FROM seen_books WHERE LOWER(title) = LOWER(?) AND LOWER(author) = LOWER(?)",
        (title.strip(), author.strip()),
    ).fetchone()
    return row is not None


def get_seen_rows_by_name(conn: sqlite3.Connection, title: str, author: str) -> list[dict]:
    """Return (passed_filter, genre_tags) for every logged row with this
    title+author (case-insensitive). Used for union-aware StoryGraph dedup:
    a StoryGraph pick is suppressed only if the title was already SHOWN
    (passed_filter=1) or is genre-excluded — not merely because a Goodreads
    twin failed its rating bar."""
    rows = conn.execute(
        """SELECT passed_filter, genre_tags FROM seen_books
           WHERE LOWER(title) = LOWER(?) AND LOWER(author) = LOWER(?)""",
        (title.strip(), author.strip()),
    ).fetchall()
    return [dict(r) for r in rows]


def log_book(
    conn: sqlite3.Connection,
    *,
    title: str,
    author: str,
    isbn13: str | None = None,
    goodreads_id: str | None = None,
    pub_date: str | None = None,
    rating: float | None = None,
    rating_count: int | None = None,
    passed_filter: bool = False,
    genre_tags: str | None = None,
    goodreads_url: str | None = None,
    description: str | None = None,
    source: str = "goodreads",
    storygraph_url: str | None = None,
    notes: str | None = None,
) -> None:
    """Insert or update a book in the seen log."""
    # Normalize on write so is_seen_by_name's LOWER(title)=LOWER(?) comparison
    # (which strips its query args) matches consistently.
    title = title.strip()
    author = author.strip()
    key = isbn13 or _title_author_hash(title, author)
    today = date.today().isoformat()

    try:
        existing = conn.execute(
            "SELECT source, passed_filter FROM seen_books WHERE isbn13 = ?", (key,)
        ).fetchone()
        # Never let a StoryGraph write clobber a Goodreads row that is currently
        # SHOWN (passed_filter=1) and shares this ISBN/hash key — that would flip
        # it out of the catalog and corrupt its rating history. A StoryGraph write
        # over a *failed* Goodreads row (passed_filter=0) is allowed, so the union
        # can surface a book Goodreads rated below its bar. Goodreads writes always
        # win over StoryGraph rows.
        if (existing and source == "storygraph"
                and (existing["source"] or "goodreads") == "goodreads"
                and existing["passed_filter"]):
            logger.info(
                "Skipping StoryGraph log for %r — key owned by a passing Goodreads row", title
            )
            return
        if existing:
            conn.execute(
                """UPDATE seen_books
                   SET last_checked_date = ?, last_rating = ?, last_rating_count = ?,
                       passed_filter = ?, genre_tags = COALESCE(?, genre_tags),
                       description = COALESCE(?, description),
                       source = COALESCE(?, source),
                       storygraph_url = COALESCE(?, storygraph_url)
                 WHERE isbn13 = ?""",
                (today, rating, rating_count, passed_filter, genre_tags, description,
                 source, storygraph_url, key),
            )
        else:
            conn.execute(
                """INSERT INTO seen_books
                   (isbn13, goodreads_id, title, author, pub_date,
                    first_seen_date, last_checked_date,
                    first_rating, first_rating_count,
                    last_rating, last_rating_count,
                    passed_filter, genre_tags, goodreads_url, description,
                    source, storygraph_url, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, goodreads_id, title, author, pub_date,
                    today, today,
                    rating, rating_count,
                    rating, rating_count,
                    passed_filter, genre_tags, goodreads_url, description,
                    source, storygraph_url, notes,
                ),
            )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error("Failed to log book %r (key=%s): %s", title, key, e)


def get_books_for_recheck(conn: sqlite3.Connection, days_since_last_check: int = 90) -> list[dict]:
    """Return previously-failed books that haven't been checked recently."""
    rows = conn.execute(
        """SELECT isbn13, title, author, goodreads_url, last_rating, last_rating_count
           FROM seen_books
           WHERE passed_filter = 0
             AND (last_checked_date IS NULL
                  OR julianday('now') - julianday(last_checked_date) >= ?)""",
        (days_since_last_check,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_books_missing_genres(conn: sqlite3.Connection) -> list[dict]:
    """Return passed books that have no genre_tags stored, for backfill."""
    rows = conn.execute(
        """SELECT isbn13, title, author, goodreads_url, goodreads_id
           FROM seen_books
           WHERE passed_filter = 1
             AND (genre_tags IS NULL OR genre_tags = '')
             AND goodreads_url IS NOT NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_catalog_books(conn: sqlite3.Connection) -> list[dict]:
    """Return all books that ever passed the filter, newest first."""
    rows = conn.execute(
        """SELECT isbn13, title, author, pub_date,
                  first_seen_date,
                  first_rating, first_rating_count,
                  last_rating, last_rating_count,
                  genre_tags, goodreads_url, description,
                  source, storygraph_url
           FROM seen_books
           WHERE passed_filter = 1
           ORDER BY first_seen_date DESC, title ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_digest_books(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return books that passed the filter within the last `days` days."""
    rows = conn.execute(
        """SELECT isbn13, goodreads_id, title, author, pub_date,
                  first_seen_date, last_rating AS rating,
                  last_rating_count AS rating_count,
                  genre_tags, goodreads_url,
                  source, storygraph_url
           FROM seen_books
           WHERE passed_filter = 1
             AND first_seen_date >= date('now', ? || ' days')
           ORDER BY first_seen_date DESC, title ASC""",
        (f"-{days}",),
    ).fetchall()
    return [dict(r) for r in rows]
