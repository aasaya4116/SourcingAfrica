"""
Sourcing Africa Ingestor — Web Edition
Polls RSS feeds and the GDELT global news index every few hours for
African tech & business stories. No inbox, no OAuth — fully shareable.

Sources are configured in config.json:
  - rss.feeds[]      : curated full-text feeds (high signal)
  - gdelt.queries[]  : headline-level breadth across the open web
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.db import (
    init_db, article_exists, insert_article, set_meta,
    get_article_id, save_tags,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Articles shorter than this are treated as headline-only: stored and shown,
# but not sent to Claude for summarisation (nothing to summarise).
MIN_BODY_FOR_SUMMARY = 400

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def load_config() -> dict:
    with open(ROOT / "config.json") as f:
        return json.load(f)


# ── Enrichment ────────────────────────────────────────────────────────────────

def enrich(article: dict, message_id: str):
    """Generate + cache a summary and tags for a freshly stored article.

    Headline-only items (short/empty body) are skipped — there is nothing to
    summarise and it avoids burning Claude calls on link-outs.
    """
    if len((article.get("body") or "")) < MIN_BODY_FOR_SUMMARY:
        return
    try:
        from backend.qa import summarize_article, tag_article
        article_id = get_article_id(message_id)
        if article_id:
            article["id"] = article_id
            summarize_article(article, save=True)
            tag_article(article, save=True)
            log.info("Enriched: [%s] %s", article["source"], article["subject"][:60])
    except Exception as exc:
        log.warning("Enrich failed for '%s': %s", article.get("subject", "")[:60], exc)


# ── RSS feeds ─────────────────────────────────────────────────────────────────

def extract_rss_image(entry, raw_html: str) -> str | None:
    """Pull the best image URL from an RSS entry."""
    for t in getattr(entry, "media_thumbnail", []):
        if t.get("url", "").startswith("http"):
            return t["url"]
    for mc in getattr(entry, "media_content", []):
        url = mc.get("url", "")
        mime = mc.get("type", "")
        if url.startswith("http") and (mime.startswith("image/") or url.rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp")):
            return url
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url")
    if raw_html:
        soup = BeautifulSoup(raw_html, "lxml")
        img = soup.find("img", src=True)
        if img and str(img["src"]).startswith("http"):
            return img["src"]
    return None


def fetch_rss(feed_cfg: dict) -> int:
    """Fetch new articles from a single RSS feed and store them."""
    url = feed_cfg["url"]
    name = feed_cfg["name"]
    log.info("Polling RSS: %s (%s)", name, url)

    feed = feedparser.parse(url)
    if getattr(feed, "bozo", 0) and not feed.entries:
        log.warning("Feed %s returned no entries (bozo: %s)", name, getattr(feed, "bozo_exception", ""))
        return 0

    new_count = 0
    for entry in feed.entries:
        message_id = entry.get("id") or entry.get("link") or ""
        if not message_id or article_exists(message_id):
            continue

        raw_html = ""
        if hasattr(entry, "content") and entry.content:
            raw_html = entry.content[0].get("value", "")
        if not raw_html:
            raw_html = entry.get("summary", "")

        image_url = extract_rss_image(entry, raw_html)

        body = ""
        if raw_html:
            soup = BeautifulSoup(raw_html, "lxml")
            for tag in soup(["script", "style", "img", "nav", "footer"]):
                tag.decompose()
            body = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()

        if getattr(entry, "published_parsed", None):
            date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        article = {
            "message_id": message_id,
            "source":     name,
            "subject":    entry.get("title", "(no title)"),
            "date":       date,
            "body":       body,
            "from_addr":  url,
            "image_url":  image_url,
        }
        insert_article(article)
        enrich(article, message_id)

        new_count += 1
        log.info("Stored RSS: [%s] %s", name, entry.get("title", "")[:80])

    return new_count


# ── GDELT global news index ───────────────────────────────────────────────────

def parse_gdelt_date(s: str) -> str:
    """GDELT seendate format is e.g. 20260615T123000Z."""
    try:
        dt = datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_gdelt(query_cfg: dict, gdelt_cfg: dict) -> int:
    """Fetch headline-level articles from the GDELT DOC 2.0 API (no key required)."""
    name = query_cfg["name"]
    params = {
        "query":      query_cfg["query"],
        "mode":       "ArtList",
        "format":     "json",
        "maxrecords": gdelt_cfg.get("maxrecords", 75),
        "timespan":   gdelt_cfg.get("timespan", "1d"),
        "sort":       "DateDesc",
    }
    log.info("Polling GDELT: %s", name)

    resp = requests.get(
        GDELT_ENDPOINT, params=params, timeout=30,
        headers={"User-Agent": "SourcingAfrica/1.0"},
    )
    resp.raise_for_status()
    data = resp.json()

    new_count = 0
    for art in data.get("articles", []):
        url = art.get("url", "")
        if not url or article_exists(url):
            continue

        domain = art.get("domain", "") or "GDELT"
        article = {
            "message_id": url,
            "source":     domain,
            "subject":    art.get("title", "(no title)"),
            "date":       parse_gdelt_date(art.get("seendate", "")),
            "body":       "",  # headline-only
            "from_addr":  url,
            "image_url":  art.get("socialimage") or None,
        }
        insert_article(article)

        # GDELT gives us the source country for free — tag it without a Claude call.
        country = (art.get("sourcecountry") or "").strip()
        if country:
            article_id = get_article_id(url)
            if article_id:
                save_tags(article_id, json.dumps({"country": country, "topic": None}))

        new_count += 1
        log.info("Stored GDELT: [%s] %s", domain, art.get("title", "")[:80])

    return new_count


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_ingestor():
    cfg = load_config()
    init_db()
    total = 0

    for feed_cfg in cfg.get("rss", {}).get("feeds", []):
        try:
            total += fetch_rss(feed_cfg)
        except Exception as e:
            log.error("RSS fetch failed for %s: %s", feed_cfg.get("name"), e)

    gdelt_cfg = cfg.get("gdelt", {})
    if gdelt_cfg.get("enabled"):
        for q in gdelt_cfg.get("queries", []):
            try:
                total += fetch_gdelt(q, gdelt_cfg)
            except Exception as e:
                log.error("GDELT fetch failed for %s: %s", q.get("name"), e)

    set_meta("last_sync_at", datetime.now(timezone.utc).isoformat())
    log.info("Ingestion complete. %d new article(s) stored.", total)


def main():
    cfg = load_config()
    hours = cfg.get("poll_hours", 6)
    log.info("Starting Sourcing Africa Ingestor — polling web sources every %dh.", hours)
    run_ingestor()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_ingestor, "interval", hours=hours)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Ingestor stopped.")


if __name__ == "__main__":
    main()
