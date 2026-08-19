"""
PostgreSQL (Supabase) database layer for Sourcing Africa.
"""

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pg_pool

# ThreadedConnectionPool, not SimpleConnectionPool: FastAPI runs sync endpoints
# in a threadpool and we also spawn background threads (the scheduler, /api/sync),
# so the pool is genuinely reached from more than one thread.
_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check: another thread may have won the race
                dsn = os.environ.get("DATABASE_URL")
                if not dsn:
                    raise RuntimeError(
                        "DATABASE_URL is not set. On Railway use the Supabase "
                        "connection-pooler string (IPv4), not the direct db host."
                    )
                _pool = pg_pool.ThreadedConnectionPool(1, 10, dsn)
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    # Supabase's pooler closes idle connections, so a handle from the pool may be
    # dead. Validate it and transparently swap in a fresh one before use —
    # otherwise the first request after an idle period fails with InterfaceError.
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()  # clear the validation transaction
    except psycopg2.Error:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    id           SERIAL PRIMARY KEY,
                    message_id   TEXT UNIQUE NOT NULL,
                    source       TEXT NOT NULL,
                    subject      TEXT NOT NULL,
                    date         TEXT NOT NULL,
                    body         TEXT NOT NULL,
                    from_addr    TEXT,
                    summary_json TEXT,
                    image_url    TEXT,
                    ingested_at  TEXT NOT NULL DEFAULT TO_CHAR(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                    tags_json    TEXT,
                    is_digest    INTEGER DEFAULT 0,
                    parent_id    INTEGER REFERENCES articles(id)
                )
            """)
            # Canonical link to the original article. `from_addr` was carrying
            # the *feed* URL for every RSS row, which is not something a reader
            # can follow, so link-outs need their own column.
            cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS url TEXT")
            # Backfill: message_id is the entry's permalink/GUID and is a usable
            # URL on every existing row.
            cur.execute("""
                UPDATE articles SET url = message_id
                WHERE url IS NULL AND message_id LIKE 'http%'
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_date ON articles(date DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)


def article_exists(message_id: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM articles WHERE message_id = %s", (message_id,))
            return cur.fetchone() is not None


def get_article_id(message_id: str) -> int | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM articles WHERE message_id = %s", (message_id,))
            row = cur.fetchone()
            return row[0] if row else None


def insert_article(a: dict):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO articles
                    (message_id, source, subject, date, body, from_addr, image_url, parent_id, url)
                VALUES
                    (%(message_id)s, %(source)s, %(subject)s, %(date)s, %(body)s,
                     %(from_addr)s, %(image_url)s, %(parent_id)s, %(url)s)
                ON CONFLICT (message_id) DO NOTHING
            """, {**a, "image_url": a.get("image_url"), "parent_id": a.get("parent_id"),
                  "url": a.get("url") or a.get("message_id")})


def get_article_by_id(article_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM articles WHERE id = %s", (article_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_recent_articles(limit: int = 40, source: str | None = None) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if source:
                cur.execute(
                    "SELECT * FROM articles WHERE source = %s AND (is_digest IS NULL OR is_digest = 0) ORDER BY date DESC LIMIT %s",
                    (source, limit)
                )
            else:
                cur.execute(
                    "SELECT * FROM articles WHERE (is_digest IS NULL OR is_digest = 0) ORDER BY date DESC LIMIT %s",
                    (limit,)
                )
            return [dict(r) for r in cur.fetchall()]


def get_articles_since(days: int = 30, limit: int = 150) -> list[dict]:
    """Most recent `limit` articles from the last `days`.

    The limit is not optional cosmetics: without it the Q&A context grows with
    the archive and eventually exceeds the model's context window (and costs
    dollars per question). Newest-first, so the cap drops the stalest rows.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM articles
                   WHERE date >= %s
                   AND (is_digest IS NULL OR is_digest = 0)
                   ORDER BY date DESC
                   LIMIT %s""",
                (cutoff, limit)
            )
            return [dict(r) for r in cur.fetchall()]


def get_sources() -> list[str]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT DISTINCT source FROM articles ORDER BY source")
            return [r["source"] for r in cur.fetchall()]


def save_summary(article_id: int, summary_json: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE articles SET summary_json = %s WHERE id = %s",
                (summary_json, article_id)
            )


def save_tags(article_id: int, tags_json: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE articles SET tags_json = %s WHERE id = %s",
                (tags_json, article_id)
            )


def get_untagged(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM articles WHERE tags_json IS NULL AND (is_digest IS NULL OR is_digest = 0) ORDER BY date DESC LIMIT %s",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]


def get_unsummarised(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM articles WHERE summary_json IS NULL AND (is_digest IS NULL OR is_digest = 0) ORDER BY date DESC LIMIT %s",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]


def count_articles() -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM articles")
            return cur.fetchone()[0]


def get_meta(key: str) -> str | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT value FROM meta WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None


def set_meta(key: str, value: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value)
            )
