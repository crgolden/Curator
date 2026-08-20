"""Symmetric encryption for tokens at rest.

``psn_links.token_response_enc`` and ``user_enrichment_keys.{rawg,opencritic}_api_key_enc`` store each
user's secret material encrypted with a single application-wide key -- the database is never trusted alone
to keep a secret. :class:`TokenCrypto` wraps AES-256-GCM (``cryptography.hazmat.primitives.ciphers.aead
.AESGCM``, the same audited library this project already depends on -- not a hand-rolled cipher), resolving
the key the same arg -> env var -> ``.env`` way every other Curator setting resolves (see
:mod:`curator.persistence.config`).

Wire format: ``nonce(12 bytes) || ciphertext || tag(16 bytes)`` -- AESGCM.encrypt already appends the tag
to the ciphertext it returns, so :meth:`TokenCrypto.encrypt` only has to prepend the nonce it generated.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from curator.persistence.config import ConfigError, resolve_setting

DEFAULT_ENV_NAMES: tuple[str, ...] = ("CURATOR_TOKEN_KEY",)

_KEY_SIZE_BYTES = 32
_NONCE_SIZE_BYTES = 12


class InvalidToken(Exception):
    """Raised when :meth:`TokenCrypto.decrypt` is given ciphertext that's corrupt, tampered with, too
    short to contain a nonce, or was encrypted under a different key."""


class TokenCrypto:
    """Encrypts and decrypts bytes with an AES-256-GCM key.

    :param key: The key, base64url-encoded (the same encoding :meth:`generate_key` returns), matching how
        every caller stores it in ``CURATOR_TOKEN_KEY``.
    :raises ValueError: If ``key`` doesn't decode to exactly 32 raw bytes.
    """

    def __init__(self, key: bytes) -> None:
        raw_key = base64.urlsafe_b64decode(key)
        if len(raw_key) != _KEY_SIZE_BYTES:
            raise ValueError(f"TokenCrypto key must decode to {_KEY_SIZE_BYTES} bytes, got {len(raw_key)}.")

        self._aesgcm = AESGCM(raw_key)

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt ``data``, returning ``nonce || ciphertext || tag``.

        :param data: The plaintext bytes to encrypt.
        :returns: The encrypted token bytes.
        """
        nonce = os.urandom(_NONCE_SIZE_BYTES)
        return nonce + self._aesgcm.encrypt(nonce, data, None)

    def decrypt(self, token: bytes) -> bytes:
        """Decrypt a token previously returned by :meth:`encrypt` back to its original bytes.

        :param token: The token bytes previously returned by :meth:`encrypt`.
        :returns: The decrypted plaintext bytes.
        :raises InvalidToken: If ``token`` is too short, corrupt, tampered with, or was encrypted under a
            different key.
        """
        if len(token) < _NONCE_SIZE_BYTES:
            raise InvalidToken("Token is shorter than a nonce.")

        nonce, ciphertext = token[:_NONCE_SIZE_BYTES], token[_NONCE_SIZE_BYTES:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise InvalidToken("AES-GCM authentication failed.") from exc

    @classmethod
    def generate_key(cls) -> bytes:
        """Generate a fresh, base64url-encoded 256-bit key, suitable for ``CURATOR_TOKEN_KEY``.

        :returns: The encoded key, as bytes (matching ``cryptography.fernet.Fernet.generate_key()``'s
            historical return shape, so existing key-generation call sites don't need to change shape).
        """
        return base64.urlsafe_b64encode(os.urandom(_KEY_SIZE_BYTES))

    @classmethod
    def from_config(
        cls,
        explicit_key: str | bytes | None = None,
        *,
        dotenv_path: Path | None = None,
    ) -> TokenCrypto:
        """Build a :class:`TokenCrypto` from the resolved encryption key.

        Priority: ``explicit_key`` argument, then ``CURATOR_TOKEN_KEY`` as an environment variable,
        then ``CURATOR_TOKEN_KEY`` from a ``.env`` file.

        :param explicit_key: An explicitly supplied key, if any.
        :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
        :returns: A configured :class:`TokenCrypto`.
        :raises ConfigError: If no key can be found.
        """
        explicit = explicit_key.decode("ascii") if isinstance(explicit_key, bytes) else explicit_key
        value = resolve_setting(explicit, env_names=DEFAULT_ENV_NAMES, dotenv_path=dotenv_path)
        if value:
            return cls(value.encode("ascii"))

        raise ConfigError(
            f"No token encryption key found. Set {DEFAULT_ENV_NAMES[0]} as an environment variable or "
            "in a .env file. Generate one with: "
            'python -c "from curator.persistence.crypto import TokenCrypto; '
            'print(TokenCrypto.generate_key().decode())"'
        )
