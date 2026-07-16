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

# Fleet-convention telemetry settings (see workspace AGENTS.md): App Service application settings of these
# exact names, already used by the other repos' OTLP/Elasticsearch legs. Every one of them is optional --
# unset locally and in CI -- so `curator.telemetry.configure_telemetry` disables each leg independently
# rather than requiring all-or-nothing.
_ALLOY_ENDPOINT_ENV_NAMES: tuple[str, ...] = ("AlloyEndpoint",)
_ELASTICSEARCH_NODE_ENV_NAMES: tuple[str, ...] = ("ElasticsearchNode",)
_ELASTICSEARCH_USERNAME_ENV_NAMES: tuple[str, ...] = ("ElasticsearchUsername",)
_ELASTICSEARCH_PASSWORD_ENV_NAMES: tuple[str, ...] = ("ElasticsearchPassword",)

# Enrichment/jobs settings -- all optional. Unset locally and in CI, same as the telemetry legs above:
# GET /catalog/games and every other route work fine without them; only POST /enrichment/runs (RAWG/
# OpenCritic) and the two job queues (library-refresh, enrichment) need them, and only once Service Bus
# queues are actually provisioned (see the migration plan's "Open follow-ups" -- not yet done as of this
# writing).
_RAWG_API_KEY_ENV_NAMES: tuple[str, ...] = ("RawgApiKey",)
_OPENCRITIC_RAPIDAPI_KEY_ENV_NAMES: tuple[str, ...] = ("OpenCriticRapidApiKey",)
_SERVICE_BUS_CONNECTION_ENV_NAMES: tuple[str, ...] = ("ServiceBusConnectionString",)

# Redis-backed trophy caching (curator.psn.trophy_cache) and distributed PSN rate limiting
# (curator.psn.rate_limiter) -- also optional, unset in dev/CI. Names match the fleet convention already
# used by Manuals/Infrastructure (RedisHost/RedisPort/RedisSsl config values, RedisPassword Key Vault
# secret). Unlike those, ``redis_host`` unset here just falls back to an uncached TrophyClient and a
# no-op NullRateLimiter rather than disabling a whole feature -- PSN calls still work, just without the
# fleet-wide shared budget/cache.
_REDIS_HOST_ENV_NAMES: tuple[str, ...] = ("RedisHost",)
_REDIS_PORT_ENV_NAMES: tuple[str, ...] = ("RedisPort",)
_REDIS_PASSWORD_ENV_NAMES: tuple[str, ...] = ("RedisPassword",)
_REDIS_SSL_ENV_NAMES: tuple[str, ...] = ("RedisSsl",)


