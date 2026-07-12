"""Symmetric encryption for tokens at rest.

``psn_links.token_response_enc`` stores each user's PSN token dict (access + refresh tokens) encrypted
with a single application-wide key — the database is never trusted alone to keep a token secret.
:class:`TokenCrypto` wraps :class:`cryptography.fernet.Fernet`, resolving the key the same
arg -> env var -> ``.env`` way every other Curator setting resolves (see
:mod:`curator.persistence.config`).
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from curator.persistence.config import ConfigError, resolve_setting

DEFAULT_ENV_NAMES: tuple[str, ...] = ("CURATOR_TOKEN_KEY",)


class TokenCrypto:
    """Encrypts and decrypts bytes with a Fernet key.

    :param key: A Fernet key, as returned by :meth:`cryptography.fernet.Fernet.generate_key`.
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt ``data``, returning a Fernet token.

        :param data: The plaintext bytes to encrypt.
        :returns: The Fernet-encrypted token bytes.
        """
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """Decrypt a Fernet token back to its original bytes.

        :param token: The Fernet token bytes previously returned by :meth:`encrypt`.
        :returns: The decrypted plaintext bytes.
        :raises cryptography.fernet.InvalidToken: If ``token`` is corrupt, tampered with, or was
            encrypted under a different key.
        """
        return self._fernet.decrypt(token)

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

        :param explicit_key: An explicitly supplied Fernet key, if any.
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
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
