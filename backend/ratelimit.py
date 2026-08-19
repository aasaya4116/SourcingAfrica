"""
Per-user rate limiting for the endpoints that cost money.

Backed by Upstash Redis when UPSTASH_REDIS_REST_URL / _TOKEN are set (the
workspace standard, and the only correct option once more than one instance is
running), and by an in-process counter otherwise so local development and a
single Railway container still get a real limit rather than none.
"""

import logging
import os
import threading
import time
from collections import defaultdict

import requests
from fastapi import HTTPException

log = logging.getLogger("uvicorn.error")

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

_local: dict[str, list[float]] = defaultdict(list)
_local_lock = threading.Lock()


def _allow_local(key: str, limit: int, window: int) -> tuple[bool, int]:
    now = time.time()
    with _local_lock:
        hits = [t for t in _local[key] if now - t < window]
        if len(hits) >= limit:
            _local[key] = hits
            return False, int(window - (now - hits[0]))
        hits.append(now)
        _local[key] = hits
        return True, 0


def _allow_upstash(key: str, limit: int, window: int) -> tuple[bool, int]:
    """INCR + EXPIRE via the Upstash REST API — a fixed window, which is all
    this needs and avoids a Lua script round trip."""
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    try:
        r = requests.post(
            f"{UPSTASH_URL}/pipeline",
            headers=headers,
            json=[["INCR", key], ["TTL", key]],
            timeout=5,
        )
        r.raise_for_status()
        results = r.json()
        count = int(results[0]["result"])
        ttl = int(results[1]["result"])
        if ttl < 0:  # key had no expiry yet — this call opened the window
            requests.post(f"{UPSTASH_URL}/expire/{key}/{window}", headers=headers, timeout=5)
            ttl = window
        if count > limit:
            return False, max(ttl, 1)
        return True, 0
    except Exception as exc:
        # A limiter outage must not take the API down with it. Log loudly and
        # fall back to the in-process counter rather than failing open entirely.
        log.warning("Upstash rate limit unavailable (%s) — using in-process limiter", exc)
        return _allow_local(key, limit, window)


def check(scope: str, identity: str, limit: int, window: int = 3600):
    """Raise 429 if `identity` has exceeded `limit` calls to `scope` in `window` seconds."""
    key = f"ratelimit:{scope}:{identity}"
    allowed, retry_after = (
        _allow_upstash(key, limit, window) if UPSTASH_URL and UPSTASH_TOKEN
        else _allow_local(key, limit, window)
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit reached. Try again in about {max(retry_after // 60, 1)} minute(s).",
            headers={"Retry-After": str(max(retry_after, 1))},
        )
