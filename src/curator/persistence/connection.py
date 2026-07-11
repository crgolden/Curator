"""Configuration helpers: resolving the PostgreSQL connection URL.

Built on :func:`curator.persistence.config.resolve_setting` so the connection string is never
hardcoded: an explicit argument wins, then an environment variable, then a local ``.env`` file.
"""

from __future__ import annotations

from pathlib import Path

from curator.persistence.config import ConfigError, resolve_setting

DEFAULT_ENV_NAMES: tuple[str, ...] = ("CURATOR_DATABASE_URL", "DATABASE_URL")


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
    :returns: The connection URL.
    :raises ConfigError: If no connection URL can be found.
    """
    value = resolve_setting(explicit, env_names=env_names, dotenv_path=dotenv_path)
    if value:
        return value

    raise ConfigError(
        f"No database URL found. Set one of {', '.join(env_names)} as an environment variable or in a "
        ".env file, e.g. postgresql://curator_app:<password>@crgolden.com:5432/curator?sslmode=require."
    )
