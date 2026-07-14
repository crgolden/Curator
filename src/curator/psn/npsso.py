"""Npsso credential normalization.

Ported from ``psnpy.config``'s ``parse_npsso()`` -- the only piece of that module Curator needs. psnpy's
env-var/``.env`` resolution chain (``resolve_npsso``, ``npsso_env_names``) assumed one shared local
credential; Curator is multi-tenant and always receives the npsso per-request via ``POST /psn/link``'s
body, so there is no equivalent "resolve from environment" concept to preserve.
"""

from __future__ import annotations

import json


class NpssoError(Exception):
    """Raised when npsso input is malformed (looks like JSON but doesn't parse, or lacks an ``npsso`` key)."""


def parse_npsso(value: str) -> str:
    """Normalize npsso input to the bare token.

    Accepts either the raw token or the ``{"npsso": "..."}`` JSON blob returned by
    ``https://ca.account.sony.com/api/v1/ssocookie`` and returns the token value.

    :param value: The raw token or JSON blob.
    :returns: The bare npsso token.
    :raises NpssoError: If the input looks like JSON but is malformed or lacks an ``npsso`` key.
    """
    stripped = value.strip()
    if "{" not in stripped and "}" not in stripped:
        return stripped

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise NpssoError("Malformed JSON passed as npsso input.") from exc

    token = data.get("npsso")
    if not isinstance(token, str) or not token:
        raise NpssoError('Input JSON is missing a non-empty "npsso" key.')
    return token
