"""Tests for DbTokenStore, using a hand-written fake Repository/Redis and the real TokenCrypto (AES-256-GCM
is exercised directly, not mocked).
"""

from __future__ import annotations

import inspect
import json
import random
import time
from datetime import datetime, timezone

from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore, access_token_cache_key
from curator.persistence.repository import LinkRecord
from curator.token_response import (
    ACCESS_TOKEN_EXPIRES_AT_KEY,
    ACCESS_TOKEN_KEY,
    EXPIRES_IN_KEY,
    REFRESH_TOKEN_EXPIRES_AT_KEY,
    REFRESH_TOKEN_KEY,
    SCOPE_KEY,
    TOKEN_TYPE_KEY,
)
from test_values import lowercase_token, new_identity_sub, new_opaque_token, new_utc_instant

SUB = new_identity_sub()


class FakeRepository:
    """Stands in for Repository: an in-memory dict of sub -> LinkRecord, with call recording."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}
        self.upsert_calls: list[tuple] = []
        self.delete_calls: list[str] = []

    async def get_link(self, sub):
        return self.links.get(sub)

    async def upsert_link(
        self, sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id=None
    ):
        self.upsert_calls.append(
            (sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id)
        )
        self.links[sub] = LinkRecord(
            psn_account_id=psn_account_id,
            token_response_enc=token_response_enc,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
            linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_verified_at=None,
        )

    async def delete_link(self, sub):
        self.delete_calls.append(sub)
        self.links.pop(sub, None)


class FakeRedis:
    """Stands in for the narrow RedisLike protocol: an in-memory string store, with call recording."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.delete_calls: list[str] = []

    async def get(self, name):
        return self.store.get(name)

    async def set(self, name, value, ex=None):
        self.store[name] = value
        self.set_calls.append((name, value, ex))

    async def delete(self, name):
        self.delete_calls.append(name)
        self.store.pop(name, None)


def _make_crypto() -> TokenCrypto:
    return TokenCrypto(TokenCrypto.generate_key())


def _encrypted_link(crypto: TokenCrypto, payload: dict) -> LinkRecord:
    encrypted = crypto.encrypt(json.dumps(payload).encode("utf-8"))
    return LinkRecord(
        psn_account_id=None,
        token_response_enc=encrypted,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
    )


def _scope() -> str:
    return f"{lowercase_token()}:{lowercase_token()}"


def _epoch_seconds() -> float:
    return new_utc_instant().timestamp()


def _lifetime_seconds() -> int:
    return random.randint(600, 7200)


async def test_load_returns_none_when_no_row():
    store = DbTokenStore(SUB, FakeRepository(), _make_crypto())
    assert await store.load() is None


async def test_load_returns_none_on_corrupt_ciphertext():
    repo = FakeRepository()
    repo.links[SUB] = LinkRecord(
        psn_account_id=None,
        token_response_enc=new_opaque_token().encode(),
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
    )
    store = DbTokenStore(SUB, repo, _make_crypto())

    assert await store.load() is None


async def test_load_returns_none_when_ciphertext_from_different_key():
    other_crypto = _make_crypto()
    repo = FakeRepository()
    repo.links[SUB] = _encrypted_link(other_crypto, {REFRESH_TOKEN_KEY: new_opaque_token()})
    store = DbTokenStore(SUB, repo, _make_crypto())

    assert await store.load() is None


async def test_load_returns_durable_fields_as_is_when_redis_not_configured():
    crypto = _make_crypto()
    repo = FakeRepository()
    durable = {REFRESH_TOKEN_KEY: new_opaque_token(), SCOPE_KEY: _scope()}
    repo.links[SUB] = _encrypted_link(crypto, durable)
    store = DbTokenStore(SUB, repo, crypto)

    assert await store.load() == durable


async def test_load_reads_a_two_key_blob_written_by_the_worker_runtime():
    """The worker persists only ``refresh_token`` and ``refresh_token_expires_at``, dropping the other
    non-ephemeral keys this runtime happens to store. Both runtimes read the same ``psn_links`` row, so
    the narrower shape has to round-trip here unchanged."""
    crypto = _make_crypto()
    repo = FakeRepository()
    worker_blob = {
        REFRESH_TOKEN_KEY: new_opaque_token(),
        REFRESH_TOKEN_EXPIRES_AT_KEY: time.time() + random.randint(86_400, 5_184_000),
    }
    repo.links[SUB] = _encrypted_link(crypto, worker_blob)
    store = DbTokenStore(SUB, repo, crypto)

    assert await store.load() == worker_blob


