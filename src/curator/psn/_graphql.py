"""Shared PSN GraphQL persisted-query helper.

PSN's mobile app calls a GraphQL gateway using pre-registered ("persisted") queries identified by a
``sha256`` hash rather than a query document -- both :mod:`curator.psn.library_client` (recently
played/purchased) and :mod:`curator.psn.catalog_client` (universal search) call through this same shape,
just with different operations, headers, and error-checking needs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from curator.psn.session import PsnSession

GRAPHQL_URL = "https://m.np.playstation.com/api/graphql/v1/op"


async def run_persisted_query(
    session: PsnSession,
    operation: tuple[str, str],
    variables: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    check_errors: bool = True,
) -> dict[str, Any]:
    """Call a PSN GraphQL persisted query and return the raw response.

    :param session: The authenticated PSN session to call through.
    :param operation: An ``(operationName, sha256Hash)`` pair identifying the persisted query.
    :param variables: The query variables.
    :param headers: Extra request headers (search queries need Apollo client-identity headers; regular
        queries need an Apollo CSRF-preflight signal instead -- callers supply whichever applies).
    :param check_errors: If ``True`` (the default), raise when the response carries a ``errors`` array
        (e.g. a rotated/unknown persisted-query hash). Universal search intentionally leaves this ``False``
        since its response shape doesn't reliably distinguish "no results" from a query-level error.
    :returns: The full decoded JSON response (including its top-level ``data`` key).
    :raises RuntimeError: If ``check_errors`` is ``True`` and PSN returns GraphQL errors.
    """
    operation_name, sha256_hash = operation
    params = {
        "operationName": operation_name,
        "variables": json.dumps(variables),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}}),
    }
    response: dict[str, Any] = (await session.get(GRAPHQL_URL, params=params, headers=headers)).json()
    if check_errors and response.get("errors"):
        message = response["errors"][0].get("message", "unknown error")
        raise RuntimeError(f"PSN GraphQL '{operation_name}' failed: {message.strip()}")
    return response
