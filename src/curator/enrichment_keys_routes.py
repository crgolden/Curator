"""``GET/PUT/DELETE /me/enrichment-keys`` -- the caller's own, optionally-provided RAWG/OpenCritic API keys.

Curator never provisions a shared RAWG/OpenCritic key -- it doesn't scale to every user's library. Instead
a user may supply their own key for either or both providers; :mod:`curator.app`'s
``_library_refresh_handler`` uses it (and only it, no fallback) for that user's own enrichment. Keys are
encrypted with the same Fernet key already protecting PSN tokens at rest (see
:class:`curator.persistence.crypto.TokenCrypto`, :meth:`curator.persistence.repository.Repository.upsert_link`)
and are never returned by any route -- ``GET`` reports only whether a key is configured and when it was
added, never the value.

Unlike ``curator.psn_routes``'s ``DELETE /psn/link``, deleting an enrichment key skips
``curator.reverify.reverify_link`` -- removing your own API key carries none of the account-takeover risk
unlinking PSN does, so ``require_bearer`` alone is enough, matching ``curator.preferences_routes``.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.audit.repository import (
    ACTION_ENRICHMENT_KEY_ADDED,
    ACTION_ENRICHMENT_KEY_REMOVED,
    AccountActionLogRepository,
)
from curator.deps import require_bearer
from curator.enrichment.opencritic_client import OpenCriticApiError, OpenCriticClient
from curator.enrichment.rawg_client import RawgApiError, RawgClient
from curator.persistence.crypto import TokenCrypto
from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository
from curator.token_validation import TokenClaims

_PROVIDER_NAMES: dict[str, str] = {"rawg": "RAWG", "opencritic": "OpenCritic"}

router = APIRouter(tags=["enrichment-keys"])
logger = logging.getLogger("curator")

Provider = Literal["rawg", "opencritic"]


class EnrichmentKeyStatusResponse(BaseModel):
    """The ``GET /me/enrichment-keys`` response body."""

    rawg_configured: bool
    opencritic_configured: bool
    rawg_added_at: str | None
    opencritic_added_at: str | None


class SetEnrichmentKeyRequest(BaseModel):
    """The ``PUT /me/enrichment-keys/{provider}`` request body."""

    api_key: str


@router.get("/me/enrichment-keys", response_model=EnrichmentKeyStatusResponse)
async def get_enrichment_key_status(
    request: Request, claims: TokenClaims = Depends(require_bearer)
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
    )


@router.put("/me/enrichment-keys/{provider}", status_code=204)
async def set_enrichment_key(
    provider: Provider,
    body: SetEnrichmentKeyRequest,
    request: Request,
    claims: TokenClaims = Depends(require_bearer),
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
    await _validate_key(provider, api_key, http_client)

    enrichment_keys_repository: EnrichmentKeysRepository = request.app.state.enrichment_keys_repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    key_enc = token_crypto.encrypt(api_key.encode())

    if provider == "rawg":
        await enrichment_keys_repository.upsert_rawg_key(claims.sub, key_enc)
    else:
        await enrichment_keys_repository.upsert_opencritic_key(claims.sub, key_enc)

    await _log(request, claims.sub, ACTION_ENRICHMENT_KEY_ADDED, provider)
    return Response(status_code=204)


@router.delete("/me/enrichment-keys/{provider}", status_code=204)
async def delete_enrichment_key(
    provider: Provider, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> Response:
    """Delete the caller's key for ``provider``, if one is configured."""
    enrichment_keys_repository: EnrichmentKeysRepository = request.app.state.enrichment_keys_repository

    if provider == "rawg":
        await enrichment_keys_repository.delete_rawg_key(claims.sub)
    else:
        await enrichment_keys_repository.delete_opencritic_key(claims.sub)

    await _log(request, claims.sub, ACTION_ENRICHMENT_KEY_REMOVED, provider)
    return Response(status_code=204)


async def _validate_key(provider: Provider, api_key: str, http_client: httpx.AsyncClient) -> None:
    """Confirm ``api_key`` is actually accepted by ``provider`` before Curator ever persists it.

    A bad key is caught immediately, with a clear message, instead of silently failing every future
    library refresh until the user notices via the friendly-but-vague job error.

    :raises fastapi.HTTPException: 400, if the provider rejected the key (401/403). 503, if the provider
        couldn't be reached at all (network error, timeout, 5xx) -- in this case Curator genuinely doesn't
        know whether the key is good, so it declines to guess and lets the caller retry.
    """
    provider_name = _PROVIDER_NAMES[provider]
    try:
        if provider == "rawg":
            await RawgClient(http_client, api_key).validate_key()
        else:
            await OpenCriticClient(http_client, api_key).validate_key()
    except (RawgApiError, OpenCriticApiError) as exc:
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


async def _log(request: Request, sub: str, action: str, provider: str) -> None:
    """Write one audit entry naming the provider only -- never the key value.

    Never lets a logging failure break the user-facing request, matching ``curator.psn_routes``'s
    ``_log`` precedent.
    """
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    try:
        await audit_repository.log(sub, action, provider)
    except Exception:
        logger.exception("Failed to write account_action_log entry (sub=%s, action=%s)", sub, action)