async def test_save_round_trips_through_the_two_key_shape_the_worker_writes():
    """A token response saved here, reduced to the worker's two durable keys, must still load as a
    usable session -- ``PsnSession.restore`` only requires ``refresh_token`` to be present."""
    crypto = _make_crypto()
    repo = FakeRepository()
    refresh_token = new_opaque_token()
    refresh_expires_at = time.time() + random.randint(86_400, 5_184_000)
    lifetime = _lifetime_seconds()
    store = DbTokenStore(SUB, repo, crypto)

    await store.save(
        {
            ACCESS_TOKEN_KEY: new_opaque_token(),
            EXPIRES_IN_KEY: lifetime,
            ACCESS_TOKEN_EXPIRES_AT_KEY: time.time() + lifetime,
            REFRESH_TOKEN_KEY: refresh_token,
            REFRESH_TOKEN_EXPIRES_AT_KEY: refresh_expires_at,
        }
    )

    assert await store.load() == {REFRESH_TOKEN_KEY: refresh_token, REFRESH_TOKEN_EXPIRES_AT_KEY: refresh_expires_at}


async def test_load_merges_cached_access_token_with_durable_refresh_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    durable = {REFRESH_TOKEN_KEY: new_opaque_token()}
    repo.links[SUB] = _encrypted_link(crypto, durable)
    redis = FakeRedis()
    cached = {ACCESS_TOKEN_KEY: new_opaque_token(), ACCESS_TOKEN_EXPIRES_AT_KEY: _epoch_seconds()}
    redis.store[access_token_cache_key(SUB)] = json.dumps(cached)
    store = DbTokenStore(SUB, repo, crypto, redis)

    assert await store.load() == {**durable, **cached}


async def test_load_falls_back_to_durable_only_on_redis_cache_miss():
    crypto = _make_crypto()
    repo = FakeRepository()
    durable = {REFRESH_TOKEN_KEY: new_opaque_token()}
    repo.links[SUB] = _encrypted_link(crypto, durable)
    store = DbTokenStore(SUB, repo, crypto, FakeRedis())

    assert await store.load() == durable


async def test_load_ignores_corrupt_cached_access_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    durable = {REFRESH_TOKEN_KEY: new_opaque_token()}
    repo.links[SUB] = _encrypted_link(crypto, durable)
    redis = FakeRedis()
    redis.store[access_token_cache_key(SUB)] = new_opaque_token()
    store = DbTokenStore(SUB, repo, crypto, redis)

    assert await store.load() == durable


async def test_load_returns_durable_dict_even_with_neither_access_nor_refresh_token():
    """No pre-emptive access_token gate on load() -- an all-but-empty durable dict (the rare case where
    even the refresh token is absent, e.g. right after a stale access-only session's cache entry expired)
    is still returned as-is. PsnSession's own _ensure_fresh()/_refresh() surfaces the real PsnAuthError
    when there is truly nothing usable, rather than DbTokenStore pre-emptively deciding via None."""
    crypto = _make_crypto()
    repo = FakeRepository()
    durable = {SCOPE_KEY: _scope()}
    repo.links[SUB] = _encrypted_link(crypto, durable)
    store = DbTokenStore(SUB, repo, crypto)

    assert await store.load() == durable


async def test_save_no_op_when_dict_has_no_access_token():
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.save({REFRESH_TOKEN_KEY: new_opaque_token()})

    assert repo.upsert_calls == []


async def test_save_no_op_when_access_token_falsy():
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.save({ACCESS_TOKEN_KEY: None, REFRESH_TOKEN_KEY: new_opaque_token()})

    assert repo.upsert_calls == []


async def test_save_strips_ephemeral_access_token_fields_from_the_encrypted_blob():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, crypto)
    durable = {
        REFRESH_TOKEN_KEY: new_opaque_token(),
        REFRESH_TOKEN_EXPIRES_AT_KEY: _epoch_seconds(),
        SCOPE_KEY: _scope(),
    }
    ephemeral = {
        ACCESS_TOKEN_KEY: new_opaque_token(),
        EXPIRES_IN_KEY: _lifetime_seconds(),
        ACCESS_TOKEN_EXPIRES_AT_KEY: _epoch_seconds(),
    }

    await store.save({**ephemeral, **durable})

    _, token_response_enc, _, _, _ = repo.upsert_calls[0]
    assert json.loads(crypto.decrypt(token_response_enc)) == durable


async def test_save_still_sets_the_sql_expiry_columns_even_though_the_blob_omits_access_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, crypto)
    access_expires_at, refresh_expires_at = _epoch_seconds(), _epoch_seconds()

    await store.save(
        {
            ACCESS_TOKEN_KEY: new_opaque_token(),
            REFRESH_TOKEN_KEY: new_opaque_token(),
            ACCESS_TOKEN_EXPIRES_AT_KEY: access_expires_at,
            REFRESH_TOKEN_EXPIRES_AT_KEY: refresh_expires_at,
        }
    )

    _, _, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert access_expires == datetime.fromtimestamp(access_expires_at, tz=timezone.utc)
    assert refresh_expires == datetime.fromtimestamp(refresh_expires_at, tz=timezone.utc)


