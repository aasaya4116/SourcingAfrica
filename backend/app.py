"""
Sourcing Africa — FastAPI Backend
Serves the PWA and provides API endpoints for Q&A and article browsing.
"""

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import ratelimit
from backend.auth import current_user, user_id
from backend.db import init_db, get_recent_articles, get_sources, count_articles, get_meta
from backend.qa import answer, summarize_article, backfill_summaries, backfill_tags, generate_suggestions, get_top5

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

log = logging.getLogger("uvicorn.error")

# The ingestor used to run as an unsupervised `python ingestor.py &` alongside
# uvicorn. When it died, PID 1 stayed up, so Railway never restarted anything
# and the archive silently went stale for months. Running it as a scheduled job
# inside this process means a crash is this process's crash — visible, and
# restarted by the platform.
RUN_INGESTOR = os.environ.get("RUN_INGESTOR", "1") != "0"

# Per-user hourly ceilings. Tuned for a handful of testers, not for scale —
# raise them deliberately, with the per-question cost in mind.
ASK_LIMIT_PER_HOUR = int(os.environ.get("ASK_LIMIT_PER_HOUR", "30"))
SUMMARY_LIMIT_PER_HOUR = int(os.environ.get("SUMMARY_LIMIT_PER_HOUR", "120"))

_scheduler = None


def _ingest_once():
    """One ingestion cycle, with the result recorded so /api/status can show staleness."""
    import sys
    sys.path.insert(0, str(ROOT))
    from ingestor.ingestor import run_ingestor
    from backend.db import set_meta
    try:
        run_ingestor()
        set_meta("last_sync_status", "ok")
    except Exception as exc:
        log.error("ingestion cycle failed: %s", exc, exc_info=True)
        set_meta("last_sync_status", f"error: {exc}"[:500])
        set_meta("last_sync_error_at", datetime.now(timezone.utc).isoformat())
        raise


def _startup_backfill():
    # Bounded on purpose: this runs on every restart, and an uncapped backfill
    # meant ~200 model calls each time the container bounced.
    try:
        backfill_summaries(limit=25)
        backfill_tags(limit=25)
    except Exception as exc:
        log.warning("startup backfill failed (non-fatal): %s", exc)


# Set at boot when auth can't be verified. Every data route already depends on
# current_user, which refuses while this is true — so the deployment serves
# nothing and the Anthropic key stays unspendable.
AUTH_MISCONFIGURED = False


