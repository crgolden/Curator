"""Tests for TokenCrypto, using real AES-256-GCM keys (no mocking of the cryptography library)."""

from __future__ import annotations

import base64
import os
import uuid

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from curator.persistence.config import ConfigError
from curator.persistence.crypto import SCHEME_AES_GCM_V1, InvalidToken, TokenCrypto

NONCE_SIZE_BYTES = 12
TAG_SIZE_BYTES = 16
KEY_SIZE_BYTES = 32
SCHEME_SIZE_BYTES = 1
UNVERSIONED_FRAMING_OVERHEAD_BYTES = NONCE_SIZE_BYTES + TAG_SIZE_BYTES
VERSIONED_FRAMING_OVERHEAD_BYTES = SCHEME_SIZE_BYTES + UNVERSIONED_FRAMING_OVERHEAD_BYTES

NON_COLLIDING_FIRST_NONCE_BYTE = 0x00

DOTNET_GENERATED_KEY = "m5zFCu2PmtB-58aRqME3ylgoFnBxzcCB0JcWvqCkeYc="
DOTNET_GENERATED_UNVERSIONED_TOKEN_BASE64 = (
    "4mmoUfQvAYZ0hpHteWDhljj9hZQWpM86WLxUUVVxzXcPcESEMwQK4rFiyBWN4Bvr6yPjPA5Srqse5Ano"
    "+Zwhyz/idtsI9V/dnwLj/1zbkVVh0JcYIr4pf5oqftuK6y75LKxyPsRfJE4NKw=="
)
DOTNET_GENERATED_VERSIONED_TOKEN_BASE64 = (
    "ATfDtPHIbTRG5gSKhOUWEcqlvHMi8OG8RgAgdbWaMm8gIv7EyZQADsFLt4Y//d2UrLr1yyTh2u75Phee"
    "uxNJGwOZ4NBwi6a8rS1LBouTXYP5Uhb8jr9e6qBhkk+o20caug96SpIcnBffqyQ="
)
DOTNET_GENERATED_PLAINTEXT = '{"refresh_token": "sample-refresh-token-value", "scope": "psn:mobile.v2.core"}'


def new_secret() -> bytes:
    return f"secret-{uuid.uuid4().hex}".encode()


def new_raw_key() -> bytes:
    return os.urandom(KEY_SIZE_BYTES)


def to_base64url(raw_key: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw_key)


def prepend_scheme_byte(scheme: int, unversioned: bytes) -> bytes:
    return bytes([scheme]) + unversioned


def unversioned_token(raw_key: bytes, plaintext: bytes, first_nonce_byte: int) -> bytes:
    """Build a token in the pre-scheme-byte framing, pinning the nonce's first byte.

    Mirrors ``TokenCryptoTests.UnversionedToken`` so both runtimes prove the collision case the same way.
    """
    nonce = bytes([first_nonce_byte]) + os.urandom(NONCE_SIZE_BYTES - 1)
    return nonce + AESGCM(raw_key).encrypt(nonce, plaintext, None)


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


@pytest.mark.parametrize("stray", [b"\n", b" ", b"!"])
def test_a_key_carrying_a_stray_character_is_rejected_rather_than_silently_accepted(stray: bytes):
    """The stray replaces a character rather than being inserted, so the key stays a valid base64 length
    and the alphabet check is what rejects it -- not the length check."""
    key = TokenCrypto.generate_key()
    with_a_stray_character = key[:8] + stray + key[9:]

    with pytest.raises(ValueError, match="not valid base64url"):
        TokenCrypto(with_a_stray_character)


def test_an_unpadded_key_decodes_to_the_same_bytes_the_padded_form_does():
    """The .NET port pads before decoding, so rejecting an unpadded key here would strand a deployment on
    whichever runtime is stricter. Both accept it, and both must land on the same 32 bytes."""
    padded = TokenCrypto.generate_key()
    unpadded = padded.rstrip(b"=")

    plaintext = b"parity"
    assert TokenCrypto(unpadded).decrypt(TokenCrypto(padded).encrypt(plaintext)) == plaintext


