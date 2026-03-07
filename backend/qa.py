"""
Claude-powered Q&A against the article archive.
"""

import logging
import os
from datetime import datetime, timezone

import anthropic

log = logging.getLogger(__name__)

from backend.db import (
    get_recent_articles, get_articles_since, get_meta, set_meta,
    save_tags, get_untagged, get_unextracted_newsletters, mark_as_digest,
    insert_article,
)


TOP5_SYSTEM = """You are a signal analyst for Sourcing Africa using the ADE Framework to rank African tech and business stories.

The ADE Framework scores each article on three dimensions (1–10 each):
- AUTOMATION: Does this story reveal an efficiency gain, tech adoption, or process transformation?
- DISCOVERY: Does this story cover a startup launch, funding round, new market entrant, or product release?
- EMERGENCE: Does this story signal a macro shift — policy change, infrastructure build-out, sector-wide trend, or geopolitical move?

Given a list of articles, score each on all three ADE dimensions, then select the 5 with the highest combined ADE score.

Return ONLY a JSON array of exactly 5 objects:
[{"id": <integer>, "ade_tag": "<strongest signal: AUTOMATION | DISCOVERY | EMERGENCE>", "ade_score": <total 1-30>, "reason": "<what ADE signal this story carries, ≤ 15 words>"}, ...]

Rules:
- HARD RULE: No more than 2 stories from the same source
- HARD RULE: No two stories on the same theme
- No markdown, valid JSON only"""


def get_top5() -> list[dict]:
    import json

    # Return cached if < 6 hours old
    cached  = get_meta("top5_json")
    updated = get_meta("top5_updated_at")
    if cached and updated:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()
            if age < 21600:
                return json.loads(cached)
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    articles = get_articles_since(14) or get_recent_articles(limit=50)
    if not articles:
        return []

    article_list = "\n".join(
        f"{a['id']} | {a['source']} | {a['date'][:10]} | {a['subject']}"
        for a in articles[:60]
    )

    article_map = {a["id"]: a for a in articles}
    log.info("get_top5: %d articles available, IDs: %s", len(articles), list(article_map.keys())[:10])

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
            max_tokens=300,
            system=TOP5_SYSTEM,
            messages=[{"role": "user", "content": f"Articles:\n{article_list}"}],
        )
        raw = msg.content[0].text.strip()
        log.info("get_top5 Claude raw: %s", raw[:300])
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        picks = json.loads(raw.strip())
        if isinstance(picks, list):
            result = []
            for p in picks[:5]:
                aid = int(p.get("id", 0))
                if aid in article_map:
                    a = article_map[aid]
                    tags = {}
                    if a.get("tags_json"):
                        try:
                            tags = json.loads(a["tags_json"])
                        except Exception:
                            pass
                    result.append({
                        "id":        aid,
                        "source":    a["source"],
                        "subject":   a["subject"],
                        "date":      a["date"],
                        "reason":    p.get("reason", ""),
                        "ade_tag":   p.get("ade_tag", ""),
                        "ade_score": p.get("ade_score", 0),
                        "image_url": a.get("image_url"),
                        "country":   tags.get("country"),
                        "topic":     tags.get("topic"),
                    })
                else:
                    log.warning("get_top5: Claude picked id=%s not in article_map", aid)
            if result:
                set_meta("top5_json",       json.dumps(result))
                set_meta("top5_updated_at", datetime.now(timezone.utc).isoformat())
                return result
    except Exception as exc:
        log.error("get_top5 failed: %s", exc, exc_info=True)
    return []


SUMMARIZE_SYSTEM = """You are a concise analyst summarizing African tech and business newsletters.

Given a newsletter, return ONLY a JSON object with this exact structure:
{
  "summary": "<4-6 sentence paragraph covering what happened, key details, and context>",
  "takeaways": ["<takeaway 1>", "<takeaway 2>", "<takeaway 3>", "<takeaway 4>"],
  "so_what": "<one sentence on why this matters for African tech or business>"
}

Rules:
- summary: 4-6 complete sentences, factual, covering the core story and key details
- takeaways: 3-4 bullets, each ≤ 20 words, start with a strong verb, most important insights only
- so_what: one crisp sentence on the broader implication
- No markdown, no extra keys, just valid JSON"""


def summarize_article(article: dict, save: bool = False) -> dict:
    import json
    from backend.db import save_summary

    # Return cached summary if available
    if article.get("summary_json"):
        try:
            return json.loads(article["summary_json"])
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    client = anthropic.Anthropic(api_key=api_key)
    content = (
        f"Source: {article['source']}\n"
        f"Date: {article['date']}\n"
        f"Subject: {article['subject']}\n\n"
        f"{article['body'][:3000]}"
    )
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
        max_tokens=500,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    try:
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        if save and article.get("id"):
            save_summary(article["id"], json.dumps(result))
        return result
    except Exception:
        return {"error": "Could not parse summary"}


