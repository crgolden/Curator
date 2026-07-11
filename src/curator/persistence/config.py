"""Configuration helpers: generic arg -> env var -> ``.env`` file resolution.

Curator resolves every piece of runtime configuration (database URL, token-encryption key, ...) the
same way: prefer an explicit argument, then an environment variable, then a local ``.env`` file. This
module implements that priority once so :mod:`curator.persistence.connection` and
:mod:`curator.persistence.crypto` don't each re-derive it. Mirrors ``psnpy.config``'s ``resolve_npsso``
shape, generalized to an arbitrary setting rather than just the npsso token.
"""

from __future__ import annotations

import os
from pathlib import Path


class ConfigError(Exception):
    """Raised when required configuration cannot be resolved from any source."""


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal ``.env`` file (``KEY=VALUE`` lines) without external dependencies.

    :param path: The path to the ``.env`` file.
    :returns: A mapping of the keys found, or an empty mapping if the file does not exist.
    """
    if not path.is_file():
        return {}

    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def resolve_setting(
    explicit: str | None,
    *,
    env_names: tuple[str, ...],
    dotenv_path: Path | None = None,
) -> str | None:
    """Resolve a setting from the first available source.

    Priority: ``explicit`` argument, then each name in ``env_names`` as an environment variable, then
    each name in ``env_names`` from a ``.env`` file (defaults to ``.env`` in the current working
    directory).

    :param explicit: An explicitly supplied value, if any.
    :param env_names: The env-var names to try, in order.
    :param dotenv_path: Path to a ``.env`` file to consult; defaults to ``./.env``.
    :returns: The resolved value, or ``None`` if no source has it.
    """
    if explicit:
        return explicit

    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value

    dotenv = _read_dotenv(dotenv_path or Path.cwd() / ".env")
    for env_name in env_names:
        if dotenv.get(env_name):
            return dotenv[env_name]

    return None
