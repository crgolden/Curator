"""Tests for the Fernet -> AES-256-GCM re-key script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from curator.persistence.crypto import SCHEME_AES_GCM_V1, TokenCrypto

_SCRIPT = Path(__file__).resolve().parents[1] / "db" / "reencrypt_tokens.py"
_SPEC = importlib.util.spec_from_file_location("reencrypt_tokens", _SCRIPT)
assert _SPEC is not None
assert _SPEC.loader is not None
reencrypt_tokens = importlib.util.module_from_spec(_SPEC)
sys.modules["reencrypt_tokens"] = reencrypt_tokens
_SPEC.loader.exec_module(reencrypt_tokens)

Disposition = reencrypt_tokens.Disposition
classify = reencrypt_tokens.classify

SECRET = b'{"refresh_token": "sample-refresh-token-value", "scope": "psn:mobile.v2.core"}'


@pytest.fixture
def key() -> bytes:
    return TokenCrypto.generate_key()


def test_fernet_blob_is_recovered_so_the_rekey_can_rewrite_it(key: bytes) -> None:
    disposition, plaintext = classify(Fernet(key).encrypt(SECRET), TokenCrypto(key), Fernet(key))

    assert disposition is Disposition.REKEY
    assert plaintext == SECRET


def test_rekeyed_blob_decrypts_under_the_new_scheme(key: bytes) -> None:
    crypto = TokenCrypto(key)
    _, plaintext = classify(Fernet(key).encrypt(SECRET), crypto, Fernet(key))
    assert plaintext is not None

    assert crypto.decrypt(crypto.encrypt(plaintext)) == SECRET


def test_already_migrated_blob_is_left_alone_so_a_rerun_is_a_noop(key: bytes) -> None:
    crypto = TokenCrypto(key)

    disposition, plaintext = classify(crypto.encrypt(SECRET), crypto, Fernet(key))

    assert disposition is Disposition.ALREADY_AES
    assert plaintext is None


def test_scheme_byte_led_blob_is_left_alone_rather_than_failing_the_deploy(key: bytes) -> None:
    """The deploy gate is a third reader of these columns. Once either runtime writes the scheme byte, a
    versioned blob must classify as already-migrated -- classifying it UNREADABLE would exit non-zero and
    block every subsequent deploy."""
    crypto = TokenCrypto(key)
    versioned = crypto.encrypt(SECRET)

    disposition, plaintext = classify(versioned, crypto, Fernet(key))

    assert versioned[0] == SCHEME_AES_GCM_V1
    assert disposition is Disposition.ALREADY_AES
    assert plaintext is None


def test_blob_written_under_a_different_key_is_reported_not_dropped(key: bytes) -> None:
    foreign = Fernet(TokenCrypto.generate_key()).encrypt(SECRET)

    disposition, plaintext = classify(foreign, TokenCrypto(key), Fernet(key))

    assert disposition is Disposition.UNREADABLE
    assert plaintext is None


def test_empty_blob_is_not_mistaken_for_corruption(key: bytes) -> None:
    disposition, plaintext = classify(b"", TokenCrypto(key), Fernet(key))

    assert disposition is Disposition.EMPTY
    assert plaintext is None
