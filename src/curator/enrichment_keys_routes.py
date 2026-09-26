from __future__ import annotations

import logging
from typing import Annotated, Final, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.audit.recorded import recorded, request_recorder
from curator.audit.repository import ACTION_ENRICHMENT_KEY_ADDED, ACTION_ENRICHMENT_KEY_REMOVED
from curator.deps import require_bearer
from curator.enrichment.opencritic_client import OpenCriticApiError, OpenCriticClient
from curator.enrichment.rawg_client import RawgApiError, RawgClient
from curator.persistence.crypto import TokenCrypto
from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository
from curator.token_validation import TokenClaims

Provider = Literal["rawg", "opencritic"]

RAWG_PROVIDER: Final[Provider] = "rawg"
OPENCRITIC_PROVIDER: Final[Provider] = "opencritic"

_PROVIDER_NAMES: dict[Provider, str] = {RAWG_PROVIDER: "RAWG", OPENCRITIC_PROVIDER: "OpenCritic"}

router = APIRouter(tags=["enrichment-keys"])
logger = logging.getLogger("curator")


class EnrichmentKeyStatusResponse(BaseModel):
    """The ``GET /me/enrichment-keys`` response body.

    ``rawg_key_rejected_at``/``opencritic_key_rejected_at`` are set when a library refresh reported that
    provider's key as rejected (401/403) -- see ``db/migrations/0020_enrichment_key_rejection.sql``. A
    provider can be both ``*_configured`` and rejected at once: the key still exists, it just no longer
    works. Cleared automatically by a successful ``PUT`` for that provider.
    """

    rawg_configured: bool
    opencritic_configured: bool
    rawg_added_at: str | None
    opencritic_added_at: str | None
    rawg_key_rejected_at: str | None = None
    opencritic_key_rejected_at: str | None = None


class SetEnrichmentKeyRequest(BaseModel):
    """The ``PUT /me/enrichment-keys/{provider}`` request body."""

    api_key: str


@router.get("/me/enrichment-keys")
async def get_enrichment_key_status(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> EnrichmentKeyStatusResponse:
    """Return whether the caller has a RAWG/OpenCritic key configured, and when each was added.

    Always answerable -- never 404s, even for a caller who has never configured either provider.
    """
    enrichment_keys_repository: EnrichmentKeysRepository = request.app.state.enrichment_keys_repository
    status = await enrichment_keys_repository.get_status(claims.sub)
    return EnrichmentKeyStatusResponse(
        rawg_configured=status.rawg_configured,
        opencritic_configured=status.opencritic_configured,
        rawg_added_at=status.rawg_added_at.isoformat() if status.rawg_added_at is not None else None,
        opencritic_added_at=status.opencritic_added_at.isoformat() if status.opencritic_added_at is not None else None,
        rawg_key_rejected_at=(
            status.rawg_key_rejected_at.isoformat() if status.rawg_key_rejected_at is not None else None
        ),
        opencritic_key_rejected_at=(
            status.opencritic_key_rejected_at.isoformat() if status.opencritic_key_rejected_at is not None else None
        ),
    )


@router.put("/me/enrichment-keys/{provider}", status_code=204)
async def set_enrichment_key(
    provider: Provider,
    body: SetEnrichmentKeyRequest,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> Response:
    """Set (or replace) the caller's key for ``provider``.

    :raises fastapi.HTTPException: 400, if ``api_key`` is empty/whitespace-only or was rejected by the
        provider as invalid. 503, if the provider couldn't be reached to validate the key -- the key is
        never persisted in either failure case, so the caller always gets a chance to correct it.
    """
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must not be empty.")

    http_client: httpx.AsyncClient = request.app.state.http_client
    enrichment_keys_repository: EnrichmentKeysRepository = request.app.state.enrichment_keys_repository
    token_crypto: TokenCrypto = request.app.state.token_crypto

    async with recorded(request_recorder(request), claims.sub, ACTION_ENRICHMENT_KEY_ADDED, provider):
        await _validate_key(provider, api_key, http_client)
        key_enc = token_crypto.encrypt(api_key.encode())
        if provider == RAWG_PROVIDER:
            await enrichment_keys_repository.upsert_rawg_key(claims.sub, key_enc)
        else:
            await enrichment_keys_repository.upsert_opencritic_key(claims.sub, key_enc)
    return Response(status_code=204)


@router.delete("/me/enrichment-keys/{provider}", status_code=204)
async def delete_enrichment_key(
    provider: Provider, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Delete the caller's key for ``provider``, if one is configured."""
    enrichment_keys_repository: EnrichmentKeysRepository = request.app.state.enrichment_keys_repository

    async with recorded(request_recorder(request), claims.sub, ACTION_ENRICHMENT_KEY_REMOVED, provider):
        if provider == RAWG_PROVIDER:
            await enrichment_keys_repository.delete_rawg_key(claims.sub)
        else:
            await enrichment_keys_repository.delete_opencritic_key(claims.sub)
    return Response(status_code=204)


async def _validate_key(provider: Provider, api_key: str, http_client: httpx.AsyncClient) -> None:
    """Confirm ``api_key`` is actually accepted by ``provider`` before Curator ever persists it.

    A bad key is caught immediately, with a clear message, instead of silently failing every future
    library refresh until the user notices via the friendly-but-vague job error.

    :raises fastapi.HTTPException: 400, if the provider rejected the key (401/403). 503, if the provider
        couldn't be reached at all (network error, timeout, 5xx) -- in this case Curator genuinely doesn't
        know whether the key is good, so it declines to guess and lets the caller retry.

    The provider's own explanation is logged before the response is narrowed down to one of those two
    messages. A 401 from RAWG can mean a wrong key, an unverified account, or an exhausted monthly quota,
    and the user-facing text has to pick one wording for all of them -- so the body is the only way to
    tell a genuinely bad key from a correct key that can't be used right now. It is truncated and has the
    key redacted out of it by the clients; it is never echoed back to the caller.
    """
    provider_name = _PROVIDER_NAMES[provider]
    try:
        if provider == RAWG_PROVIDER:
            await RawgClient(http_client, api_key).validate_key()
        else:
            await OpenCriticClient(http_client, api_key).validate_key()
    except (RawgApiError, OpenCriticApiError) as exc:
        logger.warning(
            "%s key validation failed with status %s: %s",
            provider_name,
            exc.status_code,
            exc.provider_detail or "<no response body>",
        )
        if exc.status_code in (401, 403):
            raise HTTPException(
                status_code=400, detail=f"{provider_name} rejected this API key. Check that it's correct and try again."
            ) from None
        raise HTTPException(
            status_code=503, detail=f"Couldn't validate this {provider_name} key right now. Try again shortly."
        ) from None
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503, detail=f"Couldn't reach {provider_name} to validate this key. Try again shortly."
        ) from None
