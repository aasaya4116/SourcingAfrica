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
    save_tags, get_untagged,
)

# Current Opus. Same price as the 4.6 this used to pin, three generations newer.
DEFAULT_MODEL = "claude-opus-5"

# Hard ceiling on the archive we hand Claude for a question. Bodies are ~700
# chars each, so this keeps a question well inside the context window and at a
# predictable cost no matter how large the archive grows.
QA_MAX_ARTICLES = 120
QA_CHARS_PER_ARTICLE = 1200
QA_MAX_CONTEXT_CHARS = 220_000

# Mirrors the ingestor: below this there is nothing to summarise.
MIN_BODY_FOR_SUMMARY = 400


def _model() -> str:
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)


def _text(msg) -> str:
    """First text block of a response.

    Not `msg.content[0].text` — on current models content[0] is a thinking
    block, so indexing blindly raises AttributeError.
    """
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""


def _strip_fence(raw: str) -> str:
    """Unwrap ```json ... ``` fencing if the model added it."""
    raw = raw.strip()
    if not raw.startswith("```"):
        return raw
    parts = raw.split("```")
    if len(parts) < 2:
        return raw
    body = parts[1]
    if body.lstrip().lower().startswith("json"):
        body = body.lstrip()[4:]
    return body.strip()


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


def _enforce_source_cap(picks: list[dict], candidates: list[dict],
                        max_per_source: int = 2, target: int = 5) -> list[dict]:
    """Guarantee no single source dominates the Top 5; backfill from the
    deduped candidate pool (most recent) if the cap drops us below `target`."""
    import json
    capped, counts, picked = [], {}, set()
    for item in picks:
        src = item.get("source", "")
        if counts.get(src, 0) >= max_per_source:
            continue
        counts[src] = counts.get(src, 0) + 1
        capped.append(item)
        picked.add(item.get("id"))
    for a in candidates:
        if len(capped) >= target:
            break
        if a["id"] in picked or counts.get(a.get("source", ""), 0) >= max_per_source:
            continue
        tags = {}
        if a.get("tags_json"):
            try:
                tags = json.loads(a["tags_json"])
            except Exception:
                pass
        capped.append({
            "id":        a["id"],
            "source":    a["source"],
            "subject":   a["subject"],
            "date":      a["date"],
            "reason":    "Recent coverage",
            "ade_tag":   "",
            "ade_score": 0,
            "image_url": a.get("image_url"),
            "country":   tags.get("country"),
            "topic":     tags.get("topic"),
            "coverage_count":  a.get("coverage_count", 1),
            "also_covered_by": a.get("also_covered_by", []),
        })
        counts[a["source"]] = counts.get(a["source"], 0) + 1
        picked.add(a["id"])
    return capped


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

    # Collapse the same story from multiple outlets before ranking, so Claude
    # never sees (or picks) five versions of one headline.
    from backend.dedup import collapse_duplicates
    articles = collapse_duplicates(articles)

    article_list = "\n".join(
        f"{a['id']} | {a['source']} | {a['date'][:10]} | {a['subject']}"
        for a in articles[:60]
    )

    article_map = {a["id"]: a for a in articles}
    log.info("get_top5: %d articles available, IDs: %s", len(articles), list(article_map.keys())[:10])

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=_model(),
            max_tokens=1500,
            system=TOP5_SYSTEM,
            messages=[{"role": "user", "content": f"Articles:\n{article_list}"}],
        )
        raw = _strip_fence(_text(msg))
        log.info("get_top5 Claude raw: %s", raw[:300])
        picks = json.loads(raw)
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
                        "coverage_count":  a.get("coverage_count", 1),
                        "also_covered_by": a.get("also_covered_by", []),
                    })
                else:
                    log.warning("get_top5: Claude picked id=%s not in article_map", aid)
            # Enforce source diversity (HARD <=2/source) in code, not just prompt.
            result = _enforce_source_cap(result, articles, max_per_source=2, target=5)
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

    # Headline-only items (GDELT link-outs) have no body. Summarising nothing
    # produces a confident invention, so refuse rather than hallucinate.
    if len((article.get("body") or "").strip()) < MIN_BODY_FOR_SUMMARY:
        return {"error": "headline_only"}

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
        model=_model(),
        max_tokens=1500,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    try:
        raw = _strip_fence(_text(msg))
        result = json.loads(raw)
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
        model=_model(),
        max_tokens=200,
        system=SUGGESTIONS_SYSTEM,
        messages=[{"role": "user", "content": f"Recent newsletter topics:\n{topics}"}],
    )
    try:
        raw = _strip_fence(_text(msg))
        result = json.loads(raw)
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
            model=_model(),
            max_tokens=60,
            system=TAG_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        raw = _strip_fence(_text(msg))
        result = json.loads(raw)
        if save and article.get("id"):
            save_tags(article["id"], json.dumps(result))
        return result
    except Exception as exc:
        log.warning("tag_article failed for '%s': %s", article.get("subject", "")[:60], exc)
        return {}


