"""
PostgreSQL (Supabase) database layer for Sourcing Africa.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pg_pool

_pool: pg_pool.SimpleConnectionPool | None = None


def _get_pool() -> pg_pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "DATABASE_URL is not set. On Railway use the Supabase "
                "connection-pooler string (IPv4), not the direct db host."
            )
        _pool = pg_pool.SimpleConnectionPool(1, 5, dsn)
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
                    (message_id, source, subject, date, body, from_addr, image_url, parent_id)
                VALUES
                    (%(message_id)s, %(source)s, %(subject)s, %(date)s, %(body)s,
                     %(from_addr)s, %(image_url)s, %(parent_id)s)
                ON CONFLICT (message_id) DO NOTHING
            """, {**a, "image_url": a.get("image_url"), "parent_id": a.get("parent_id")})


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


def get_articles_since(days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM articles
                   WHERE date >= %s
                   AND (is_digest IS NULL OR is_digest = 0)
                   ORDER BY date DESC""",
                (cutoff,)
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


def get_unextracted_newsletters(limit: int = 20) -> list[dict]:
    """Return newsletter digests that haven't been split into individual stories yet."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM articles
                   WHERE parent_id IS NULL
                   AND (is_digest IS NULL OR is_digest = 0)
                   AND length(body) > 2000
                   ORDER BY date DESC LIMIT %s""",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]


def mark_as_digest(article_id: int):
    """Hide a newsletter parent from the feed once its stories have been extracted."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE articles SET is_digest = 1 WHERE id = %s", (article_id,))


def count_articles() -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM articles")
            return cur.fetchone()[0]


def debug_stats() -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM articles")
            total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM articles WHERE is_digest = 1")
            digests = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM articles WHERE parent_id IS NOT NULL")
            children = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM articles WHERE parent_id IS NULL "
                "AND (is_digest IS NULL OR is_digest = 0) AND length(body) > 2000"
            )
            unextracted = cur.fetchone()["n"]
            cur.execute(
                "SELECT id, source, subject, is_digest, parent_id, length(body) AS body_len "
                "FROM articles ORDER BY id DESC LIMIT 10"
            )
            sample = [dict(r) for r in cur.fetchall()]
    return {
        "total": total,
        "digests_marked": digests,
        "child_stories": children,
        "unextracted_newsletters": unextracted,
        "recent_10": sample,
    }


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
