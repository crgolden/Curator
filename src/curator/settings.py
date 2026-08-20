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

_ALLOY_ENDPOINT_ENV_NAMES: tuple[str, ...] = ("AlloyEndpoint",)
_ELASTICSEARCH_NODE_ENV_NAMES: tuple[str, ...] = ("ElasticsearchNode",)
_ELASTICSEARCH_USERNAME_ENV_NAMES: tuple[str, ...] = ("ElasticsearchUsername",)
_ELASTICSEARCH_PASSWORD_ENV_NAMES: tuple[str, ...] = ("ElasticsearchPassword",)
_LOG_LEVEL_ENV_NAMES: tuple[str, ...] = ("LogLevel", "Logging__LogLevel__Default")

_STORE_QUERY_HASH_PREFIX = "StoreQueryHash"
_SERVICE_BUS_NAMESPACE_ENV_NAMES: tuple[str, ...] = ("ServiceBusNamespace",)
_SERVICE_BUS_CONNECTION_ENV_NAMES: tuple[str, ...] = ("ServiceBusConnectionString",)

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
    :param token_key: The AES-256-GCM key encrypting stored PSN tokens at rest.
    :param database_url: The PostgreSQL connection URL.
    :param alloy_endpoint: The Grafana Alloy OTLP gRPC endpoint (traces + metrics); ``None`` disables that
        telemetry leg entirely.
    :param elasticsearch_node: The Elasticsearch node URL structured logs ship to; ``None`` (along with
        either credential being absent) disables that telemetry leg entirely.
    :param elasticsearch_username: Basic-auth username for ``elasticsearch_node``.
    :param elasticsearch_password: Basic-auth password for ``elasticsearch_node``.
    :param log_level: Threshold for what reaches Elasticsearch, as a standard level name
        (``DEBUG``/``INFO``/``WARNING``/``ERROR``), from ``LogLevel`` or
        ``Logging__LogLevel__Default``; defaults to ``WARNING``.
    :param store_query_hashes: Persisted-query hashes for the anonymous PlayStation Store gateway,
        resolved from ``StoreQueryHash__0``, ``StoreQueryHash__1``, ...; tried before the built-in
        defaults in ``curator.psn.store_client``. Empty (the normal case) uses the built-ins alone.
    :param service_bus_namespace: The fully-qualified Azure Service Bus namespace (e.g.
        ``crgolden.servicebus.windows.net``) backing the ``curator-library-refresh``/``curator-enrichment``
        job queues, authenticated via ``DefaultAzureCredential`` (managed identity in production). Takes
        precedence over ``service_bus_connection_string`` when both are set. This is the only path that
        works against the fleet's shared namespace, which has ``DisableLocalAuth`` enabled.
    :param service_bus_connection_string: A Service Bus connection string, for local development or an
        environment without a real managed identity (the fleet's production namespace rejects this outright).
        Ignored when ``service_bus_namespace`` is set. ``None`` for both disables the queue consumer and the
        job-publishing routes.
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
    log_level: str = "WARNING"
    store_query_hashes: tuple[str, ...] = ()
    service_bus_namespace: str | None = None
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
        log_level = resolve_setting(None, env_names=_LOG_LEVEL_ENV_NAMES, dotenv_path=dotenv_path)
        elasticsearch_username = resolve_setting(
            None, env_names=_ELASTICSEARCH_USERNAME_ENV_NAMES, dotenv_path=dotenv_path
        )
        elasticsearch_password = resolve_setting(
            None, env_names=_ELASTICSEARCH_PASSWORD_ENV_NAMES, dotenv_path=dotenv_path
        )
        store_query_hashes = _resolve_indexed_keys(_STORE_QUERY_HASH_PREFIX, dotenv_path)
        service_bus_namespace = resolve_setting(
            None, env_names=_SERVICE_BUS_NAMESPACE_ENV_NAMES, dotenv_path=dotenv_path
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
            log_level=(log_level or "WARNING").upper(),
            store_query_hashes=store_query_hashes,
            service_bus_namespace=service_bus_namespace,
            service_bus_connection_string=service_bus_connection_string,
            redis_host=redis_host,
            redis_port=int(redis_port_raw) if redis_port_raw else 6380,
            redis_password=redis_password,
            redis_ssl=redis_ssl_raw.strip().lower() != "false" if redis_ssl_raw else True,
        )


def _resolve_indexed_keys(prefix: str, dotenv_path: Path | None) -> tuple[str, ...]:
    """Resolve an array-shaped setting from indexed env vars: ``{prefix}__0``, ``{prefix}__1``, ...,
    stopping at the first missing index.
    """
    keys: list[str] = []
    index = 0
    while True:
        value = resolve_setting(None, env_names=(f"{prefix}__{index}",), dotenv_path=dotenv_path)
        if value is None:
            break
        keys.append(value)
        index += 1
    return tuple(keys)


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
