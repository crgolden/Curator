from __future__ import annotations

from http import HTTPStatus
from typing import Any

OpenApiResponse = dict[str, Any]
OpenApiResponses = dict[int | str, OpenApiResponse]


def _response(status_code: int) -> OpenApiResponse:
    return {"description": HTTPStatus(status_code).phrase}


BAD_REQUEST_RESPONSE = _response(400)
UNAUTHORIZED_RESPONSE = _response(401)
FORBIDDEN_RESPONSE = _response(403)
NOT_FOUND_RESPONSE = _response(404)
CONFLICT_RESPONSE = _response(409)
UNPROCESSABLE_RESPONSE = _response(422)
BAD_GATEWAY_RESPONSE = _response(502)
SERVICE_UNAVAILABLE_RESPONSE = _response(503)

BEARER_ERROR_RESPONSES: OpenApiResponses = {
    401: UNAUTHORIZED_RESPONSE,
    403: FORBIDDEN_RESPONSE,
    503: SERVICE_UNAVAILABLE_RESPONSE,
}