def backfill_tags(limit: int = 100):
    """Generate tags for articles that don't have them yet."""
    articles = get_untagged(limit=limit)
    if not articles:
        return
    log.info("Backfilling tags for %d article(s)…", len(articles))
    for a in articles:
        result = tag_article(a, save=True)
        if result:
            log.info("Tagged: %s → %s/%s", a["subject"][:50], result.get("country"), result.get("topic"))


def backfill_summaries(limit: int = 100):
    """Generate and cache summaries for articles that don't have one yet."""
    from backend.db import get_unsummarised
    articles = get_unsummarised(limit=limit)
    if not articles:
        return
    log.info("Backfilling summaries for %d article(s)…", len(articles))
    for a in articles:
        result = summarize_article(a, save=True)
        if "error" not in result:
            log.info("Summarised: %s", a["subject"][:60])
        elif result["error"] != "headline_only":
            log.warning("Failed to summarise article %d: %s", a["id"], result["error"])


SYSTEM = """You are a knowledgeable assistant for Sourcing Africa, an intelligence tool
tracking African tech, business, and macro trends.

You are given a slice of a curated archive built from African tech and business
publications (TechCabal, Techpoint Africa, Nairametrics, Rest of World, The Africa
Report, African Business and others) plus the GDELT global news index.

Rules:
- Answer only from the articles provided. The archive slice is recent but partial —
  if it does not cover the question, say so plainly rather than filling the gap.
- Cite your sources: after each key claim, note (Source Name, date)
- Be direct — the user reads on mobile, keep answers tight
- When relevant, note patterns across sources (e.g. multiple outlets covering the same story)
- If you used web search, label those facts (Web)"""


# Claude's server-side web search. The old duckduckgo_search dependency was
# renamed upstream to `ddgs` and now returns zero results for every query, so
# the "live web" half of an answer had been silently dead.
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
}


def _all_text(msg) -> str:
    """Concatenate every text block — with web search the model may emit text
    before and after a search, and only joining them gives a whole answer."""
    parts = [b.text.strip() for b in msg.content
             if getattr(b, "type", None) == "text" and b.text.strip()]
    return "\n\n".join(parts)


def _count_searches(msg) -> int:
    return sum(1 for b in msg.content
               if getattr(b, "type", None) == "server_tool_use"
               and getattr(b, "name", "") == "web_search")


def build_context(articles: list[dict]) -> str:
    """Render articles as context, stopping at a hard character budget.

    Both caps matter: per-article truncation keeps one long piece from crowding
    out the rest, and the total budget keeps the request inside the context
    window (and at a predictable price) however large the archive grows.
    """
    lines, used = [], 0
    for a in articles[:QA_MAX_ARTICLES]:
        block = (
            f"---\n"
            f"SOURCE: {a['source']}\n"
            f"DATE: {a['date'][:10]}\n"
            f"SUBJECT: {a['subject']}\n"
            f"CONTENT:\n{(a.get('body') or '')[:QA_CHARS_PER_ARTICLE]}\n"
        )
        if used + len(block) > QA_MAX_CONTEXT_CHARS:
            break
        lines.append(block)
        used += len(block)
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

    context_prefix = (
        f"Today is {today}.\n\n"
        f"--- ARCHIVE (most recent {len(articles)} articles, past {days} days) ---\n"
        f"{archive_context}\n\n"
        "Answer from the archive first. If the archive doesn't cover it, or the "
        "question needs something more recent, use web search to fill the gap. "
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
    kwargs = dict(
        model=_model(),
        max_tokens=4096,
        system=SYSTEM,
        messages=api_messages,
    )
    try:
        msg = client.messages.create(tools=[WEB_SEARCH_TOOL], **kwargs)
    except anthropic.BadRequestError as exc:
        # Older//self-hosted model pins may not carry the server-side search
        # tool. The archive answer is still worth returning without it.
        log.warning("web_search unavailable, answering from archive only: %s", exc)
        msg = client.messages.create(**kwargs)

    return {
        "answer": _all_text(msg),
        "article_count": len(articles),
        "days_covered": days,
        "web_results": _count_searches(msg),
    }
