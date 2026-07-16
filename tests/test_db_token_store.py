"""Tests for DbTokenStore, using a hand-written fake Repository/Redis and the real TokenCrypto (Fernet is
exercised directly, not mocked).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore, access_token_cache_key
from curator.persistence.repository import LinkRecord


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
    return TokenCrypto(Fernet.generate_key())


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


# ── load() ────────────────────────────────────────────────────────────────────


async def test_load_returns_none_when_no_row():
    store = DbTokenStore("sub-1", FakeRepository(), _make_crypto())
    assert await store.load() is None


async def test_load_returns_none_on_corrupt_ciphertext():
    repo = FakeRepository()
    repo.links["sub-1"] = LinkRecord(
        psn_account_id=None,
        token_response_enc=b"not-a-valid-fernet-token",
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
    )
    store = DbTokenStore("sub-1", repo, _make_crypto())

    assert await store.load() is None


async def test_load_returns_none_when_ciphertext_from_different_key():
    other_crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(other_crypto, {"refresh_token": "RT"})
    store = DbTokenStore("sub-1", repo, _make_crypto())

    assert await store.load() is None


async def test_load_returns_durable_fields_as_is_when_redis_not_configured():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"refresh_token": "RT", "scope": "psn:mobile.v2.core"})
    store = DbTokenStore("sub-1", repo, crypto)

    assert await store.load() == {"refresh_token": "RT", "scope": "psn:mobile.v2.core"}


async def test_load_merges_cached_access_token_with_durable_refresh_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"refresh_token": "RT"})
    redis = FakeRedis()
    redis.store[access_token_cache_key("sub-1")] = json.dumps(
        {"access_token": "AT", "access_token_expires_at": 1_900_000_000.0}
    )
    store = DbTokenStore("sub-1", repo, crypto, redis)

    result = await store.load()

    assert result == {"refresh_token": "RT", "access_token": "AT", "access_token_expires_at": 1_900_000_000.0}


async def test_load_falls_back_to_durable_only_on_redis_cache_miss():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"refresh_token": "RT"})
    store = DbTokenStore("sub-1", repo, crypto, FakeRedis())

    assert await store.load() == {"refresh_token": "RT"}


async def test_load_ignores_corrupt_cached_access_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"refresh_token": "RT"})
    redis = FakeRedis()
    redis.store[access_token_cache_key("sub-1")] = "not valid json"
    store = DbTokenStore("sub-1", repo, crypto, redis)

    assert await store.load() == {"refresh_token": "RT"}


async def test_load_returns_durable_dict_even_with_neither_access_nor_refresh_token():
    """No pre-emptive access_token gate on load() -- an all-but-empty durable dict (the rare case where
    even the refresh token is absent, e.g. right after a stale access-only session's cache entry expired)
    is still returned as-is. PsnSession's own _ensure_fresh()/_refresh() surfaces the real PsnAuthError
    when there is truly nothing usable, rather than DbTokenStore pre-emptively deciding via None."""
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"scope": "psn:mobile.v2.core"})
    store = DbTokenStore("sub-1", repo, crypto)

    assert await store.load() == {"scope": "psn:mobile.v2.core"}


# ── save() ────────────────────────────────────────────────────────────────────


async def test_save_no_op_when_dict_has_no_access_token():
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.save({"refresh_token": "RT"})

    assert repo.upsert_calls == []


async def test_save_no_op_when_access_token_falsy():
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.save({"access_token": "", "refresh_token": "RT"})

    assert repo.upsert_calls == []


async def test_save_strips_ephemeral_access_token_fields_from_the_encrypted_blob():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, crypto)
    token = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3599,
        "access_token_expires_at": 1_700_000_000.0,
        "refresh_token_expires_at": 1_800_000_000.0,
        "scope": "psn:mobile.v2.core",
    }

    await store.save(token)

    _, token_response_enc, _, _, _ = repo.upsert_calls[0]
    durable = json.loads(crypto.decrypt(token_response_enc))
    assert durable == {
        "refresh_token": "RT",
        "refresh_token_expires_at": 1_800_000_000.0,
        "scope": "psn:mobile.v2.core",
    }
    assert "access_token" not in durable
    assert "expires_in" not in durable
    assert "access_token_expires_at" not in durable


async def test_save_still_sets_the_sql_expiry_columns_even_though_the_blob_omits_access_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, crypto)
    token = {
        "access_token": "AT",
        "refresh_token": "RT",
        "access_token_expires_at": 1_700_000_000.0,
        "refresh_token_expires_at": 1_800_000_000.0,
    }

    await store.save(token)

    _, _, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert access_expires == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)
    assert refresh_expires == datetime.fromtimestamp(1_800_000_000.0, tz=timezone.utc)


async def test_save_persists_when_access_token_present_but_refresh_token_absent():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, crypto)
    token = {"access_token": "AT", "access_token_expires_at": 1_700_000_000.0}

    await store.save(token)

    assert len(repo.upsert_calls) == 1
    _, token_response_enc, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert json.loads(crypto.decrypt(token_response_enc)) == {}
    assert access_expires == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)
    assert refresh_expires is None


async def test_save_passes_none_expiries_when_keys_absent():
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.save({"access_token": "AT", "refresh_token": "RT"})

    _, _, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert access_expires is None
    assert refresh_expires is None


async def test_save_works_without_redis_configured():
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.save({"access_token": "AT", "refresh_token": "RT", "access_token_expires_at": time.time() + 3600})

    assert len(repo.upsert_calls) == 1


async def test_save_caches_the_access_token_in_redis_with_a_ttl_matching_its_remaining_lifetime():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore("sub-1", repo, _make_crypto(), redis)
    expires_at = time.time() + 3599
    token = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3599,
        "access_token_expires_at": expires_at,
        "id_token": "IDT",
        "token_type": "bearer",
        "scope": "psn:mobile.v2.core",
    }

    await store.save(token)

    assert len(redis.set_calls) == 1
    name, value, ex = redis.set_calls[0]
    assert name == access_token_cache_key("sub-1")
    cached = json.loads(value)
    assert cached == {
        "access_token": "AT",
        "expires_in": 3599,
        "access_token_expires_at": expires_at,
    }
    assert ex is not None
    assert 3595 <= ex <= 3599


async def test_save_skips_redis_cache_when_access_token_expires_at_missing():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore("sub-1", repo, _make_crypto(), redis)

    await store.save({"access_token": "AT", "refresh_token": "RT"})

    assert redis.set_calls == []


async def test_save_skips_redis_cache_when_access_token_already_expired():
    repo = FakeRepository()
    redis = FakeRedis()
    store = DbTokenStore("sub-1", repo, _make_crypto(), redis)

    await store.save(
        {"access_token": "AT", "refresh_token": "RT", "access_token_expires_at": time.time() - 10}
    )

    assert redis.set_calls == []


# ── clear() ───────────────────────────────────────────────────────────────────


async def test_clear_deletes_the_link():
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(_make_crypto(), {"refresh_token": "RT"})
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.clear()

    assert repo.delete_calls == ["sub-1"]


async def test_clear_also_deletes_the_cached_access_token():
    repo = FakeRepository()
    redis = FakeRedis()
    redis.store[access_token_cache_key("sub-1")] = json.dumps({"access_token": "AT"})
    store = DbTokenStore("sub-1", repo, _make_crypto(), redis)

    await store.clear()

    assert redis.delete_calls == [access_token_cache_key("sub-1")]
    assert access_token_cache_key("sub-1") not in redis.store


async def test_clear_without_redis_configured_only_deletes_the_row():
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(_make_crypto(), {"refresh_token": "RT"})
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.clear()

    assert repo.delete_calls == ["sub-1"]


def test_db_token_store_satisfies_async_token_store_contract_shape():
    # The folded-in PSN client's TokenStore contract is duck-typed: async load()/save(dict)/clear()
    # coroutine methods. Verify DbTokenStore exposes the same shape.
    import inspect

    store = DbTokenStore("sub-1", FakeRepository(), _make_crypto())

    assert inspect.iscoroutinefunction(store.load)
    assert inspect.iscoroutinefunction(store.save)
    assert inspect.iscoroutinefunction(store.clear)
