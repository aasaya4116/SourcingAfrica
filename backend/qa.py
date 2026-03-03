"""
Claude-powered Q&A against the article archive.
"""

import os
from datetime import datetime, timezone

import anthropic

from backend.db import get_recent_articles, get_articles_since


SYSTEM = """You are a knowledgeable assistant for Sourcing Africa, a personal intelligence tool
tracking African tech, business, and macro trends.

You have access to a curated archive of newsletters from Semafor Africa, Bloomberg Africa,
and Tech Safari. Answer questions directly and concisely based on the provided articles.

Rules:
- Cite your sources: after each key claim, note (Source Name, date)
- If the archive doesn't cover the question, say so plainly
- Be direct — the user reads on mobile, keep answers tight
- When relevant, note patterns across sources (e.g. multiple outlets covering the same story)"""


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

    context = build_context(articles)
    today = datetime.now(timezone.utc).strftime("%d %B %Y")

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
        max_tokens=1024,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Here is the newsletter archive from the past {days} days:\n\n"
                    f"{context}\n\n"
                    f"Question: {question}"
                ),
            }
        ],
    )
    return {
        "answer": msg.content[0].text.strip(),
        "article_count": len(articles),
        "days_covered": days,
    }
