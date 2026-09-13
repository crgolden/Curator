"""Configuration helpers: resolving the PostgreSQL connection URL.

Built on :func:`curator.persistence.config.resolve_setting` so the connection string is never
hardcoded: an explicit argument wins, then an environment variable, then a local ``.env`` file.

A URL asking for ``verify-ca`` or ``verify-full`` is also given the CA bundle to verify against, because
libpq cannot find one for itself here: ``psycopg[binary]`` links a statically built OpenSSL whose compiled-in
certificate directory does not exist in the deployed image, so libpq's own ``sslrootcert=system`` keyword
resolves to an empty trust store and every connection fails verification. The bundle named is certifi's, the
same one :func:`curator.http_client.shared_ssl_context` already trusts for every outbound HTTPS call, so this
app has one trust store rather than two.
"""

from __future__ import annotations

from pathlib import Path

import certifi
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from curator.persistence.config import ConfigError, resolve_setting

DEFAULT_ENV_NAMES: tuple[str, ...] = ("CURATOR_DATABASE_URL", "DATABASE_URL")
CERTIFICATE_VERIFYING_SSL_MODES: frozenset[str] = frozenset({"verify-ca", "verify-full"})


def resolve_database_url(
    explicit: str | None = None,
    *,
    dotenv_path: Path | None = None,
    env_names: tuple[str, ...] = DEFAULT_ENV_NAMES,
) -> str:
    """Resolve the PostgreSQL connection URL from the first available source.

    :param explicit: An explicitly supplied connection URL, if any.
    :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
    :param env_names: The env-var names to try, in order.
    :returns: The connection URL, carrying a root certificate when it asks for certificate verification.
    :raises ConfigError: If no connection URL can be found.
    """
    value = resolve_setting(explicit, env_names=env_names, dotenv_path=dotenv_path)
    if value:
        return with_root_certificate(value)

    raise ConfigError(
        f"No database URL found. Set one of {', '.join(env_names)} as an environment variable or in a "
        ".env file, e.g. postgresql://curator_app:<password>@crgolden.com:5432/curator?sslmode=verify-full."
    )


def with_root_certificate(url: str) -> str:
    """Name the CA bundle to verify against when ``url`` asks for verification and names none itself.

    :param url: A libpq connection URL or keyword/value conninfo string.
    :returns: ``url`` unchanged, or an equivalent conninfo string carrying ``sslrootcert``.
    """
    parameters = conninfo_to_dict(url)
    if parameters.get("sslmode") not in CERTIFICATE_VERIFYING_SSL_MODES or parameters.get("sslrootcert"):
        return url

    return make_conninfo(url, sslrootcert=certifi.where())
