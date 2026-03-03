"""
Claude-powered Q&A against the article archive.
"""

import os
from datetime import datetime, timezone

import anthropic

from backend.db import get_recent_articles, get_articles_since


SUMMARIZE_SYSTEM = """You are a concise analyst summarizing African tech and business newsletters.

Given a newsletter, return ONLY a JSON object with this exact structure:
{
  "headline": "<one punchy sentence capturing the single most important thing, ≤ 15 words>",
  "highlights": ["<point 1>", "<point 2>", "<point 3>"],
  "so_what": "<one sentence on why this matters>"
}

Rules:
- highlights: exactly 3 bullet points, each ≤ 20 words, start with a strong verb
- No markdown, no extra keys, just valid JSON"""


def summarize_article(article: dict) -> dict:
    import json
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
        max_tokens=300,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    try:
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if Claude wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return {"error": "Could not parse summary"}


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


def answer(question: str, days: int = 30) -> dict:
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

    # Always supplement with live web results
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

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
        max_tokens=1024,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {today}.\n\n"
                    f"--- NEWSLETTER ARCHIVE (past {days} days) ---\n"
                    f"{archive_context}"
                    f"{web_context}\n\n"
                    f"Question: {question}\n\n"
                    f"Answer using the archive first. Use web results to fill gaps or add recency. "
                    f"Label web-sourced facts with (Web) and archive facts with the source name and date."
                ),
            }
        ],
    )
    return {
        "answer": msg.content[0].text.strip(),
        "article_count": len(articles),
        "days_covered": days,
        "web_results": len(web_results),
    }