@dataclass(frozen=True)
class Settings:
    """Curator's resolved runtime configuration.

    :param oidc_authority: The Identity OIDC authority base URL -- both its
        ``/.well-known/openid-configuration`` discovery document (for the JWKS) and the expected ``iss``
        claim on every validated access token are derived from this.
    :param token_key: The Fernet key encrypting stored PSN tokens at rest.
    :param database_url: The PostgreSQL connection URL.
    :param alloy_endpoint: The Grafana Alloy OTLP gRPC endpoint (traces + metrics); ``None`` disables that
        telemetry leg entirely.
    :param elasticsearch_node: The Elasticsearch node URL structured logs ship to; ``None`` (along with
        either credential being absent) disables that telemetry leg entirely.
    :param elasticsearch_username: Basic-auth username for ``elasticsearch_node``.
    :param elasticsearch_password: Basic-auth password for ``elasticsearch_node``.
    :param rawg_api_key: The RAWG API key; ``None`` disables live RAWG enrichment lookups.
    :param opencritic_rapidapi_key: The RapidAPI key for the OpenCritic API; ``None`` disables live
        OpenCritic enrichment lookups.
    :param service_bus_connection_string: The Azure Service Bus connection string backing the
        ``curator-library-refresh``/``curator-enrichment`` job queues; ``None`` disables the queue
        consumer and the job-publishing routes.
    :param redis_host: The Redis host backing trophy caching and the distributed PSN rate limiter;
        ``None`` disables both (uncached trophy reads, no shared rate-limit budget).
    :param redis_port: The Redis port; defaults to Azure Cache for Redis's SSL port.
    :param redis_password: Redis auth password, if required.
    :param redis_ssl: Whether to connect to Redis over TLS; defaults to ``True``.
    """

    oidc_authority: str
    token_key: str
    database_url: str
    alloy_endpoint: str | None = None
    elasticsearch_node: str | None = None
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    rawg_api_key: str | None = None
    opencritic_rapidapi_key: str | None = None
    service_bus_connection_string: str | None = None
    redis_host: str | None = None
    redis_port: int = 6380
    redis_password: str | None = None
    redis_ssl: bool = True

    @classmethod
    def from_config(cls, dotenv_path: Path | None = None) -> Settings:
        """Build :class:`Settings`, resolving every field from env vars / a ``.env`` file.

        :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
        :returns: The resolved :class:`Settings`.
        :raises ConfigError: If a required setting cannot be resolved.
        """
        oidc_authority = _require(
            "OIDC_AUTHORITY",
            _OIDC_AUTHORITY_ENV_NAMES,
            dotenv_path,
        )
        token_key = _require(
            "CURATOR_TOKEN_KEY",
            _TOKEN_KEY_ENV_NAMES,
            dotenv_path,
        )
        database_url = resolve_database_url(dotenv_path=dotenv_path)

        alloy_endpoint = resolve_setting(None, env_names=_ALLOY_ENDPOINT_ENV_NAMES, dotenv_path=dotenv_path)
        elasticsearch_node = resolve_setting(None, env_names=_ELASTICSEARCH_NODE_ENV_NAMES, dotenv_path=dotenv_path)
        elasticsearch_username = resolve_setting(
            None, env_names=_ELASTICSEARCH_USERNAME_ENV_NAMES, dotenv_path=dotenv_path
        )
        elasticsearch_password = resolve_setting(
            None, env_names=_ELASTICSEARCH_PASSWORD_ENV_NAMES, dotenv_path=dotenv_path
        )
        rawg_api_key = resolve_setting(None, env_names=_RAWG_API_KEY_ENV_NAMES, dotenv_path=dotenv_path)
        opencritic_rapidapi_key = resolve_setting(
            None, env_names=_OPENCRITIC_RAPIDAPI_KEY_ENV_NAMES, dotenv_path=dotenv_path
        )
        service_bus_connection_string = resolve_setting(
            None, env_names=_SERVICE_BUS_CONNECTION_ENV_NAMES, dotenv_path=dotenv_path
        )

        redis_host = resolve_setting(None, env_names=_REDIS_HOST_ENV_NAMES, dotenv_path=dotenv_path)
        redis_port_raw = resolve_setting(None, env_names=_REDIS_PORT_ENV_NAMES, dotenv_path=dotenv_path)
        redis_password = resolve_setting(None, env_names=_REDIS_PASSWORD_ENV_NAMES, dotenv_path=dotenv_path)
        redis_ssl_raw = resolve_setting(None, env_names=_REDIS_SSL_ENV_NAMES, dotenv_path=dotenv_path)

        return cls(
            oidc_authority=oidc_authority,
            token_key=token_key,
            database_url=database_url,
            alloy_endpoint=alloy_endpoint,
            elasticsearch_node=elasticsearch_node,
            elasticsearch_username=elasticsearch_username,
            elasticsearch_password=elasticsearch_password,
            rawg_api_key=rawg_api_key,
            opencritic_rapidapi_key=opencritic_rapidapi_key,
            service_bus_connection_string=service_bus_connection_string,
            redis_host=redis_host,
            redis_port=int(redis_port_raw) if redis_port_raw else 6380,
            redis_password=redis_password,
            redis_ssl=redis_ssl_raw.strip().lower() != "false" if redis_ssl_raw else True,
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
    raise ConfigError(f"No {key} found. Set {', '.join(env_names)} as an environment variable or in a .env file.")
