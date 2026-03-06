"""
Sourcing Africa Ingestor — Gmail Edition
Polls your inbox every 6 hours for newsletters from configured senders.
Parses HTML email bodies into clean text and stores them in SQLite.
No forwarding rules required.
"""

import base64
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from pathlib import Path

import feedparser
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.db import init_db, article_exists, insert_article, set_meta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(ROOT / "config.json") as f:
        return json.load(f)


# ── Gmail authentication ──────────────────────────────────────────────────────

def get_gmail_service():
    """Build an authenticated Gmail service using env-var credentials."""
    client_id     = os.environ["GMAIL_CLIENT_ID"]
    client_secret = os.environ["GMAIL_CLIENT_SECRET"]
    refresh_token = os.environ["GMAIL_REFRESH_TOKEN"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Email parsing ─────────────────────────────────────────────────────────────

def decode_part(part: dict) -> bytes:
    data = part.get("body", {}).get("data", "")
    return base64.urlsafe_b64decode(data + "==")


def extract_text_from_payload(payload: dict) -> str:
    """Recursively extract readable text from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime == "text/plain":
        return decode_part(payload).decode("utf-8", errors="replace")

    if mime == "text/html":
        html = decode_part(payload).decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "img", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # Multipart: prefer plain text, fall back to HTML
    plain, html_text = "", ""
    for part in parts:
        t = extract_text_from_payload(part)
        if part.get("mimeType") == "text/plain" and not plain:
            plain = t
        elif part.get("mimeType") == "text/html" and not html_text:
            html_text = t
        elif part.get("mimeType", "").startswith("multipart/"):
            plain = plain or t

    return plain or html_text


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def parse_date(date_str: str) -> str:
    """Return ISO-8601 UTC date, best-effort."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Main ingest logic ─────────────────────────────────────────────────────────

def build_query(senders: list[dict], lookback_days: int) -> str:
    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    from_parts = " OR ".join(f"from:{s['match']}" for s in senders)
    return f"({from_parts}) after:{after}"


def fetch_and_store(service, cfg: dict) -> int:
    gmail_cfg = cfg["gmail"]
    query = build_query(gmail_cfg["senders"], gmail_cfg.get("lookback_days", 90))
    log.info("Gmail query: %s", query)

    # Collect all message IDs
    msg_ids = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        msg_ids.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d message(s) matching query.", len(msg_ids))
    new_count = 0

    sender_map = {s["match"]: s["name"] for s in gmail_cfg["senders"]}

    for meta in msg_ids:
        msg_id = meta["id"]

        # Use Gmail Message-ID header as our dedup key
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["Message-ID", "From", "Subject", "Date"]
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        message_id = get_header(headers, "Message-ID") or msg_id

        if article_exists(message_id):
            continue  # Already stored

        # Fetch full message for body
        full_msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        payload = full_msg.get("payload", {})
        full_headers = payload.get("headers", [])

        from_header = get_header(full_headers, "From")
        subject    = get_header(full_headers, "Subject") or "(no subject)"
        date_str   = get_header(full_headers, "Date")
        body       = extract_text_from_payload(payload)

        # Match sender name
        source = "Unknown"
        from_lower = from_header.lower()
        for match_key, name in sender_map.items():
            if match_key.lower() in from_lower:
                source = name
                break

        article = {
            "message_id": message_id,
            "source":     source,
            "subject":    subject,
            "date":       parse_date(date_str),
            "body":       body,
            "from_addr":  from_header,
        }
        insert_article(article)

        # Generate and cache summary immediately after storing
        try:
            import json as _json
            from backend.db import _conn as _db_conn
            from backend.qa import summarize_article
            with _db_conn() as c:
                row = c.execute(
                    "SELECT id FROM articles WHERE message_id = ?", (message_id,)
                ).fetchone()
            if row:
                article["id"] = row["id"]
                summarize_article(article, save=True)
                log.info("Summarised: [%s] %s", source, subject[:60])
        except Exception as exc:
            log.warning("Summary failed for '%s': %s", subject[:60], exc)

        new_count += 1
        log.info("Stored: [%s] %s", source, subject[:80])

    return new_count


def extract_rss_image(entry, raw_html: str) -> str | None:
    """Pull the best image URL from an RSS entry."""
    # 1. media:thumbnail (most common in modern feeds)
    for t in getattr(entry, "media_thumbnail", []):
        if t.get("url", "").startswith("http"):
            return t["url"]
    # 2. media:content with image type
    for mc in getattr(entry, "media_content", []):
        url = mc.get("url", "")
        mime = mc.get("type", "")
        if url.startswith("http") and (mime.startswith("image/") or url.rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp")):
            return url
    # 3. enclosures
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url")
    # 4. First <img> in raw HTML
    if raw_html:
        soup = BeautifulSoup(raw_html, "lxml")
        img = soup.find("img", src=True)
        if img and str(img["src"]).startswith("http"):
            return img["src"]
    return None


def fetch_rss(feed_cfg: dict) -> int:
    """Fetch new articles from a single RSS feed and store them."""
    url  = feed_cfg["url"]
    name = feed_cfg["name"]
    log.info("Polling RSS: %s (%s)", name, url)

    feed = feedparser.parse(url)
    new_count = 0

    for entry in feed.entries:
        message_id = entry.get("id") or entry.get("link") or ""
        if not message_id or article_exists(message_id):
            continue

        # Prefer full content, fall back to summary
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

        # Parse date from feedparser's pre-parsed tuple (more reliable than raw string)
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

        # Generate and cache summary immediately
        try:
            from backend.db import _conn as _db_conn
            from backend.qa import summarize_article
            with _db_conn() as c:
                row = c.execute(
                    "SELECT id FROM articles WHERE message_id = ?", (message_id,)
                ).fetchone()
            if row:
                article["id"] = row["id"]
                summarize_article(article, save=True)
                log.info("Summarised RSS: [%s] %s", name, entry.get("title", "")[:60])
        except Exception as exc:
            log.warning("RSS summary failed for '%s': %s", entry.get("title", "")[:60], exc)

        new_count += 1
        log.info("Stored RSS: [%s] %s", name, entry.get("title", "")[:80])

    return new_count


def run_ingestor():
    cfg = load_config()
    init_db()
    total = 0

    # Gmail newsletters
    try:
        service = get_gmail_service()
        total += fetch_and_store(service, cfg)
    except KeyError as e:
        log.error("Missing Gmail env var: %s. Skipping Gmail.", e)
    except Exception as e:
        log.error("Gmail fetch failed: %s", e)

    # RSS feeds
    for feed_cfg in cfg.get("rss", {}).get("feeds", []):
        try:
            total += fetch_rss(feed_cfg)
        except Exception as e:
            log.error("RSS fetch failed for %s: %s", feed_cfg.get("name"), e)

    set_meta("last_sync_at", datetime.now(timezone.utc).isoformat())
    log.info("Ingestion complete. %d new article(s) stored.", total)


def main():
    cfg = load_config()
    hours = cfg["gmail"].get("poll_hours", 6)
    log.info("Starting Sourcing Africa Ingestor — polling Gmail every %dh.", hours)
    run_ingestor()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_ingestor, "interval", hours=hours)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Ingestor stopped.")


if __name__ == "__main__":
    main()
