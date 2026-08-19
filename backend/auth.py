"""
Supabase Auth verification for the API.

Every model-calling endpoint sits behind this. Without it, anyone who has the
URL can spend the deployment's Anthropic key, which is the single thing that
makes an otherwise-shareable app unsafe to share.

Two verification paths, because Supabase projects sign JWTs differently
depending on when they were created:

  * asymmetric (current default) — RS256/ES256, verified against the project's
    published JWKS, which we fetch once and cache.
  * legacy shared secret — HS256, verified with SUPABASE_JWT_SECRET.

Set whichever your project uses; if both are present the JWKS path wins.
"""

import logging
import os
import time
from typing import Any

import jwt
import requests
from fastapi import Depends, HTTPException, Request

log = logging.getLogger("uvicorn.error")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")

# Comma-separated emails allowed to sign in. Empty means "anyone with a valid
# Supabase account for this project" — fine once you intend to be public, wrong
# while you are handing the URL to a handful of testers.
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if e.strip()
}

# Escape hatch for local development only. Never set this in Railway.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "0") == "1"

_jwks_client: Any = None
_jwks_fetched_at = 0.0
_JWKS_TTL = 3600


def _get_jwks_client():
    """Cached PyJWKClient for the project's published keys."""
    global _jwks_client, _jwks_fetched_at
    if not SUPABASE_URL:
        return None
    now = time.time()
    if _jwks_client is None or (now - _jwks_fetched_at) > _JWKS_TTL:
        try:
            _jwks_client = jwt.PyJWKClient(
                f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
                cache_keys=True,
            )
            _jwks_fetched_at = now
        except Exception as exc:
            log.warning("could not build JWKS client: %s", exc)
            return None
    return _jwks_client


def _decode(token: str) -> dict:
    """Verify a Supabase access token and return its claims.

    Raises a jwt.PyJWTError when a verifier existed and rejected the token, and
    only reports a configuration fault when no verifier was available at all.
    Collapsing those two cases turns an ordinary expired session into a 500,
    which the frontend can't tell apart from a server fault — so it never
    re-prompts for sign-in.
    """
    client = _get_jwks_client()
    jwks_error: Exception | None = None

    if client is not None:
        try:
            key = client.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["RS256", "ES256"], audience="authenticated")
        except jwt.PyJWTError as exc:
            # Might still be a legacy HS256 project, whose published JWKS won't
            # match; keep the error in case the secret path isn't available.
            jwks_error = exc
        except Exception as exc:
            log.warning("JWKS verification error: %s", exc)
            jwks_error = exc

    if JWT_SECRET:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="authenticated")

    if isinstance(jwks_error, jwt.PyJWTError):
        raise jwks_error          # a real verifier said no — that's a 401
    if jwks_error is not None:
        raise HTTPException(status_code=503, detail="Cannot verify sessions right now.")

    raise HTTPException(
        status_code=503,
        detail="This deployment is missing its auth configuration. "
               "Check /healthz to see which variables reached the server.",
    )


def current_user(request: Request) -> dict:
    """FastAPI dependency. Returns the caller's claims, or raises 401/403."""
    if AUTH_DISABLED:
        return {"sub": "local-dev", "email": "dev@localhost"}

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to use Sourcing Africa.")
    token = header.split(" ", 1)[1].strip()

    try:
        claims = _decode(token)
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — sign in again.")
    except jwt.PyJWTError as exc:
        log.info("rejected token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid session.")

    email = (claims.get("email") or "").lower()
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="This account is not on the tester list.")

    return claims


def user_id(user: dict = Depends(current_user)) -> str:
    """The caller's stable id — the key rate limits and quotas are counted against."""
    return user.get("sub") or user.get("email") or "unknown"
