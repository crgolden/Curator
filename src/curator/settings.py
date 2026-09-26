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
from urllib.parse import urlparse

from curator.persistence.config import ConfigError, resolve_setting
from curator.persistence.connection import resolve_database_url
from curator.persistence.crypto import TOKEN_KEY_ENV

OIDC_AUTHORITY = "OIDC_AUTHORITY"

_OIDC_AUTHORITY_ENV_NAMES: tuple[str, ...] = (OIDC_AUTHORITY,)
_TOKEN_KEY_ENV_NAMES: tuple[str, ...] = (TOKEN_KEY_ENV,)

APP_SERVICE_SITE_NAME = "WEBSITE_SITE_NAME"
ALLOY_ENDPOINT = "AlloyEndpoint"
ELASTICSEARCH_NODE = "ElasticsearchNode"
ELASTICSEARCH_USERNAME = "ElasticsearchUsername"
ELASTICSEARCH_PASSWORD = "ElasticsearchPassword"

_APP_SERVICE_SITE_NAME_ENV_NAMES: tuple[str, ...] = (APP_SERVICE_SITE_NAME,)
_ALLOY_ENDPOINT_ENV_NAMES: tuple[str, ...] = (ALLOY_ENDPOINT,)
_ELASTICSEARCH_NODE_ENV_NAMES: tuple[str, ...] = (ELASTICSEARCH_NODE,)
_ELASTICSEARCH_USERNAME_ENV_NAMES: tuple[str, ...] = (ELASTICSEARCH_USERNAME,)
_ELASTICSEARCH_PASSWORD_ENV_NAMES: tuple[str, ...] = (ELASTICSEARCH_PASSWORD,)
_LOG_LEVEL_ENV_NAMES: tuple[str, ...] = ("LogLevel", "Logging__LogLevel__Default")

_STORE_QUERY_HASH_PREFIX = "StoreQueryHash"
SERVICE_BUS_NAMESPACE = "ServiceBusNamespace"
SERVICE_BUS_CONNECTION_STRING = "ServiceBusConnectionString"
REDIS_HOST = "RedisHost"
REDIS_PORT = "RedisPort"
REDIS_PASSWORD = "RedisPassword"
REDIS_SSL = "RedisSsl"

_SERVICE_BUS_NAMESPACE_ENV_NAMES: tuple[str, ...] = (SERVICE_BUS_NAMESPACE,)
_SERVICE_BUS_CONNECTION_ENV_NAMES: tuple[str, ...] = (SERVICE_BUS_CONNECTION_STRING,)

_REDIS_HOST_ENV_NAMES: tuple[str, ...] = (REDIS_HOST,)
_REDIS_PORT_ENV_NAMES: tuple[str, ...] = (REDIS_PORT,)
_REDIS_PASSWORD_ENV_NAMES: tuple[str, ...] = (REDIS_PASSWORD,)
_REDIS_SSL_ENV_NAMES: tuple[str, ...] = (REDIS_SSL,)

_BOOLEAN_SPELLINGS: dict[str, bool] = {"true": True, "false": False}


