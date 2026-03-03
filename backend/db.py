"""
SQLite database layer for Sourcing Africa.
"""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "sourcing_africa.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id   TEXT UNIQUE NOT NULL,
                source       TEXT NOT NULL,
                subject      TEXT NOT NULL,
                date         TEXT NOT NULL,
                body         TEXT NOT NULL,
                from_addr    TEXT,
                summary_json TEXT,
                ingested_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON articles(date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
        # Migrate existing DB — add column if not present
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN summary_json TEXT")
        except Exception:
            pass


def article_exists(message_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def insert_article(a: dict):
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO articles
                (message_id, source, subject, date, body, from_addr)
            VALUES
                (:message_id, :source, :subject, :date, :body, :from_addr)
        """, a)


def get_recent_articles(limit: int = 40, source: str | None = None) -> list[dict]:
    with _conn() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM articles WHERE source = ? ORDER BY date DESC LIMIT ?",
                (source, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM articles ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_articles_since(days: int = 30) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM articles
               WHERE date >= datetime('now', ?)
               ORDER BY date DESC""",
            (f"-{days} days",)
        ).fetchall()
        return [dict(r) for r in rows]


def get_sources() -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source FROM articles ORDER BY source"
        ).fetchall()
        return [r["source"] for r in rows]


def save_summary(article_id: int, summary_json: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE articles SET summary_json = ? WHERE id = ?",
            (summary_json, article_id)
        )


def get_unsummarised(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM articles WHERE summary_json IS NULL ORDER BY date DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_articles() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
