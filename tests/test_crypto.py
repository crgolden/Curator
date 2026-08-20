"""Tests for TokenCrypto, using real AES-256-GCM keys (no mocking of the cryptography library)."""

from __future__ import annotations

import pytest

from curator.persistence.config import ConfigError
from curator.persistence.crypto import InvalidToken, TokenCrypto


def test_encrypt_decrypt_round_trip():
    key = TokenCrypto.generate_key()
    crypto = TokenCrypto(key)

    plaintext = b'{"access_token": "AT", "refresh_token": "RT"}'
    encrypted = crypto.encrypt(plaintext)

    assert encrypted != plaintext
    assert crypto.decrypt(encrypted) == plaintext


def test_decrypt_wrong_key_raises_invalid_token():
    crypto_a = TokenCrypto(TokenCrypto.generate_key())
    crypto_b = TokenCrypto(TokenCrypto.generate_key())

    encrypted = crypto_a.encrypt(b"secret")

    with pytest.raises(InvalidToken):
        crypto_b.decrypt(encrypted)


def test_decrypt_too_short_raises_invalid_token():
    crypto = TokenCrypto(TokenCrypto.generate_key())

    with pytest.raises(InvalidToken):
        crypto.decrypt(b"short")


def test_decrypt_tampered_ciphertext_raises_invalid_token():
    crypto = TokenCrypto(TokenCrypto.generate_key())
    encrypted = bytearray(crypto.encrypt(b"secret"))
    encrypted[-1] ^= 0xFF

    with pytest.raises(InvalidToken):
        crypto.decrypt(bytes(encrypted))


def test_from_config_prefers_explicit_key():
    explicit_key = TokenCrypto.generate_key()
    crypto = TokenCrypto.from_config(explicit_key.decode("ascii"))

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_reads_env_var(monkeypatch, tmp_path):
    key = TokenCrypto.generate_key()
    monkeypatch.setenv("CURATOR_TOKEN_KEY", key.decode("ascii"))

    crypto = TokenCrypto.from_config(dotenv_path=tmp_path / "absent.env")

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_TOKEN_KEY", raising=False)
    key = TokenCrypto.generate_key()
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"CURATOR_TOKEN_KEY={key.decode('ascii')}\n", encoding="utf-8")

    crypto = TokenCrypto.from_config(dotenv_path=dotenv)

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_missing_raises_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_TOKEN_KEY", raising=False)
    with pytest.raises(ConfigError):
        TokenCrypto.from_config(dotenv_path=tmp_path / "absent.env")
