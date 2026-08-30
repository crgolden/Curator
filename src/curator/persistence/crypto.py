"""Symmetric encryption for tokens at rest.

``psn_links.token_response_enc`` and ``user_enrichment_keys.{rawg,opencritic}_api_key_enc`` store each
user's secret material encrypted with a single application-wide key -- the database is never trusted alone
to keep a secret. :class:`TokenCrypto` wraps AES-256-GCM (``cryptography.hazmat.primitives.ciphers.aead
.AESGCM``, the same audited library this project already depends on -- not a hand-rolled cipher), resolving
the key the same arg -> env var -> ``.env`` way every other Curator setting resolves (see
:mod:`curator.persistence.config`).

Wire format: an optional leading scheme byte, then ``nonce(12 bytes) || ciphertext || tag(16 bytes)`` --
AESGCM.encrypt already appends the tag to the ciphertext it returns, so :meth:`TokenCrypto.encrypt` only
has to prepend the nonce it generated.

:meth:`TokenCrypto.decrypt` reads both framings: a blob led by :data:`SCHEME_AES_GCM_V1`, and a blob with
no scheme byte at all, which is what every blob written before this dispatch existed looks like. Reading
both is what turns the next cipher change into a dual-read instead of the silent data loss that
``db/reencrypt_tokens.py`` exists to repair: a new scheme claims a new byte, and blobs under the old one
keep decrypting. An unversioned blob whose first byte happens to equal the scheme byte is still read
correctly -- the versioned reading is *attempted*, and its GCM tag check is what rejects it.

:meth:`TokenCrypto.encrypt` writes the versioned framing, led by :data:`SCHEME_AES_GCM_V1`, as does the
Functions .NET port of this class. Deploy ordering between the two runtimes is a real constraint and is
recorded in ``AGENTS/Curator.md``.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from curator.persistence.config import ConfigError, resolve_setting

DEFAULT_ENV_NAMES: tuple[str, ...] = ("CURATOR_TOKEN_KEY",)

SCHEME_AES_GCM_V1 = 0x01

_KEY_SIZE_BYTES = 32
_NONCE_SIZE_BYTES = 12
_TAG_SIZE_BYTES = 16
_SCHEME_SIZE_BYTES = 1


class InvalidToken(Exception):
    """Raised when :meth:`TokenCrypto.decrypt` is given ciphertext that's corrupt, tampered with, too
    short to contain a nonce, or was encrypted under a different key."""


class TokenCrypto:
    """Encrypts and decrypts bytes with an AES-256-GCM key.

    :param key: The key, base64url-encoded (the same encoding :meth:`generate_key` returns), matching how
        every caller stores it in ``CURATOR_TOKEN_KEY``.
    :raises ValueError: If ``key`` is not valid base64url, or doesn't decode to exactly 32 raw bytes.
        Accepted is exactly: the base64url alphabet, in any padding state a base64 length admits.
        Rejected is a stray character, embedded whitespace, and a length that is one more than a
        multiple of four. **The .NET port accepts and rejects the same set, deliberately** -- a key one
        runtime decodes and the other refuses, or worse decodes differently, makes every blob one of them
        wrote unreadable by the other.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) % 4 == 1:
            raise ValueError("TokenCrypto key is not valid base64url.")

        try:
            raw_key = base64.b64decode(key + b"=" * (-len(key) % 4), altchars=b"-_", validate=True)
        except binascii.Error as exc:
            raise ValueError("TokenCrypto key is not valid base64url.") from exc

        if len(raw_key) != _KEY_SIZE_BYTES:
            raise ValueError(f"TokenCrypto key must decode to {_KEY_SIZE_BYTES} bytes, got {len(raw_key)}.")

        self._aesgcm = AESGCM(raw_key)

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt ``data``, returning ``scheme || nonce || ciphertext || tag``.

        :param data: The plaintext bytes to encrypt.
        :returns: The encrypted token bytes, led by :data:`SCHEME_AES_GCM_V1`.
        """
        nonce = os.urandom(_NONCE_SIZE_BYTES)
        return bytes([SCHEME_AES_GCM_V1]) + nonce + self._aesgcm.encrypt(nonce, data, None)

    def decrypt(self, token: bytes) -> bytes:
        """Decrypt a token previously returned by :meth:`encrypt` back to its original bytes.

        Accepts either framing: led by :data:`SCHEME_AES_GCM_V1`, or unversioned.

        :param token: The token bytes previously returned by :meth:`encrypt`.
        :returns: The decrypted plaintext bytes.
        :raises InvalidToken: If ``token`` is too short, corrupt, tampered with, or was encrypted under a
            different key.
        """
        versioned = self._try_decrypt_scheme_aes_gcm_v1(token)
        if versioned is not None:
            return versioned

        if len(token) < _NONCE_SIZE_BYTES + _TAG_SIZE_BYTES:
            raise InvalidToken("Token is shorter than a nonce + tag.")

        try:
            return self._decrypt_nonce_ciphertext_tag(token)
        except InvalidTag as exc:
            raise InvalidToken("AES-GCM authentication failed.") from exc

    def _try_decrypt_scheme_aes_gcm_v1(self, token: bytes) -> bytes | None:
        """Read ``token`` as scheme-byte-led, or report that it is not.

        :param token: The stored bytes.
        :returns: The plaintext, or ``None`` when ``token`` is not this scheme -- either because it is too
            short to carry the framing, or its first byte is not the scheme byte, or the GCM tag rejects
            the versioned reading, which is how an unversioned blob whose nonce merely *starts* with the
            scheme byte falls through to the unversioned reading instead of being misread.
        """
        if len(token) < _SCHEME_SIZE_BYTES + _NONCE_SIZE_BYTES + _TAG_SIZE_BYTES:
            return None
        if token[0] != SCHEME_AES_GCM_V1:
            return None

        try:
            return self._decrypt_nonce_ciphertext_tag(token[_SCHEME_SIZE_BYTES:])
        except InvalidTag:
            return None

    def _decrypt_nonce_ciphertext_tag(self, framed: bytes) -> bytes:
        """Read ``nonce || ciphertext || tag`` with no scheme byte in front of it.

        :param framed: The bytes to read, already stripped of any scheme byte.
        :returns: The decrypted plaintext bytes.
        :raises InvalidTag: If the GCM tag does not authenticate.
        """
        nonce, ciphertext = framed[:_NONCE_SIZE_BYTES], framed[_NONCE_SIZE_BYTES:]
        return self._aesgcm.decrypt(nonce, ciphertext, None)

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
