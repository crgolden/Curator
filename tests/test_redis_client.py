"""Tests for build_redis_client (Settings -> optional Redis client) and RedisAdapter (narrows the real
client down to the exact surface curator.psn.rate_limiter.RedisLike/curator.psn.trophy_cache.RedisLike
declare), using a hand-written fake standing in for redis.asyncio.Redis's broader method shape.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from curator.persistence.config import ConfigError
from curator.redis_client import (
    _HEALTH_CHECK_INTERVAL_SECONDS,
    _RETRY_BACKOFF_BASE_SECONDS,
    _RETRY_COUNT,
    RedisAdapter,
    build_redis_client,
)
from curator.settings import Settings
from test_values import (
    lowercase_token,
    new_database_url,
    new_endpoint_url,
    new_port,
    new_positive_count,
    new_sha256_hash,
)


def _settings(**overrides) -> Settings:
    values: dict[str, Any] = dict(
        oidc_authority=new_endpoint_url(),
        token_key=lowercase_token(),
        database_url=new_database_url(),
        store_graphql_endpoint=new_endpoint_url(),
        store_query_hashes=(new_sha256_hash(),),
    )
    values.update(overrides)
    return Settings(**values)


def test_build_redis_client_returns_none_when_host_unset():
    assert build_redis_client(_settings()) is None


def test_build_redis_client_builds_real_client_when_host_set():
    client = build_redis_client(
        _settings(redis_host=lowercase_token(), redis_port=new_port(), redis_password=lowercase_token(), redis_ssl=True)
    )
    assert isinstance(client, Redis)


def test_build_redis_client_refuses_a_host_without_its_port_and_tls_flag():
    settings = _settings(redis_host=lowercase_token())

    with pytest.raises(ConfigError):
        build_redis_client(settings)


def test_build_redis_client_configures_connection_resilience():
    """A dropped/reset idle connection (production: `redis.exceptions.ConnectionError: ... Connection
    reset by peer` mid library-refresh job -- Azure SNAT reaps an idle flow, and Python's redis.asyncio
    fails fast where node-redis hung silently; AGENTS/OBSERVABILITY.md carries the mechanism) must be
    retried against a fresh connection rather than surfacing straight into the caller, and a
    long-idle pooled connection must be health-checked before reuse.
    """
    client = build_redis_client(_settings(redis_host=lowercase_token(), redis_port=new_port(), redis_ssl=True))
    assert client is not None
    connection_kwargs = client.connection_pool.connection_kwargs

    assert connection_kwargs["socket_keepalive"] is True
    assert connection_kwargs["health_check_interval"] == _HEALTH_CHECK_INTERVAL_SECONDS
    assert connection_kwargs["retry_on_error"] == [RedisConnectionError, RedisTimeoutError]

    retry = connection_kwargs["retry"]
    assert retry._retries == _RETRY_COUNT
    assert isinstance(retry._backoff, ExponentialBackoff)
    assert retry._backoff._base == _RETRY_BACKOFF_BASE_SECONDS
    assert retry._backoff._cap == 1.0


class FakeRawRedis:
    """Stands in for redis.asyncio.Redis's broader async surface (keyword names/param widths differ from
    curator.psn.*.RedisLike -- e.g. real ``expire`` takes ``time``, not ``seconds``) with call recording."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, name):
        return self.strings.get(name)

    async def set(self, name, value, ex=None):
        self.strings[name] = value
        self.set_calls.append((name, value, ex))
        return True

    async def zremrangebyscore(self, name, min, max):
        members = self.zsets.setdefault(name, {})
        to_remove = [member for member, score in members.items() if min <= score <= max]
        for member in to_remove:
            del members[member]
        return len(to_remove)

    async def zcard(self, name):
        return len(self.zsets.get(name, {}))

    async def zrange(self, name, start, end, withscores=False):
        ordered = sorted(self.zsets.get(name, {}).items(), key=lambda item: item[1])
        sliced = ordered[start : end + 1] if end >= 0 else ordered[start:]
        return sliced if withscores else [member for member, _ in sliced]

    async def zadd(self, name, mapping):
        self.zsets.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def expire(self, name, seconds):
        self.expire_calls.append((name, seconds))
        return True


async def test_adapter_get_set_round_trip():
    raw = FakeRawRedis()
    adapter = RedisAdapter(raw)

    key, value, ttl_seconds = lowercase_token(), lowercase_token(), new_positive_count()

    await adapter.set(key, value, ex=ttl_seconds)
    assert await adapter.get(key) == value
    assert raw.set_calls == [(key, value, ttl_seconds)]


async def test_adapter_zset_methods_pass_through():
    raw = FakeRawRedis()
    adapter = RedisAdapter(raw)

    key, lower_member, higher_member = lowercase_token(), lowercase_token(), lowercase_token()
    lower_score = float(new_positive_count())
    higher_score = lower_score + new_positive_count()
    members = {lower_member: lower_score, higher_member: higher_score}
    ttl_seconds = new_positive_count()

    assert await adapter.zadd(key, members) == len(members)
    assert await adapter.zcard(key) == len(members)
    assert await adapter.zrange(key, 0, -1) == [lower_member, higher_member]
    assert await adapter.zrange(key, 0, 0, withscores=True) == [(lower_member, lower_score)]
    assert await adapter.zremrangebyscore(key, 0, lower_score) == 1
    assert await adapter.zcard(key) == 1
    assert await adapter.expire(key, ttl_seconds) is True
    assert raw.expire_calls == [(key, ttl_seconds)]
