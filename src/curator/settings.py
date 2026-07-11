"""Curator's application-level runtime configuration.

Curator is a pure JWT Bearer resource server sitting behind Duende IdentityServer (OIDC) -- there is no
OIDC client registration here at all (no client id, no redirect URI, no session secret): Curator never
starts a login flow, it only validates access tokens Identity already minted (see
:class:`~curator.token_validation.JwtValidator`). Every setting resolves the same
arg -> env var -> ``.env`` way as the persistence layer (:mod:`curator.persistence.config`), so
:class:`Settings` is really just a bundle of those individual resolutions plus the database URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from curator.persistence.config import ConfigError, resolve_setting
from curator.persistence.connection import resolve_database_url

_OIDC_AUTHORITY_ENV_NAMES: tuple[str, ...] = ("OIDC_AUTHORITY",)
_TOKEN_KEY_ENV_NAMES: tuple[str, ...] = ("CURATOR_TOKEN_KEY",)


@dataclass(frozen=True)
class Settings:
    """Curator's resolved runtime configuration.

    :param oidc_authority: The Identity OIDC authority base URL -- both its
        ``/.well-known/openid-configuration`` discovery document (for the JWKS) and the expected ``iss``
        claim on every validated access token are derived from this.
    :param token_key: The Fernet key encrypting stored PSN tokens at rest.
    :param database_url: The PostgreSQL connection URL.
    """

    oidc_authority: str
    token_key: str
    database_url: str

    @classmethod
    def from_config(cls, dotenv_path: Path | None = None) -> "Settings":
        """Build :class:`Settings`, resolving every field from env vars / a ``.env`` file.

        :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
        :returns: The resolved :class:`Settings`.
        :raises ConfigError: If a required setting cannot be resolved.
        """
        oidc_authority = _require(
            "OIDC_AUTHORITY", _OIDC_AUTHORITY_ENV_NAMES, dotenv_path,
        )
        token_key = _require(
            "CURATOR_TOKEN_KEY", _TOKEN_KEY_ENV_NAMES, dotenv_path,
        )
        database_url = resolve_database_url(dotenv_path=dotenv_path)

        return cls(
            oidc_authority=oidc_authority,
            token_key=token_key,
            database_url=database_url,
        )


def _require(key: str, env_names: tuple[str, ...], dotenv_path: Path | None) -> str:
    """Resolve a required setting, raising a named :class:`ConfigError` when it cannot be found.

    :param key: The canonical setting name, used only in the error message.
    :param env_names: The env-var names to try, in order.
    :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
    :returns: The resolved value.
    :raises ConfigError: If no source has the setting.
    """
    value = resolve_setting(None, env_names=env_names, dotenv_path=dotenv_path)
    if value:
        return value
    raise ConfigError(
        f"No {key} found. Set {', '.join(env_names)} as an environment variable or in a .env file."
    )
