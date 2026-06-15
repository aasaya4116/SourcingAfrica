"""
One-shot migration: copy all rows from local SQLite → Supabase PostgreSQL.

Run this ONCE from your local machine before redeploying to Railway:
    pip install psycopg2-binary
    DATABASE_URL="postgresql://..." python migrate_sqlite_to_supabase.py

Set DATABASE_URL to your Supabase connection string (Transaction mode / port 5432).
The local SQLite file is read from data/sourcing_africa.db relative to this script.
"""

import os
import json
import sqlite3
import psycopg2
from pathlib import Path

SQLITE_PATH = Path(__file__).parent / "data" / "sourcing_africa.db"


def migrate():
    if not SQLITE_PATH.exists():
        print(f"SQLite DB not found at {SQLITE_PATH} — nothing to migrate.")
        return

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set DATABASE_URL environment variable before running.")

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(database_url)

    try:
        with pg_conn:
            cur = pg_conn.cursor()

            # --- articles ---
            articles = sqlite_conn.execute(
                "SELECT * FROM articles ORDER BY id"
            ).fetchall()
            print(f"Migrating {len(articles)} articles...")

            for row in articles:
                r = dict(row)
                cur.execute("""
                    INSERT INTO articles
                        (message_id, source, subject, date, body, from_addr,
                         summary_json, image_url, ingested_at, tags_json, is_digest, parent_id)
                    VALUES
                        (%(message_id)s, %(source)s, %(subject)s, %(date)s, %(body)s, %(from_addr)s,
                         %(summary_json)s, %(image_url)s, %(ingested_at)s, %(tags_json)s,
                         %(is_digest)s, %(parent_id)s)
                    ON CONFLICT (message_id) DO NOTHING
                """, r)

            # --- meta ---
            meta_rows = sqlite_conn.execute("SELECT * FROM meta").fetchall()
            print(f"Migrating {len(meta_rows)} meta rows...")

            for row in meta_rows:
                r = dict(row)
                cur.execute("""
                    INSERT INTO meta (key, value) VALUES (%(key)s, %(value)s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, r)

        print("Migration complete.")

    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    migrate()