def _check_auth_config():
    """Report whether auth is usable, without killing the process.

    This used to raise, which crash-looped the container. That was safe but
    undiagnosable: the only evidence was a traceback in the deploy log, and a
    dead container can't tell you which variables it actually received. Booting
    into a refusing state is equally safe and far easier to debug — /healthz
    reports the config state, so one URL answers "did the variable arrive?".
    """
    global AUTH_MISCONFIGURED
    if os.environ.get("AUTH_DISABLED", "0") == "1":
        log.warning(
            "AUTH_DISABLED=1 — every API endpoint is open. Local development only."
        )
        return
    if not (os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_JWT_SECRET")):
        AUTH_MISCONFIGURED = True
        log.error(
            "AUTH NOT CONFIGURED — serving /healthz only; every data endpoint "
            "will return 503. Set SUPABASE_URL (and SUPABASE_ANON_KEY) in this "
            "service's variables, then redeploy. Visit /healthz to confirm what "
            "this container actually received."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    _check_auth_config()
    # Never let a DB hiccup at boot crash-loop the container: log and carry on.
    # init_db() is idempotent and re-runs cheaply once the DB is back.
    try:
        init_db()
    except Exception as exc:
        log.error("init_db() failed at startup; serving anyway, will retry on demand: %s", exc)

    threading.Thread(target=_startup_backfill, daemon=True).start()

    if RUN_INGESTOR:
        from apscheduler.schedulers.background import BackgroundScheduler
        with open(ROOT / "config.json") as f:
            poll_hours = json.load(f).get("poll_hours", 6)
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.add_job(
            _ingest_once, "interval", hours=poll_hours,
            id="ingest", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
        _scheduler.start()
        # Kick one cycle now rather than waiting a full interval after a deploy.
        threading.Thread(target=_ingest_once, daemon=True).start()
        log.info("Ingestor scheduled every %dh", poll_hours)

    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Sourcing Africa", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tags(row: dict) -> dict:
    if not row.get("tags_json"):
        return {}
    try:
        return json.loads(row["tags_json"])
    except Exception:
        return {}


def _preview(body: str | None, limit: int = 200) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return body[:limit].strip() + ("…" if len(body) > limit else "")


# ── API routes ────────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class QuestionRequest(BaseModel):
    question: str = Field(max_length=2000)
    days: int = Field(default=30, ge=1, le=365)
    messages: list[Message] = Field(default_factory=list, max_length=40)


@app.post("/api/ask")
def ask(req: QuestionRequest, uid: str = Depends(user_id)):
    # Each question is a full model call over the archive, so this is where an
    # unauthenticated URL turns into someone else's Anthropic bill.
    ratelimit.check("ask", uid, limit=ASK_LIMIT_PER_HOUR, window=3600)
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages] or None
    try:
        result = answer(req.question.strip(), req.days, messages=msgs)
    except Exception as exc:
        log.error("/api/ask failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="The model call failed. Try again.")
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/articles")
def articles(limit: int = Query(default=20, ge=1, le=100), source: str | None = None,
             user: dict = Depends(current_user)):
    from backend.dedup import collapse_duplicates
    rows = get_recent_articles(limit=limit, source=source or None)
    rows = collapse_duplicates(rows)  # one card per story across outlets
    result = []
    for r in rows:
        tags = _tags(r)
        result.append({
            "id":        r["id"],
            "source":    r["source"],
            "subject":   r["subject"],
            "date":      r["date"][:10],
            "preview":   _preview(r["body"]),
            "image_url": r["image_url"],
            # The original article. Without it this is a news reader you can't
            # read the news from — and link-back is what makes republishing
            # other outlets' feeds fair to them.
            "url":       r.get("url") or r.get("message_id"),
            "country":   tags.get("country"),
            "topic":     tags.get("topic"),
            "coverage_count":  r.get("coverage_count", 1),
            "also_covered_by": r.get("also_covered_by", []),
        })
    return {"articles": result, "total": count_articles()}


@app.get("/api/articles/{article_id}")
def article_detail(article_id: int, user: dict = Depends(current_user)):
    from backend.db import get_article_by_id
    r = get_article_by_id(article_id)
    if not r:
        raise HTTPException(status_code=404, detail="Article not found")
    tags = _tags(r)
    return {
        "id":        r["id"],
        "source":    r["source"],
        "subject":   r["subject"],
        "date":      r["date"][:10],
        "body":      r["body"],
        "image_url": r["image_url"],
        "url":       r.get("url") or r.get("message_id"),
        "country":   tags.get("country"),
        "topic":     tags.get("topic"),
    }


@app.get("/api/articles/{article_id}/summary")
def article_summary(article_id: int, uid: str = Depends(user_id)):
    ratelimit.check("summary", uid, limit=SUMMARY_LIMIT_PER_HOUR, window=3600)
    from backend.db import get_article_by_id
    row = get_article_by_id(article_id)
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    # Pass save=True so a cache miss is written back automatically
    result = summarize_article(row, save=True)
    if result.get("error") == "headline_only":
        # Not a failure: GDELT items are headlines with no body. Say so, and let
        # the client send the reader to the source instead of inventing a summary.
        raise HTTPException(status_code=422, detail="headline_only")
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/sources")
def sources(user: dict = Depends(current_user)):
    return {"sources": get_sources()}


@app.get("/api/suggestions")
def suggestions(user: dict = Depends(current_user)):
    """Return 4 dynamic question chips generated from the current archive."""
    return {"suggestions": generate_suggestions()}


@app.get("/api/top5")
def top5(user: dict = Depends(current_user)):
    """Return Claude-curated top 5 stories from the last 14 days (cached 6h)."""
    return {"stories": get_top5()}


@app.get("/api/status")
def status(user: dict = Depends(current_user)):
    return {
        "status": "ok",
        "total_articles": count_articles(),
        "sources": get_sources(),
        "last_sync_at": get_meta("last_sync_at"),
        "last_sync_status": get_meta("last_sync_status"),
    }


@app.get("/api/config")
def config():
    """Public client config. Unauthenticated by necessity — the frontend needs
    these to build the Supabase client it signs in with. Both values are
    publishable; neither grants data access on its own, because every table has
    RLS on and the API is the only thing holding real credentials."""
    return {
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "auth_disabled": os.environ.get("AUTH_DISABLED", "0") == "1",
    }


@app.get("/healthz")
def healthz():
    """Liveness probe for Railway, and the fastest way to diagnose a bad deploy.

    Reports whether each required variable ARRIVED in this container — never
    its value. A variable saved in a dashboard but not applied to the running
    deployment is invisible from the outside otherwise, which is exactly the
    failure this endpoint exists to make obvious.
    """
    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "DATABASE_URL", "ANTHROPIC_API_KEY")
    present = {k: bool(os.environ.get(k)) for k in required}
    missing = [k for k, ok in present.items() if not ok]
    return {
        "ok": True,
        "auth_configured": not AUTH_MISCONFIGURED,
        "env_present": present,
        "missing": missing,
        "serving": "degraded — data endpoints return 503" if AUTH_MISCONFIGURED else "normal",
    }


# ── Serve PWA (must come last) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    # Without this guard an unknown /api/* path fell through to the SPA and
    # returned HTML with a 200, which reads as "endpoint works" to a client.
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Frontend not found")
