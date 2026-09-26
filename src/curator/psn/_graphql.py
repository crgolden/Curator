"""Shared PSN GraphQL persisted-query helper.

PSN's mobile app calls a GraphQL gateway using pre-registered ("persisted") queries identified by a
``sha256`` hash rather than a query document -- both the library (recently played/purchased) and
catalog (universal search) callers go through this same shape,
just with different operations, headers, and error-checking needs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from curator.psn.session import PsnSession

GRAPHQL_URL = "https://m.np.playstation.com/api/graphql/v1/op"

OPERATION_NAME_PARAM: Final = "operationName"
VARIABLES_PARAM: Final = "variables"
EXTENSIONS_PARAM: Final = "extensions"
PERSISTED_QUERY_KEY: Final = "persistedQuery"
VERSION_KEY: Final = "version"
SHA256_HASH_KEY: Final = "sha256Hash"

DATA_KEY: Final = "data"
ERRORS_KEY: Final = "errors"
MESSAGE_KEY: Final = "message"


def persisted_query_params(operation_name: str, variables: dict[str, Any], sha256_hash: str) -> dict[str, str]:
    """The query-string parameters that name one persisted query and its variables.

    :param operation_name: The persisted operation's name.
    :param variables: The query variables, JSON-encoded into the ``variables`` parameter.
    :param sha256_hash: The hash the gateway registered the query document under.
    :returns: The ``operationName``/``variables``/``extensions`` parameters.
    """
    return {
        OPERATION_NAME_PARAM: operation_name,
        VARIABLES_PARAM: json.dumps(variables),
        EXTENSIONS_PARAM: json.dumps({PERSISTED_QUERY_KEY: {VERSION_KEY: 1, SHA256_HASH_KEY: sha256_hash}}),
    }


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
    params = persisted_query_params(operation_name, variables, sha256_hash)
    response: dict[str, Any] = (await session.get(GRAPHQL_URL, params=params, headers=headers)).json()
    if check_errors and response.get(ERRORS_KEY):
        message = response[ERRORS_KEY][0].get(MESSAGE_KEY, "unknown error")
        raise RuntimeError(f"PSN GraphQL '{operation_name}' failed: {message.strip()}")
    return response