SUGGESTIONS_SYSTEM = """You are a brief assistant for Sourcing Africa, tracking African tech, business, and macro trends.

Based on the recent newsletter topics provided, generate exactly 4 concise questions a reader would want to ask.
Return ONLY a JSON array of 4 strings, e.g.:
["What happened in Nigerian fintech this week?", "Any new startup funding in East Africa?", "What's the latest on African infrastructure?", "Key macro trends to watch?"]

Rules:
- Each question ≤ 12 words
- Questions must be grounded in the actual topics from the articles provided
- Vary the topics — don't repeat the same country or theme twice
- No markdown, just valid JSON"""


def generate_suggestions() -> list[str]:
    import json
    from datetime import datetime, timezone

    # Return cached suggestions if < 24 hours old
    cached = get_meta("suggestions_json")
    updated = get_meta("suggestions_updated_at")
    if cached and updated:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()
            if age < 86400:
                return json.loads(cached)
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    articles = get_recent_articles(limit=20)
    if not articles:
        return []

    topics = "\n".join(
        f"- {a['subject']} ({a['source']}, {a['date'][:10]})" for a in articles
    )

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
        max_tokens=200,
        system=SUGGESTIONS_SYSTEM,
        messages=[{"role": "user", "content": f"Recent newsletter topics:\n{topics}"}],
    )
    try:
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        if isinstance(result, list):
            suggestions = [str(s) for s in result[:4]]
            set_meta("suggestions_json", json.dumps(suggestions))
            set_meta("suggestions_updated_at", datetime.now(timezone.utc).isoformat())
            return suggestions
    except Exception:
        pass
    return []


TAG_SYSTEM = """You extract geographic and topic tags from African tech/business news articles.
Return ONLY valid JSON — no markdown, no explanation:
{"country": "<primary African country, or 'Pan-Africa'>", "topic": "<one of: Fintech, Startups, Energy, Logistics, Policy, AI, Infrastructure, Telecom, E-commerce, Agriculture, Health, Media, Other>"}"""


def tag_article(article: dict, save: bool = False) -> dict:
    import json
    if article.get("tags_json"):
        try:
            return json.loads(article["tags_json"])
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {}

    client = anthropic.Anthropic(api_key=api_key)
    content = f"Title: {article['subject']}\n\n{article['body'][:600]}"
    try:
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
            max_tokens=60,
            system=TAG_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        if save and article.get("id"):
            save_tags(article["id"], json.dumps(result))
        return result
    except Exception as exc:
        log.warning("tag_article failed for '%s': %s", article.get("subject", "")[:60], exc)
        return {}


EXTRACT_STORIES_SYSTEM = """You extract individual news stories from African newsletter digests.

Given a newsletter body, identify and return each distinct story it contains.
Return ONLY a JSON array:
[{"headline": "<concise story title, ≤ 12 words>", "body": "<story text, max 400 words>"}, ...]

Rules:
- Each element = ONE story (one event, one company, one policy move, one data point)
- Do not combine unrelated items into one entry
- Skip boilerplate: ads, subscription CTAs, unsubscribe links, navigation menus
- Return 2–8 stories per newsletter
- No markdown, valid JSON only"""


def extract_stories(article: dict) -> list[dict]:
    """Split a newsletter article into individual story dicts ready for insert_article."""
    import json

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    content = f"Source: {article['source']}\nDate: {article['date'][:10]}\n\n{article['body'][:6000]}"
    try:
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
            max_tokens=2000,
            system=EXTRACT_STORIES_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        stories = json.loads(raw.strip())
        if not isinstance(stories, list):
            return []
        parent_id = article.get("id")
        result = []
        for i, s in enumerate(stories):
            headline = s.get("headline", "").strip()
            body = s.get("body", "").strip()
            if not headline or not body:
                continue
            result.append({
                "message_id": f"story:{parent_id}:{i}",
                "source":     article["source"],
                "subject":    headline,
                "date":       article["date"],
                "body":       body,
                "from_addr":  article.get("from_addr", ""),
                "image_url":  article.get("image_url"),
                "parent_id":  parent_id,
            })
        return result
    except Exception as exc:
        log.error("extract_stories failed for '%s': %s", article.get("subject", "")[:60], exc)
        return []


