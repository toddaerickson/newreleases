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
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_title_author
    ON seen_books(LOWER(title), LOWER(author));
"""


def _title_author_hash(title: str, author: str) -> str:
    """Fallback key when ISBN is missing."""
    norm = f"{title.strip().lower()}|{author.strip().lower()}"
    return f"hash:{hashlib.sha256(norm.encode()).hexdigest()[:16]}"


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
    except sqlite3.Error as e:
        conn.close()
        raise RuntimeError(f"Failed to initialize DB at {db_path}") from e
    return conn


def is_seen(conn: sqlite3.Connection, isbn13: str | None, title: str, author: str) -> bool:
    """Return True if this book has already been logged."""
    key = isbn13 or _title_author_hash(title, author)
    row = conn.execute("SELECT 1 FROM seen_books WHERE isbn13 = ?", (key,)).fetchone()
    return row is not None


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
    notes: str | None = None,
) -> None:
    """Insert or update a book in the seen log."""
    key = isbn13 or _title_author_hash(title, author)
    today = date.today().isoformat()

    try:
        existing = conn.execute("SELECT 1 FROM seen_books WHERE isbn13 = ?", (key,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE seen_books
                   SET last_checked_date = ?, last_rating = ?, last_rating_count = ?,
                       passed_filter = ?, genre_tags = COALESCE(?, genre_tags)
                 WHERE isbn13 = ?""",
                (today, rating, rating_count, passed_filter, genre_tags, key),
            )
        else:
            conn.execute(
                """INSERT INTO seen_books
                   (isbn13, goodreads_id, title, author, pub_date,
                    first_seen_date, last_checked_date,
                    first_rating, first_rating_count,
                    last_rating, last_rating_count,
                    passed_filter, genre_tags, goodreads_url, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, goodreads_id, title, author, pub_date,
                    today, today,
                    rating, rating_count,
                    rating, rating_count,
                    passed_filter, genre_tags, goodreads_url, notes,
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
