"""Redis-backed cache for trophy summary/title-list reads.

Trophy progress is time-decaying, current-state data: a short TTL self-heals staleness with no explicit
invalidation path needed, unlike the durable Postgres caches (RAWG/OpenCritic/PSN catalog) that protect
genuinely scarce external quota (see ``db/migrations/0001_initial.sql``'s header comment). Deliberately
NOT a Postgres table for that reason -- see the migration plan's "Infrastructure services used" section.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from curator.psn.models import TrophyCounts, TrophySummary, TrophyTitle

if TYPE_CHECKING:
    from curator.psn.trophy_client import TrophyClient

DEFAULT_TTL_SECONDS = 15 * 60


class RedisLike(Protocol):
    """The narrow slice of ``redis.asyncio.Redis``'s string API this cache needs."""

    async def get(self, name: str) -> bytes | str | None:
        """Return the cached value, or ``None`` if absent/expired."""
        ...

    async def set(self, name: str, value: str, ex: int | None = None) -> Any:
        """Set a value with an optional TTL (seconds)."""
        ...


def _counts_to_dict(counts: TrophyCounts) -> dict[str, int]:
    return {"bronze": counts.bronze, "silver": counts.silver, "gold": counts.gold, "platinum": counts.platinum}


def _counts_from_dict(data: dict[str, Any]) -> TrophyCounts:
    return TrophyCounts(
        bronze=data.get("bronze", 0),
        silver=data.get("silver", 0),
        gold=data.get("gold", 0),
        platinum=data.get("platinum", 0),
    )


def _summary_to_json(summary: TrophySummary) -> str:
    return json.dumps(
        {
            "level": summary.level,
            "progress": summary.progress,
            "tier": summary.tier,
            "earned": _counts_to_dict(summary.earned),
            "account_id": summary.account_id,
        }
    )


def _summary_from_json(raw: str) -> TrophySummary:
    data = json.loads(raw)
    return TrophySummary(
        level=data["level"],
        progress=data["progress"],
        tier=data["tier"],
        earned=_counts_from_dict(data["earned"]),
        account_id=data.get("account_id"),
    )


def _title_to_dict(title: TrophyTitle) -> dict[str, Any]:
    return {
        "name": title.name,
        "np_communication_id": title.np_communication_id,
        "platforms": list(title.platforms),
        "progress": title.progress,
        "earned": _counts_to_dict(title.earned),
        "defined": _counts_to_dict(title.defined),
        "last_updated": title.last_updated,
    }


def _title_from_dict(data: dict[str, Any]) -> TrophyTitle:
    return TrophyTitle(
        name=data.get("name"),
        np_communication_id=data.get("np_communication_id"),
        platforms=tuple(data.get("platforms") or ()),
        progress=data.get("progress"),
        earned=_counts_from_dict(data["earned"]),
        defined=_counts_from_dict(data["defined"]),
        last_updated=data.get("last_updated"),
    )


def _decode(cached: bytes | str) -> str:
    return cached if isinstance(cached, str) else cached.decode()


def _cache_key(kind: str, online_id: str | None, account_id: str | None) -> str:
    return f"curator:psn:trophy:{kind}:{online_id or '-'}:{account_id or '-'}"


class CachedTrophyClient:
    """Wraps a :class:`~curator.psn.trophy_client.TrophyClient`, caching its two most-repeated reads
    (``trophy_summary``/``trophy_titles``) in Redis with a short TTL. Every other method passes through
    uncached (per-title trophy detail, presence-adjacent data, etc. don't repeat often enough to justify it).

    :param client: The underlying trophy client.
    :param redis: An async Redis client (``redis.asyncio.Redis`` or a hand-written fake satisfying
        :class:`RedisLike`).
    :param ttl_seconds: The cache TTL; defaults to PSN's own conservative rate-limit window (15 minutes).
    """

    def __init__(self, client: TrophyClient, redis: RedisLike, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._client = client
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def trophy_summary(self, online_id: str | None = None, account_id: str | None = None) -> TrophySummary:
        """Cached :meth:`~curator.psn.trophy_client.TrophyClient.trophy_summary`."""
        key = _cache_key("summary", online_id, account_id)
        cached = await self._redis.get(key)
        if cached is not None:
            return _summary_from_json(_decode(cached))
        summary = await self._client.trophy_summary(online_id, account_id)
        await self._redis.set(key, _summary_to_json(summary), ex=self._ttl_seconds)
        return summary

    async def trophy_titles(
        self,
        online_id: str | None = None,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[TrophyTitle]:
        """Cached :meth:`~curator.psn.trophy_client.TrophyClient.trophy_titles`."""
        key = _cache_key(f"titles:{limit}", online_id, account_id)
        cached = await self._redis.get(key)
        if cached is not None:
            return [_title_from_dict(item) for item in json.loads(_decode(cached))]
        titles = await self._client.trophy_titles(online_id, account_id, limit)
        await self._redis.set(key, json.dumps([_title_to_dict(title) for title in titles]), ex=self._ttl_seconds)
        return titles