def test_a_key_one_character_past_a_multiple_of_four_is_rejected():
    """No base64 string has this length, and the .NET port rejects it too rather than padding it to a
    length that decodes."""
    over_padded = TokenCrypto.generate_key() + b"="

    with pytest.raises(ValueError, match="not valid base64url"):
        TokenCrypto(over_padded)


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


def test_decrypt_reads_a_versioned_token_when_the_scheme_byte_leads_the_unversioned_framing():
    raw_key = new_raw_key()
    crypto = TokenCrypto(to_base64url(raw_key))
    plaintext = new_secret()

    versioned = prepend_scheme_byte(
        SCHEME_AES_GCM_V1, unversioned_token(raw_key, plaintext, NON_COLLIDING_FIRST_NONCE_BYTE)
    )

    assert crypto.decrypt(versioned) == plaintext


def test_decrypt_still_reads_an_unversioned_token_when_its_first_nonce_byte_collides_with_the_scheme_byte():
    raw_key = new_raw_key()
    crypto = TokenCrypto(to_base64url(raw_key))
    plaintext = new_secret()

    colliding = unversioned_token(raw_key, plaintext, SCHEME_AES_GCM_V1)

    assert colliding[0] == SCHEME_AES_GCM_V1
    assert crypto.decrypt(colliding) == plaintext


def test_decrypt_still_reads_the_shortest_possible_unversioned_token_that_collides_with_the_scheme_byte():
    """The 28-byte boundary: an empty plaintext is one byte too short to be a versioned token.

    A cross-port parity pin, not a discrimination proof. Python reaches the right answer either way --
    ``AESGCM.decrypt`` raises ``InvalidTag`` at every undersized length, so the probe's own catch absorbs a
    mis-sliced body. Its .NET twin, ``TokenCryptoTests
    .Decrypt_StillReadsTheShortestUnversionedToken_WhenItsFirstNonceByteCollidesWithTheSchemeByte``, is the
    one that discriminates: ``TokenCrypto.DecryptNonceCiphertextTag`` slices the tag explicitly and throws
    ``ArgumentOutOfRangeException`` -- not a ``CryptographicException``, so no caller catches it.
    """
    raw_key = new_raw_key()
    crypto = TokenCrypto(to_base64url(raw_key))

    colliding = unversioned_token(raw_key, b"", SCHEME_AES_GCM_V1)

    assert len(colliding) == UNVERSIONED_FRAMING_OVERHEAD_BYTES
    assert colliding[0] == SCHEME_AES_GCM_V1
    assert crypto.decrypt(colliding) == b""


def test_decrypt_tampered_versioned_token_raises_invalid_token():
    crypto = TokenCrypto(TokenCrypto.generate_key())
    versioned = bytearray(crypto.encrypt(new_secret()))
    versioned[-1] ^= 0xFF

    with pytest.raises(InvalidToken):
        crypto.decrypt(bytes(versioned))


def test_encrypt_writes_the_versioned_framing_led_by_the_scheme_byte():
    crypto = TokenCrypto(TokenCrypto.generate_key())
    plaintext = new_secret()

    token = crypto.encrypt(plaintext)

    assert len(token) == len(plaintext) + VERSIONED_FRAMING_OVERHEAD_BYTES
    assert token[0] == SCHEME_AES_GCM_V1


def test_decrypt_reads_an_unversioned_token_encrypted_by_functions_dotnet_token_crypto():
    crypto = TokenCrypto(DOTNET_GENERATED_KEY.encode("ascii"))

    plaintext = crypto.decrypt(base64.b64decode(DOTNET_GENERATED_UNVERSIONED_TOKEN_BASE64))

    assert plaintext.decode("utf-8") == DOTNET_GENERATED_PLAINTEXT


def test_decrypt_reads_a_versioned_token_encrypted_by_functions_dotnet_token_crypto():
    crypto = TokenCrypto(DOTNET_GENERATED_KEY.encode("ascii"))

    plaintext = crypto.decrypt(base64.b64decode(DOTNET_GENERATED_VERSIONED_TOKEN_BASE64))

    assert plaintext.decode("utf-8") == DOTNET_GENERATED_PLAINTEXT
