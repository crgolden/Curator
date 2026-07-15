"""Tests for DbTokenStore, using a hand-written fake Repository and the real TokenCrypto (Fernet is
exercised directly, not mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
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


async def test_load_happy_path_returns_decrypted_dict():
    crypto = _make_crypto()
    repo = FakeRepository()
    token = {"access_token": "AT", "refresh_token": "RT"}
    repo.links["sub-1"] = _encrypted_link(crypto, token)
    store = DbTokenStore("sub-1", repo, crypto)

    assert await store.load() == token


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


async def test_load_returns_dict_when_no_refresh_token_but_access_token_present():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"access_token": "AT"})
    store = DbTokenStore("sub-1", repo, crypto)

    assert await store.load() == {"access_token": "AT"}


async def test_load_returns_none_when_dict_has_no_access_token():
    crypto = _make_crypto()
    repo = FakeRepository()
    repo.links["sub-1"] = _encrypted_link(crypto, {"refresh_token": "RT"})
    store = DbTokenStore("sub-1", repo, crypto)

    assert await store.load() is None


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


async def test_save_persists_when_access_token_present_but_refresh_token_absent():
    crypto = _make_crypto()
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, crypto)
    token = {"access_token": "AT", "access_token_expires_at": 1_700_000_000.0}

    await store.save(token)

    assert len(repo.upsert_calls) == 1
    _, token_response_enc, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert json.loads(crypto.decrypt(token_response_enc)) == token
    assert access_expires == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)
    assert refresh_expires is None


async def test_save_persists_encrypted_token_and_converts_epochs_to_aware_datetimes():
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

    assert len(repo.upsert_calls) == 1
    sub, token_response_enc, access_expires, refresh_expires, psn_account_id = repo.upsert_calls[0]
    assert sub == "sub-1"
    assert psn_account_id is None
    assert json.loads(crypto.decrypt(token_response_enc)) == token
    assert access_expires == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)
    assert refresh_expires == datetime.fromtimestamp(1_800_000_000.0, tz=timezone.utc)
    assert access_expires.tzinfo is not None
    assert refresh_expires.tzinfo is not None


async def test_save_passes_none_expiries_when_keys_absent():
    repo = FakeRepository()
    store = DbTokenStore("sub-1", repo, _make_crypto())

    await store.save({"access_token": "AT", "refresh_token": "RT"})

    _, _, access_expires, refresh_expires, _ = repo.upsert_calls[0]
    assert access_expires is None
    assert refresh_expires is None


async def test_clear_deletes_the_link():
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
