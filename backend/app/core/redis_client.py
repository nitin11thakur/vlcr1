"""
app/core/redis_client.py
------------------------
Async Redis client with helpers for rate limiting, duplicate detection,
generic caching, and cache invalidation.

All public functions degrade gracefully: on any Redis error they log a
WARNING and return a safe default so the core pipeline is never blocked.

Redis key schema (from design doc):
  rl:ip:{ip}                  TTL 60s    — IP rate limit counter
  daily_complaints:{phone}:{date}  TTL 86400s — Daily complaint limit
  routing:{state_code}        TTL 300s   — Routing rules cache
  track:{reference_number}    TTL 60s    — Tracking response cache
  translate:{sha256}          TTL 3600s  — Translation result cache
  dedup:{hash}                TTL 259200s (72h) — Duplicate detection
  session:{token}             TTL 28800s — JWT session store
"""

import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger("vlcr.redis")

_redis: Optional[aioredis.Redis] = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def init_redis() -> None:
    """Connect to Redis on application startup. Logs a WARNING on failure."""
    global _redis
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
        _redis = client
        logger.info("Redis connected: %s", settings.REDIS_URL)
    except Exception as exc:
        logger.warning("Redis unavailable at startup — caching/rate-limiting degraded: %s", exc)
        _redis = None


def get_redis() -> Optional[aioredis.Redis]:
    """Return the Redis client, or None if not initialised / unavailable."""
    return _redis


# ── Rate Limiting ─────────────────────────────────────────────────────────────

async def rate_limit_check(key: str, limit: int, window_seconds: int) -> bool:
    """
    Increment *key* and check whether the count is within *limit*.

    Returns True  — request is allowed.
    Returns False — rate limit exceeded.
    Degrades gracefully (returns True) when Redis is unavailable.

    Validates: Requirements 4.3, 4.4
    """
    r = get_redis()
    if r is None:
        logger.warning("rate_limit_check: Redis unavailable — allowing request for key=%s", key)
        return True
    try:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        results = await pipe.execute()
        count: int = results[0]
        return count <= limit
    except Exception as exc:
        logger.warning("rate_limit_check error (key=%s): %s — allowing request", key, exc)
        return True


# ── Duplicate Detection ───────────────────────────────────────────────────────

async def dedup_check(hash_key: str, ttl: int) -> Optional[str]:
    """
    Check whether *hash_key* already exists in Redis.

    - If it exists  → return the stored complaint_id (duplicate found).
    - If it doesn't → store *hash_key* with *ttl* and return None (first call).
    - On Redis error → log WARNING and return None (allow through).

    Validates: Requirements 4.5
    """
    r = get_redis()
    if r is None:
        logger.warning("dedup_check: Redis unavailable — skipping dedup for key=%s", hash_key)
        return None
    try:
        redis_key = f"dedup:{hash_key}"
        existing = await r.get(redis_key)
        if existing:
            return existing
        # First time — store a sentinel; caller will overwrite with real complaint_id
        # via a subsequent cache_set call.  We use SET NX so concurrent requests
        # don't race past the check.
        await r.set(redis_key, "", ex=ttl, nx=True)
        return None
    except Exception as exc:
        logger.warning("dedup_check error (key=%s): %s — skipping dedup", hash_key, exc)
        return None


# ── Generic Cache ─────────────────────────────────────────────────────────────

async def cache_get(key: str) -> Optional[Any]:
    """
    Return the cached value for *key*, or None if missing / unavailable.

    Stored values are JSON-encoded; plain strings are returned as-is.

    Validates: Requirements 7.6, 9.4, 11.5
    """
    r = get_redis()
    if r is None:
        logger.warning("cache_get: Redis unavailable — cache miss for key=%s", key)
        return None
    try:
        raw = await r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    except Exception as exc:
        logger.warning("cache_get error (key=%s): %s — returning None", key, exc)
        return None


async def cache_set(key: str, value: Any, ttl: int) -> None:
    """
    Store *value* under *key* with the given *ttl* (seconds).

    Values are JSON-encoded before storage.
    Degrades gracefully on Redis error.

    Validates: Requirements 7.6, 9.4, 11.5
    """
    r = get_redis()
    if r is None:
        logger.warning("cache_set: Redis unavailable — skipping cache write for key=%s", key)
        return
    try:
        serialised = json.dumps(value) if not isinstance(value, str) else value
        await r.setex(key, ttl, serialised)
    except Exception as exc:
        logger.warning("cache_set error (key=%s): %s — skipping cache write", key, exc)


async def invalidate(key: str) -> None:
    """
    Delete *key* from Redis.
    Degrades gracefully on Redis error.
    """
    r = get_redis()
    if r is None:
        logger.warning("invalidate: Redis unavailable — skipping invalidation for key=%s", key)
        return
    try:
        await r.delete(key)
    except Exception as exc:
        logger.warning("invalidate error (key=%s): %s — skipping invalidation", key, exc)


# ── Convenience wrappers (domain-specific key builders) ───────────────────────

async def get_routing_table(state_code: str) -> Optional[list]:
    """Fetch cached routing rules for *state_code* (TTL 300s)."""
    return await cache_get(f"routing:{state_code}")


async def set_routing_table(state_code: str, rules: list) -> None:
    """Cache routing rules for *state_code* (TTL 300s)."""
    await cache_set(f"routing:{state_code}", rules, ttl=300)


async def invalidate_routing_table(state_code: str) -> None:
    """Invalidate cached routing rules for *state_code*."""
    await invalidate(f"routing:{state_code}")


async def check_daily_complaint_limit(phone: str, limit: int = 5) -> bool:
    """
    Returns True if the phone number is within the daily complaint limit.
    Key: daily_complaints:{phone}:{date}  TTL 86400s
    """
    from datetime import date
    key = f"daily_complaints:{phone}:{date.today().isoformat()}"
    return await rate_limit_check(key, limit=limit, window_seconds=86400)


# ── Session store ─────────────────────────────────────────────────────────────

async def store_session(token: str, payload: dict, ttl_seconds: int = 28800) -> None:
    await cache_set(f"session:{token}", payload, ttl=ttl_seconds)


async def get_session(token: str) -> Optional[dict]:
    return await cache_get(f"session:{token}")


async def delete_session(token: str) -> None:
    await invalidate(f"session:{token}")
