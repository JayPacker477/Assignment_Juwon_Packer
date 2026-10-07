"""Shared async HTTP client with a small in-memory TTL cache.

Caching keeps repeat demo queries fast and keeps us polite to free public APIs.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from . import config

_cache: dict[str, tuple[float, Any]] = {}
_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT_S,
            headers={"User-Agent": config.USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get(url: str, params: Optional[dict], kind: str, ttl: int) -> Any:
    key = f"{kind}:{url}?{json.dumps(params, sort_keys=True)}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    resp = await client().get(url, params=params)
    resp.raise_for_status()
    value = resp.json() if kind == "json" else resp.text
    _cache[key] = (time.monotonic(), value)
    return value


async def get_json(url: str, params: Optional[dict] = None, ttl: int = config.CACHE_TTL_S) -> Any:
    return await _get(url, params, "json", ttl)


async def get_text(url: str, params: Optional[dict] = None, ttl: int = config.CACHE_TTL_S) -> str:
    return await _get(url, params, "text", ttl)
