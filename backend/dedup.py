"""
Lightweight cross-source de-duplication — no external dependencies.

Many outlets (and GDELT's syndication) run the same story under near-identical
headlines. This collapses those into a single canonical article and records
which other outlets also covered it, so the feed shows one card instead of N.

Matching is heuristic (normalized-token Jaccard + containment within a few
days) — good enough to merge obvious duplicates without an embedding model.
"""

import re
from datetime import datetime

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at",
    "by", "from", "is", "are", "was", "were", "be", "as", "it", "its", "this",
    "that", "amp", "how", "why", "what", "new", "says", "after", "over", "into",
    "up", "out", "but", "his", "her", "their", "you", "your", "we", "amid",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(title: str) -> set:
    toks = _TOKEN_RE.findall((title or "").lower())
    return {t for t in toks if t not in _STOPWORDS and len(t) > 2}


def _similar(a: set, b: set, jaccard: float = 0.62, contain: float = 0.8) -> bool:
    """True if two token sets describe the same story."""
    if not a or not b:
        return False
    inter = len(a & b)
    if not inter:
        return False
    union = len(a | b)
    smaller = min(len(a), len(b))
    return (inter / union) >= jaccard or (inter / smaller) >= contain


def _within_days(d1: str, d2: str, days: int = 4) -> bool:
    try:
        a = datetime.fromisoformat((d1 or "")[:10])
        b = datetime.fromisoformat((d2 or "")[:10])
        return abs((a - b).days) <= days
    except Exception:
        return True  # unparseable dates shouldn't block grouping


def collapse_duplicates(
    articles: list[dict],
    *,
    subject_key: str = "subject",
    source_key: str = "source",
    date_key: str = "date",
    body_key: str = "body",
) -> list[dict]:
    """Collapse near-identical headlines into one canonical article each.

    Each returned item is a copy of the chosen canonical with two added keys:
      - coverage_count  : total outlets that ran the story (int, >= 1)
      - also_covered_by : the other outlets' names (list[str])

    Canonical = the member with a real body (full article over headline-only),
    tie-broken by most recent. Results are sorted by canonical date desc so the
    feed stays chronological.
    """
    n = len(articles)
    if n <= 1:
        return [{**a, "coverage_count": 1, "also_covered_by": []} for a in articles]

    tokens = [_tokens(a.get(subject_key, "")) for a in articles]
    used = [False] * n
    result = []

    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        members = [articles[i]]
        for j in range(i + 1, n):
            if used[j]:
                continue
            if _within_days(articles[i].get(date_key, ""), articles[j].get(date_key, "")) \
                    and _similar(tokens[i], tokens[j]):
                used[j] = True
                members.append(articles[j])

        canonical = max(
            members,
            key=lambda m: (
                1 if len((m.get(body_key) or "")) >= 200 else 0,
                m.get(date_key, ""),
            ),
        )
        seen, also = set(), []
        for m in members:
            src = m.get(source_key)
            if src and src != canonical.get(source_key) and src not in seen:
                seen.add(src)
                also.append(src)

        result.append({**canonical, "coverage_count": len(members), "also_covered_by": also})

    result.sort(key=lambda r: r.get(date_key, ""), reverse=True)
    return result
