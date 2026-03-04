"""
Claude-powered Q&A against the article archive.
"""

import os
from datetime import datetime, timezone

import anthropic

from backend.db import get_recent_articles, get_articles_since, get_meta, set_meta


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