def backfill_stories():
    """Split all unextracted newsletter digests into individual story articles."""
    from backend.db import _conn

    newsletters = get_unextracted_newsletters(limit=20)
    if not newsletters:
        return
    log.info("Extracting stories from %d newsletter(s)…", len(newsletters))
    for newsletter in newsletters:
        stories = extract_stories(newsletter)
        if stories:
            for s in stories:
                insert_article(s)
                try:
                    with _conn() as c:
                        row = c.execute(
                            "SELECT id FROM articles WHERE message_id = ?", (s["message_id"],)
                        ).fetchone()
                    if row:
                        s["id"] = row["id"]
                        tag_article(s, save=True)
                except Exception:
                    pass
            mark_as_digest(newsletter["id"])
            log.info("Extracted %d stories from: %s", len(stories), newsletter["subject"][:60])
        else:
            # Mark as digest to avoid re-processing
            mark_as_digest(newsletter["id"])
            log.warning("No stories extracted from: %s", newsletter["subject"][:60])


def backfill_tags():
    """Generate tags for all articles that don't have them yet."""
    articles = get_untagged(limit=100)
    if not articles:
        return
    log.info("Backfilling tags for %d article(s)…", len(articles))
    for a in articles:
        result = tag_article(a, save=True)
        if result:
            log.info("Tagged: %s → %s/%s", a["subject"][:50], result.get("country"), result.get("topic"))


def backfill_summaries():
    """Generate and cache summaries for all articles that don't have one yet."""
    import logging
    from backend.db import get_unsummarised
    log = logging.getLogger(__name__)
    articles = get_unsummarised(limit=100)
    if not articles:
        return
    log.info("Backfilling summaries for %d article(s)…", len(articles))
    for a in articles:
        result = summarize_article(a, save=True)
        if "error" not in result:
            log.info("Summarised: %s", a["subject"][:60])
        else:
            log.warning("Failed to summarise article %d: %s", a["id"], result["error"])


SYSTEM = """You are a knowledgeable assistant for Sourcing Africa, a personal intelligence tool
tracking African tech, business, and macro trends.

You have access to a curated archive of newsletters from Semafor Africa, Bloomberg Africa,
and Tech Safari. Answer questions directly and concisely based on the provided articles.

Rules:
- Cite your sources: after each key claim, note (Source Name, date)
- If the archive doesn't cover the question, say so plainly
- Be direct — the user reads on mobile, keep answers tight
- When relevant, note patterns across sources (e.g. multiple outlets covering the same story)"""


def web_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query + " Africa", max_results=max_results))
    except Exception:
        return []


def build_context(articles: list[dict]) -> str:
    lines = []
    for a in articles:
        date = a["date"][:10]
        lines.append(
            f"---\n"
            f"SOURCE: {a['source']}\n"
            f"DATE: {date}\n"
            f"SUBJECT: {a['subject']}\n"
            f"CONTENT:\n{a['body'][:1500]}\n"
        )
    return "\n".join(lines)


def answer(question: str, days: int = 30, messages: list[dict] | None = None) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    articles = get_articles_since(days)
    if not articles:
        articles = get_recent_articles(limit=40)

    if not articles:
        return {
            "answer": "No articles in the archive yet. The ingestor may still be running.",
            "article_count": 0,
        }

    archive_context = build_context(articles)
    today = datetime.now(timezone.utc).strftime("%d %B %Y")

    # Web search based on the latest question
    web_results = web_search(question)
    web_context = ""
    if web_results:
        web_lines = []
        for r in web_results:
            web_lines.append(
                f"TITLE: {r.get('title', '')}\n"
                f"SOURCE: {r.get('href', '')}\n"
                f"SNIPPET: {r.get('body', '')}"
            )
        web_context = "\n\n--- LIVE WEB RESULTS ---\n" + "\n\n".join(web_lines)

    context_prefix = (
        f"Today is {today}.\n\n"
        f"--- NEWSLETTER ARCHIVE (past {days} days) ---\n"
        f"{archive_context}"
        f"{web_context}\n\n"
        "Answer using the archive first. Use web results to fill gaps or add recency. "
        "Label web-sourced facts with (Web) and archive facts with the source name and date."
    )

    # Build messages for Claude — inject context into the first user turn
    if messages and len(messages) > 0:
        api_messages = []
        first_user_done = False
        for m in messages:
            if m["role"] == "user" and not first_user_done:
                api_messages.append({
                    "role": "user",
                    "content": context_prefix + "\n\n" + m["content"],
                })
                first_user_done = True
            else:
                api_messages.append({"role": m["role"], "content": m["content"]})
    else:
        api_messages = [{
            "role": "user",
            "content": context_prefix + f"\n\nQuestion: {question}",
        }]

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
        max_tokens=1024,
        system=SYSTEM,
        messages=api_messages,
    )
    return {
        "answer": msg.content[0].text.strip(),
        "article_count": len(articles),
        "days_covered": days,
        "web_results": len(web_results),
    }