async def test_save_persists_when_access_token_present_but_refresh_token_absent():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, crypto)
    access_expires_at = _epoch_seconds()

    await store.save({ACCESS_TOKEN_KEY: new_opaque_token(), ACCESS_TOKEN_EXPIRES_AT_KEY: access_expires_at})

    assert len(repo.upsert_calls) == 1
    _, token_response_enc, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert json.loads(crypto.decrypt(token_response_enc)) == {}
    assert access_expires == datetime.fromtimestamp(access_expires_at, tz=timezone.utc)
    assert refresh_expires is None


async def test_save_passes_none_expiries_when_keys_absent():
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.save({ACCESS_TOKEN_KEY: new_opaque_token(), REFRESH_TOKEN_KEY: new_opaque_token()})

    _, _, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert access_expires is None
    assert refresh_expires is None


async def test_save_works_without_redis_configured():
    repo = FakeRepository()
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.save(
        {
            ACCESS_TOKEN_KEY: new_opaque_token(),
            REFRESH_TOKEN_KEY: new_opaque_token(),
            ACCESS_TOKEN_EXPIRES_AT_KEY: time.time() + _lifetime_seconds(),
        }
    )

    assert len(repo.upsert_calls) == 1


async def test_save_caches_the_access_token_in_redis_with_a_ttl_matching_its_remaining_lifetime():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore(SUB, repo, _make_crypto(), redis)
    lifetime = _lifetime_seconds()
    ephemeral = {
        ACCESS_TOKEN_KEY: new_opaque_token(),
        EXPIRES_IN_KEY: lifetime,
        ACCESS_TOKEN_EXPIRES_AT_KEY: time.time() + lifetime,
    }

    await store.save(
        {
            **ephemeral,
            REFRESH_TOKEN_KEY: new_opaque_token(),
            lowercase_token(): new_opaque_token(),
            TOKEN_TYPE_KEY: lowercase_token(),
            SCOPE_KEY: _scope(),
        }
    )

    assert len(redis.set_calls) == 1
    name, value, ex = redis.set_calls[0]
    assert name == access_token_cache_key(SUB)
    assert json.loads(value) == ephemeral
    assert ex is not None
    assert lifetime - 4 <= ex <= lifetime


async def test_save_skips_redis_cache_when_access_token_expires_at_missing():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore(SUB, repo, _make_crypto(), redis)

    await store.save({ACCESS_TOKEN_KEY: new_opaque_token(), REFRESH_TOKEN_KEY: new_opaque_token()})

    assert redis.set_calls == []


async def test_save_skips_redis_cache_when_access_token_already_expired():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore(SUB, repo, _make_crypto(), redis)

    await store.save(
        {
            ACCESS_TOKEN_KEY: new_opaque_token(),
            REFRESH_TOKEN_KEY: new_opaque_token(),
            ACCESS_TOKEN_EXPIRES_AT_KEY: _epoch_seconds(),
        }
    )

    assert redis.set_calls == []


async def test_clear_deletes_the_link():
    repo = FakeRepository()
    repo.links[SUB] = _encrypted_link(_make_crypto(), {REFRESH_TOKEN_KEY: new_opaque_token()})
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.clear()

    assert repo.delete_calls == [SUB]


async def test_clear_also_deletes_the_cached_access_token():
    repo = FakeRepository()
    redis = FakeRedis()
    redis.store[access_token_cache_key(SUB)] = json.dumps({ACCESS_TOKEN_KEY: new_opaque_token()})
    store = DbTokenStore(SUB, repo, _make_crypto(), redis)

    await store.clear()

    assert redis.delete_calls == [access_token_cache_key(SUB)]
    assert access_token_cache_key(SUB) not in redis.store


async def test_clear_without_redis_configured_only_deletes_the_row():
    repo = FakeRepository()
    repo.links[SUB] = _encrypted_link(_make_crypto(), {REFRESH_TOKEN_KEY: new_opaque_token()})
    store = DbTokenStore(SUB, repo, _make_crypto())

    await store.clear()

    assert repo.delete_calls == [SUB]


def test_db_token_store_satisfies_async_token_store_contract_shape():
    store = DbTokenStore(SUB, FakeRepository(), _make_crypto())

    assert inspect.iscoroutinefunction(store.load)
    assert inspect.iscoroutinefunction(store.save)
    assert inspect.iscoroutinefunction(store.clear)
