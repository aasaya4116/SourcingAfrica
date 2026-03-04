"""
Sourcing Africa — FastAPI Backend
Serves the PWA and provides API endpoints for Q&A and article browsing.
"""

import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.db import init_db, get_recent_articles, get_sources, count_articles, get_meta
from backend.qa import answer, summarize_article, backfill_summaries, generate_suggestions, get_top5

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Sourcing Africa", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup():
    init_db()
    # Backfill summaries for any articles that don't have one yet
    thread = threading.Thread(target=backfill_summaries, daemon=True)
    thread.start()


# ── API routes ────────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class QuestionRequest(BaseModel):
    question: str
    days: int = 30
    messages: list[Message] = []


@app.post("/api/ask")
def ask(req: QuestionRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages] or None
    result = answer(req.question.strip(), req.days, messages=msgs)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/articles")
def articles(limit: int = 20, source: str | None = None):
    rows = get_recent_articles(limit=limit, source=source or None)
    return {
        "articles": [
            {
                "id":      r["id"],
                "source":  r["source"],
                "subject": r["subject"],
                "date":    r["date"][:10],
                "preview": r["body"][:200].strip() + "…",
            }
            for r in rows
        ],
        "total": count_articles(),
    }


@app.get("/api/articles/{article_id}")
def article_detail(article_id: int):
    from backend.db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    r = dict(row)
    return {
        "id":       r["id"],
        "source":   r["source"],
        "subject":  r["subject"],
        "date":     r["date"][:10],
        "body":     r["body"],
    }


@app.get("/api/articles/{article_id}/summary")
def article_summary(article_id: int):
    from backend.db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    # Pass save=True so a cache miss is written back automatically
    result = summarize_article(dict(row), save=True)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/sources")
def sources():
    return {"sources": get_sources()}


@app.get("/api/suggestions")
def suggestions():
    """Return 4 dynamic question chips generated from the current archive."""
    chips = generate_suggestions()
    return {"suggestions": chips}


@app.get("/api/top5")
def top5():
    """Return Claude-curated top 5 stories from the last 14 days (cached 6h)."""
    stories = get_top5()
    return {"stories": stories}


@app.get("/api/status")
def status():
    return {
        "status": "ok",
        "total_articles": count_articles(),
        "sources": get_sources(),
        "last_sync_at": get_meta("last_sync_at"),
    }


@app.post("/api/sync")
def sync():
    """Trigger a Gmail sync in the background."""
    def _run():
        import sys
        from datetime import datetime, timezone
        sys.path.insert(0, str(ROOT))
        from ingestor.ingestor import run_ingestor
        from backend.db import set_meta
        run_ingestor()
        set_meta("last_sync_at", datetime.now(timezone.utc).isoformat())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"status": "sync started"}


# ── Serve PWA (must come last) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Frontend not found")
