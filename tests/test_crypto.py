"""Tests for TokenCrypto, using real Fernet keys (no mocking of the cryptography library)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from curator.persistence.config import ConfigError
from curator.persistence.crypto import TokenCrypto


def test_encrypt_decrypt_round_trip():
    key = Fernet.generate_key()
    crypto = TokenCrypto(key)

    plaintext = b'{"access_token": "AT", "refresh_token": "RT"}'
    encrypted = crypto.encrypt(plaintext)

    assert encrypted != plaintext
    assert crypto.decrypt(encrypted) == plaintext


def test_decrypt_wrong_key_raises_invalid_token():
    crypto_a = TokenCrypto(Fernet.generate_key())
    crypto_b = TokenCrypto(Fernet.generate_key())

    encrypted = crypto_a.encrypt(b"secret")

    with pytest.raises(InvalidToken):
        crypto_b.decrypt(encrypted)


def test_from_config_prefers_explicit_key():
    explicit_key = Fernet.generate_key()
    crypto = TokenCrypto.from_config(explicit_key.decode("ascii"))

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_reads_env_var(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    monkeypatch.setenv("CURATOR_TOKEN_KEY", key.decode("ascii"))

    crypto = TokenCrypto.from_config(dotenv_path=tmp_path / "absent.env")

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_TOKEN_KEY", raising=False)
    key = Fernet.generate_key()
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"CURATOR_TOKEN_KEY={key.decode('ascii')}\n", encoding="utf-8")

    crypto = TokenCrypto.from_config(dotenv_path=dotenv)

    encrypted = crypto.encrypt(b"data")
    assert crypto.decrypt(encrypted) == b"data"


def test_from_config_missing_raises_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_TOKEN_KEY", raising=False)
    with pytest.raises(ConfigError):
        TokenCrypto.from_config(dotenv_path=tmp_path / "absent.env")