@dataclass(frozen=True)
class Settings:
    """Curator's resolved runtime configuration.

    :param oidc_authority: The Identity OIDC authority base URL -- both its
        ``/.well-known/openid-configuration`` discovery document (for the JWKS) and the expected ``iss``
        claim on every validated access token are derived from this.
    :param token_key: The AES-256-GCM key encrypting stored PSN tokens at rest.
    :param database_url: The PostgreSQL connection URL.
    :param alloy_endpoint: The Grafana Alloy OTLP gRPC endpoint (traces + metrics). Required by
        :meth:`from_config` on App Service (``WEBSITE_SITE_NAME`` present); elsewhere ``None`` disables
        that telemetry leg.
    :param elasticsearch_node: The Elasticsearch node URL structured logs ship to. Required by
        :meth:`from_config` on App Service; elsewhere ``None`` disables that leg.
    :param elasticsearch_username: Basic-auth username for ``elasticsearch_node``; required whenever the
        node is set.
    :param elasticsearch_password: Basic-auth password for ``elasticsearch_node``; required whenever the
        node is set.
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
        Ignored when ``service_bus_namespace`` is set. :meth:`from_config` requires one of the two.
    :param redis_host: The Redis host backing trophy caching and the distributed PSN rate limiter. Required
        by :meth:`from_config`; only a directly constructed :class:`Settings` may leave it ``None``.
    :param redis_port: The Redis port. Required by :meth:`from_config`.
    :param redis_password: Redis auth password, if the server requires one.
    :param redis_ssl: Whether to connect to Redis over TLS. Required by :meth:`from_config`.
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
    redis_port: int | None = None
    redis_password: str | None = None
    redis_ssl: bool | None = None

    @classmethod
    def from_config(cls, dotenv_path: Path | None = None) -> Settings:
        """Build :class:`Settings`, resolving every field from env vars / a ``.env`` file.

        :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
        :returns: The resolved :class:`Settings`.
        :raises ConfigError: If a required setting cannot be resolved.
        """
        oidc_authority = _require(
            OIDC_AUTHORITY,
            _OIDC_AUTHORITY_ENV_NAMES,
            dotenv_path,
        )
        token_key = _require(
            TOKEN_KEY_ENV,
            _TOKEN_KEY_ENV_NAMES,
            dotenv_path,
        )
        database_url = resolve_database_url(dotenv_path=dotenv_path)

        hosted = resolve_setting(None, env_names=_APP_SERVICE_SITE_NAME_ENV_NAMES, dotenv_path=dotenv_path)
        if hosted:
            alloy_endpoint: str | None = _require_url(ALLOY_ENDPOINT, _ALLOY_ENDPOINT_ENV_NAMES, dotenv_path)
            elasticsearch_node: str | None = _require_url(
                ELASTICSEARCH_NODE, _ELASTICSEARCH_NODE_ENV_NAMES, dotenv_path
            )
        else:
            alloy_endpoint = _optional_url(ALLOY_ENDPOINT, _ALLOY_ENDPOINT_ENV_NAMES, dotenv_path)
            elasticsearch_node = _optional_url(ELASTICSEARCH_NODE, _ELASTICSEARCH_NODE_ENV_NAMES, dotenv_path)
        elasticsearch_username: str | None = None
        elasticsearch_password: str | None = None
        if elasticsearch_node is not None:
            elasticsearch_username = _require(ELASTICSEARCH_USERNAME, _ELASTICSEARCH_USERNAME_ENV_NAMES, dotenv_path)
            elasticsearch_password = _require(ELASTICSEARCH_PASSWORD, _ELASTICSEARCH_PASSWORD_ENV_NAMES, dotenv_path)
        log_level = resolve_setting(None, env_names=_LOG_LEVEL_ENV_NAMES, dotenv_path=dotenv_path)
        store_query_hashes = _resolve_indexed_keys(_STORE_QUERY_HASH_PREFIX, dotenv_path)
        service_bus_namespace = resolve_setting(
            None, env_names=_SERVICE_BUS_NAMESPACE_ENV_NAMES, dotenv_path=dotenv_path
        )
        service_bus_connection_string = resolve_setting(
            None, env_names=_SERVICE_BUS_CONNECTION_ENV_NAMES, dotenv_path=dotenv_path
        )
        if not service_bus_namespace and not service_bus_connection_string:
            raise ConfigError(
                f"No Service Bus configured. Set {SERVICE_BUS_NAMESPACE} (or {SERVICE_BUS_CONNECTION_STRING} "
                "locally) as an environment variable or in a .env file.",
                SERVICE_BUS_NAMESPACE,
            )

        redis_host = _require(REDIS_HOST, _REDIS_HOST_ENV_NAMES, dotenv_path)
        redis_port = _require_int(REDIS_PORT, _REDIS_PORT_ENV_NAMES, dotenv_path)
        redis_password = resolve_setting(None, env_names=_REDIS_PASSWORD_ENV_NAMES, dotenv_path=dotenv_path)
        redis_ssl = _require_bool(REDIS_SSL, _REDIS_SSL_ENV_NAMES, dotenv_path)

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
            redis_port=redis_port,
            redis_password=redis_password,
            redis_ssl=redis_ssl,
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
    raise ConfigError(f"No {key} found. Set {', '.join(env_names)} as an environment variable or in a .env file.", key)


def _require_url(key: str, env_names: tuple[str, ...], dotenv_path: Path | None) -> str:
    """Resolve a required absolute URL setting.

    :raises ConfigError: If the setting is missing or has no scheme and host.
    """
    raw = _require(key, env_names, dotenv_path)
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigError(f"{key} must be an absolute URL.", key)
    return raw


def _optional_url(key: str, env_names: tuple[str, ...], dotenv_path: Path | None) -> str | None:
    """Resolve an optional absolute URL setting: absent is ``None``, present must parse.

    :raises ConfigError: If the setting is present but has no scheme and host.
    """
    if not resolve_setting(None, env_names=env_names, dotenv_path=dotenv_path):
        return None
    return _require_url(key, env_names, dotenv_path)


def _require_int(key: str, env_names: tuple[str, ...], dotenv_path: Path | None) -> int:
    """Resolve a required integer setting.

    :raises ConfigError: If the setting is missing or is not an integer.
    """
    raw = _require(key, env_names, dotenv_path)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer.", key) from exc


def _require_bool(key: str, env_names: tuple[str, ...], dotenv_path: Path | None) -> bool:
    """Resolve a required boolean setting, spelled ``true`` or ``false`` in any case.

    :raises ConfigError: If the setting is missing or is neither ``true`` nor ``false``.
    """
    raw = _require(key, env_names, dotenv_path).strip().lower()
    if raw not in _BOOLEAN_SPELLINGS:
        raise ConfigError(f"{key} must be true or false.", key)
    return _BOOLEAN_SPELLINGS[raw]
