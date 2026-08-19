"""
Remove off-topic headline-only (GDELT) rows from the archive.

GDELT's `sourcecountry:` operator matches where the *outlet* is based, not what
the story is about, so a query for "trade" or "funding" pulls in syndicated US
sports wire copy that African news sites republish. The ingestor now filters
these at write time (ingestor.is_relevant_headline); this script cleans up rows
that landed before that filter existed.

Only touches rows with an empty body — RSS articles always have one and are
never considered.

    python scripts/prune_offtopic.py            # dry run, prints what it would delete
    python scripts/prune_offtopic.py --apply    # actually delete
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from ingestor.ingestor import is_relevant_headline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the delete (default is a dry run)")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set.")

    conn = psycopg2.connect(dsn)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id, source, subject FROM articles WHERE body = '' OR body IS NULL")
    rows = cur.fetchall()
    bad = [r for r in rows if not is_relevant_headline(r["subject"])]

    print(f"headline-only rows: {len(rows)}")
    print(f"off-topic:          {len(bad)}")
    for r in bad[:25]:
        safe = r["subject"].encode("ascii", "replace").decode()[:70]
        print(f"   [{r['source'][:22]:<22}] {safe}")
    if len(bad) > 25:
        print(f"   … and {len(bad) - 25} more")

    if not args.apply:
        print("\nDry run — nothing deleted. Re-run with --apply to delete.")
        conn.close()
        return

    if bad:
        cur.execute("DELETE FROM articles WHERE id = ANY(%s)", ([r["id"] for r in bad],))
        # The cached Top 5 references article ids; drop it so it rebuilds
        # against rows that still exist.
        cur.execute(
            "DELETE FROM meta WHERE key IN "
            "('top5_json','top5_updated_at','suggestions_json','suggestions_updated_at')"
        )
        conn.commit()

    cur.execute("SELECT COUNT(*) AS n FROM articles")
    print(f"\nDeleted {len(bad)} rows. Archive now holds {cur.fetchone()['n']} articles.")
    conn.close()


if __name__ == "__main__":
    main()
