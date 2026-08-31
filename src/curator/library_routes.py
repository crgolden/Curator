"""``POST /library/refresh`` publishes to the ``curator-library-refresh`` Service Bus queue and returns
immediately; the actual ingest -> canonicalize -> persist -> enrich-delta pipeline runs out of process,
since it can involve many uncached RAWG/OpenCritic/PSN calls bound by those services' own rate limits.
``GET /library/refresh/{run_id}`` polls the resulting :class:`~curator.jobs.repository.JobRun`'s status.

``POST``/``DELETE /library/manual`` add and remove a game PSN has no entitlement for, and
``GET /library/manual/search`` checks a name against the real PlayStation Store before one is added.
``POST /library/manual`` also admits a searched title to the shared catalog when it names one, which is
the only path in this repo that creates a ``games`` row outside ``POST /catalog/backfill``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from curator.audit.repository import ACTION_LIBRARY_REFRESH_REQUESTED, AccountActionLogRepository
from curator.catalog.repository import CatalogRepository
from curator.deps import require_bearer
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.jobs.staleness import abandoned_run_reason
from curator.library.repository import LibraryRepository, LibrarySortField
from curator.psn.errors import PsnAuthError
from curator.psn.models import GameSearchResult
from curator.psn.social_client import (
    FULL_GAMES_DOMAIN,
    GameSearchDomain,
    SocialClient,
    SocialClientFactory,
)
from curator.psn.title_platform import (
    ConsolePlatform,
    console_platform,
    platform_for_title_id,
    platform_vocabulary_message,
)
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/library", tags=["library"])
logger = logging.getLogger("curator")

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."
_NOT_IN_THE_STORE_DETAIL = "That title is not in the PlayStation Store results for this search."

MAX_STORE_SEARCH_LIMIT: Final = 50
"""The most hits ``GET /library/manual/search`` will return, and so the most a re-search must look through.

Accepting a hit re-runs the caller's search server-side and finds their chosen id in the result, so this
bound has to be the same on both routes: a hit the search route was willing to show and the accept route
was unwilling to look far enough to find would be addable-looking and unaddable.
:data:`~curator.psn.social_client.MAX_GAME_SEARCH_PAGES` has to be large enough to reach it.
"""


class LibraryGameResponse(BaseModel):
    """One entry in the ``GET /library`` response.

    :param platforms: Every PlayStation platform this owner holds the game on, newest first. Empty when
        no platform could be established -- a manually-added entry whose caller named none and whose
        catalog row carries no npTitleId to derive one from.
    """

    game_id: str
    title: str
    genre: str | None
    rawg_rating: float | None
    opencritic_rating: float | None
    psn_rating: float | None
    psn_product_id: str | None
    rawg_enriched: bool
    opencritic_enriched: bool
    psn_enriched: bool
    is_active: bool
    percent_completed: int | None
    source: str = "psn"
    cover_image_url: str | None
    platforms: list[str] = []


class LibraryPageResponse(BaseModel):
    """The ``GET /library`` response body: one page of the caller's library plus the total count of
    every row matching the current search/filter, independent of ``limit``/``offset``."""

    games: list[LibraryGameResponse]
    total: int


class LibraryGenresResponse(BaseModel):
    """The ``GET /library/genres`` response body."""

    genres: list[str]


class LibraryRefreshResponse(BaseModel):
    """The ``POST /library/refresh`` response body."""

    run_id: str


class LibraryRefreshStatusResponse(BaseModel):
    """The ``GET /library/refresh/{run_id}`` response body."""

    run_id: str
    status: str
    error: str | None
    result_summary: dict[str, Any] | None


class ManualStoreHitRequest(BaseModel):
    """A hit from ``GET /library/manual/search``, named the way that route reported it.

    The search term comes back with the id because admitting a title re-runs the search server-side and
    matches this id against PSN's own answer. Nothing the client says about the game -- its name, art or
    platforms -- is written to the shared catalog; only the id is honoured, and every stored field is read
    from PSN's response. A body that could name its own ``canonical_title`` would let any linked caller
    write arbitrary rows into a catalog every other user browses.

    :param query: The exact ``q`` that produced the hit.
    :param id: The hit's ``id``, which for the full-games domain is a PSN concept id.
    """

    query: str = Field(min_length=1)
    id: str = Field(min_length=1)


class ManualGameRequest(BaseModel):
    """Body for ``POST /library/manual`` -- a game the user owns with no PSN entitlement.

    Names the game either way round: ``game_id`` for a title already in the catalog, or ``store_hit`` for
    one only the PlayStation Store knows about, which is admitted to the catalog as part of the add.
    Exactly one of the two.

    :param platforms: Every platform the user owns this copy on, from
        :data:`~curator.psn.title_platform.CONSOLE_PLATFORM_IDS`. Omit it to have the platform derived --
        from PSN's own ``platforms`` array for a ``store_hit``, or from the catalog's stored npTitleId
        prefix for a ``game_id``.
    :param native_ps5: Deprecated spelling of ``platforms: ["PS5"]``, unioned into ``platforms``. Kept
        because Librarian still sends it; a boolean pair cannot name PS3, Vita or PSP, which is why
        ``platforms`` exists.
    :param ps4_eligible: Deprecated spelling of ``platforms: ["PS4"]``.
    """

    game_id: str | None = None
    store_hit: ManualStoreHitRequest | None = None
    platforms: list[str] = []
    native_ps5: bool = False
    ps4_eligible: bool = False
    owned_edition: str | None = None

    @model_validator(mode="after")
    def _exactly_one_game_reference(self) -> ManualGameRequest:
        if (self.game_id is None) == (self.store_hit is None):
            raise ValueError("Name the game with exactly one of 'game_id' or 'store_hit'.")
        return self


def _requested_platforms(body: ManualGameRequest) -> list[ConsolePlatform]:
    resolved: list[ConsolePlatform] = []
    for value in body.platforms:
        try:
            platform = console_platform(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=platform_vocabulary_message()) from exc
        if platform not in resolved:
            resolved.append(platform)

    legacy_pair: tuple[tuple[ConsolePlatform, bool], ...] = (("PS5", body.native_ps5), ("PS4", body.ps4_eligible))
    for platform, requested in legacy_pair:
        if requested and platform not in resolved:
            resolved.append(platform)
    return resolved


class StoreSearchResultResponse(BaseModel):
    """One PS Store hit in the ``GET /library/manual/search`` response.

    :param kind: ``"Concept"`` or ``"Product"``, which says which id space ``id`` belongs to -- see
        :class:`~curator.psn.models.GameSearchResult`.
    :param game_id: The catalog game this hit already resolves to, or ``None``. Only the populated case
        carries information: see
        :meth:`~curator.catalog.repository.CatalogRepository.game_ids_for_store_ids` on why ``None`` is
        not evidence the catalog has never seen the title.
    :param classification: PSN's own display classification (``"Full Game"``, ``"Add-On"``, ...), verbatim.
        ``None`` where PSN published none, which is not the same as "not a full game".
    """

    id: str | None
    kind: str | None
    game_id: str | None
    default_product_id: str | None
    name: str | None
    platforms: list[str] = []
    cover_image_url: str | None
    classification: str | None
    price: str | None
    discounted_price: str | None
    is_free: bool | None


class StoreSearchResponse(BaseModel):
    """The ``GET /library/manual/search`` response body."""

    domain: str
    results: list[StoreSearchResultResponse]


@router.get("/manual/search")
async def search_store_for_manual_add(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
    q: str = Query(min_length=1),
    domain: GameSearchDomain = Query(default=FULL_GAMES_DOMAIN),
    limit: int = Query(default=20, ge=1, le=MAX_STORE_SEARCH_LIMIT),
) -> StoreSearchResponse:
    """Search the PlayStation Store by name, so a manual add can be checked against a real store entry.

    Sits with the manual-add flow it serves rather than under ``/catalog``, whose browse routes are
    deliberately anonymous: this one spends the caller's own PSN token, because the operation lives on the
    authenticated mobile gateway and the anonymous storefront has no search at all. Requiring a PSN link
    adds no restriction -- ``POST /library/manual`` is a library operation and the library already needs
    one.

    **This route reads PSN and writes nothing.** Admitting a hit to the catalog is
    :func:`add_manual_game`'s ``store_hit`` branch, which re-runs this search server-side rather than
    trusting anything the client echoes back.

    No harvest preference gates it: it reads the public store, never the caller's own PSN data, so
    ``curator.deps.require_preference`` would gate a store lookup on a flag about harvesting a profile.

    :param q: The search term.
    :param domain: Which store domain to read -- full games (the default) or add-ons.
    :param limit: Maximum hits to return, paged out of PSN as far as it takes.
    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 401, if PSN rejects the stored token.
    """
    catalog_repository: CatalogRepository = request.app.state.catalog_repository
    results = await _search_the_store(request, claims.sub, q, domain=domain, limit=limit)
    game_ids = await catalog_repository.game_ids_for_store_ids([result.id for result in results if result.id])

    return StoreSearchResponse(
        domain=domain,
        results=[
            StoreSearchResultResponse(
                id=result.id,
                kind=result.kind,
                game_id=game_ids.get(result.id or ""),
                default_product_id=result.default_product_id,
                name=result.name,
                platforms=list(result.platforms),
                cover_image_url=result.cover_image_url,
                classification=result.classification,
                price=result.price,
                discounted_price=result.discounted_price,
                is_free=result.is_free,
            )
            for result in results
        ],
    )


async def _search_the_store(
    request: Request, sub: str, query: str, *, domain: GameSearchDomain, limit: int
) -> list[GameSearchResult]:
    """Run one PSN store search on a caller's behalf, translating both failure modes into HTTP statuses.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 401, if PSN rejects the stored token.
    """
    social_client_factory: SocialClientFactory = request.app.state.social_client_factory
    try:
        client: SocialClient = await social_client_factory(sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc

    try:
        return await client.universal_search_games(query, domain=domain, limit=limit)
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc


@router.post("/manual", status_code=204)
async def add_manual_game(
    request: Request, body: ManualGameRequest, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Add a game the user owns that PSN's entitlement API has no record of -- a physical disc, typically.

    Idempotent, and never overwrites a PSN-sourced entry for the same game.

    A ``store_hit`` body admits the title to the shared catalog first, so a game the PlayStation Store
    plainly carries stops being unaddable merely because no user's library has ever ingested it. The
    search is re-run here rather than trusting the client's copy of the hit -- see
    :class:`ManualStoreHitRequest` -- and the platforms come from PSN's own ``platforms`` array unless the
    caller named some, so a PS3, Vita or PSP disc reaches the right platform with no prefix table on the
    client. A ``game_id`` body keeps deriving its platform from the catalog's stored npTitleId prefix
    (:func:`~curator.psn.title_platform.platform_for_title_id`); an unknown or non-game prefix leaves the
    entry with no platform rather than guessing at one.

    :raises fastapi.HTTPException: 400, if ``platforms`` names anything outside
        :data:`~curator.psn.title_platform.CONSOLE_PLATFORM_IDS`; 404, if ``game_id`` is not a known
        catalog game, if a ``store_hit`` matches nothing PSN returned for its own query, or if a
        ``store_hit`` caller has no PSN link; 401, if PSN rejects the caller's stored token.
    """
    library_repository: LibraryRepository = request.app.state.library_repository
    catalog_repository: CatalogRepository = request.app.state.catalog_repository

    if body.store_hit is not None:
        platforms = _requested_platforms(body)
        game_id, store_platforms = await _admit_store_hit(request, claims.sub, body.store_hit)
        platforms = platforms or store_platforms
    else:
        assert body.game_id is not None
        game_id = body.game_id
        if not await catalog_repository.game_exists(game_id):
            raise HTTPException(status_code=404, detail="Unknown game.")
        platforms = _requested_platforms(body)
        if not platforms:
            derived = platform_for_title_id(await catalog_repository.title_id_for_game(game_id))
            platforms = [derived] if derived is not None else []

    await library_repository.upsert_manual_entry(
        claims.sub, game_id, platforms=platforms, owned_edition=body.owned_edition
    )
    return Response(status_code=204)


async def _admit_store_hit(
    request: Request, sub: str, store_hit: ManualStoreHitRequest
) -> tuple[str, list[ConsolePlatform]]:
    """Re-run a caller's store search, find the hit they chose, and admit it to the shared catalog.

    Searches the full-games domain only. A manual library entry records a game the user owns, and the
    add-ons domain answers with downloadable content, so the domain restriction is what keeps a cash-card
    SKU from becoming a catalog game -- it needs no separate rejection branch.

    PSN's platform strings are filtered against
    :data:`~curator.psn.title_platform.CONSOLE_PLATFORM_IDS` rather than trusted wholesale: an
    unrecognised one is dropped, because a platform PSN has started publishing and the ``platforms``
    reference table does not yet carry is a vocabulary gap to notice, not a reason to reject a title the
    user genuinely owns.

    :raises fastapi.HTTPException: 404, if nothing PSN returned for ``store_hit.query`` carries
        ``store_hit.id``, or if the caller has no PSN link; 401, if PSN rejects the stored token.
    """
    catalog_repository: CatalogRepository = request.app.state.catalog_repository
    results = await _search_the_store(
        request, sub, store_hit.query, domain=FULL_GAMES_DOMAIN, limit=MAX_STORE_SEARCH_LIMIT
    )
    hit = next((result for result in results if result.id == store_hit.id), None)
    if hit is None or not hit.name or not hit.name.strip():
        raise HTTPException(status_code=404, detail=_NOT_IN_THE_STORE_DETAIL)

    game_id, _created = await catalog_repository.admit_store_game(
        concept_id=store_hit.id, name=hit.name, product_id=hit.default_product_id
    )

    platforms: list[ConsolePlatform] = []
    for value in hit.platforms:
        try:
            platform = console_platform(value)
        except ValueError:
            continue
        if platform not in platforms:
            platforms.append(platform)
    return game_id, platforms


@router.delete("/manual/{game_id}", status_code=204)
async def remove_manual_game(
    request: Request, game_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Remove a manually-added game. Never deletes a PSN-sourced row.

    :raises fastapi.HTTPException: 404, if the caller has no manual entry for that game.
    """
    library_repository: LibraryRepository = request.app.state.library_repository
    if not await library_repository.delete_manual_entry(claims.sub, game_id):
        raise HTTPException(status_code=404, detail="No manually-added entry for that game.")
    return Response(status_code=204)


@router.get("/genres")
async def get_library_genres(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> LibraryGenresResponse:
    """Return the distinct, sorted set of genres present in the caller's own library -- backs the
    library page's genre filter dropdown."""
    library_repository: LibraryRepository = request.app.state.library_repository
    genres = await library_repository.list_genres(claims.sub)
    return LibraryGenresResponse(genres=genres)


@router.get("")
async def get_library(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
    q: str | None = Query(default=None),
    genre: str | None = Query(default=None),
    sort: LibrarySortField = Query(default="title"),
    sort_dir: Literal["asc", "desc"] = Query(default="asc", alias="sortDir"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LibraryPageResponse:
    """Return one page of the caller's own library, with per-provider (RAWG/OpenCritic) ratings,
    the resolved genre, and PSN's own catalog rating/product id per game.

    Every entry is included, even ones no provider has enriched yet (all rating fields ``None``) --
    this is the finished-library view Librarian's ``/library`` page renders, distinct from
    ``GET /library/refresh/{run_id}``'s job-status polling.

    :param q: Optional case-insensitive title substring filter.
    :param genre: Optional exact-match genre-name filter.
    :param sort: Which column to sort by.
    :param sort_dir: Sort direction; unresolved (``None``) values always sort last regardless.
    :param limit: Page size.
    :param offset: Number of matching rows to skip.
    """
    library_repository: LibraryRepository = request.app.state.library_repository
    games, total = await library_repository.list_entries_with_enrichment(
        claims.sub, search=q, genre=genre, sort=sort, sort_dir=sort_dir, limit=limit, offset=offset
    )
    return LibraryPageResponse(
        games=[
            LibraryGameResponse(
                game_id=game.game_id,
                title=game.title,
                genre=game.genre,
                rawg_rating=game.rawg_rating,
                opencritic_rating=game.opencritic_rating,
                psn_rating=game.psn_rating,
                psn_product_id=game.psn_product_id,
                rawg_enriched=game.rawg_enriched,
                opencritic_enriched=game.opencritic_enriched,
                psn_enriched=game.psn_enriched,
                is_active=game.is_active,
                percent_completed=game.percent_completed,
                source=game.source,
                cover_image_url=game.cover_image_url,
                platforms=list(game.platforms),
            )
            for game in games
        ],
        total=total,
    )


@router.post("/refresh", status_code=202)
async def refresh_library(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> LibraryRefreshResponse:
    """Queue a library-refresh job for the caller's own PSN entitlements.

    :returns: The caller's live non-terminal run's id if one exists, otherwise a newly queued run's id --
        a non-terminal run that has gone stale (:data:`~curator.jobs.staleness.STALE_RUN_THRESHOLD`) is
        marked failed and superseded.
    :raises fastapi.HTTPException: 503, if the job queue isn't configured on this deployment.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    active_run = await job_runs_repository.find_active_run(claims.sub, "library_refresh")
    if active_run is not None:
        reason = abandoned_run_reason(active_run, datetime.now(timezone.utc), noun="refresh")
        if reason is None:
            return LibraryRefreshResponse(run_id=active_run.run_id)
        await job_runs_repository.mark_failed(active_run.run_id, reason)

    queue_publisher: QueuePublisher | None = request.app.state.queue_publisher
    if queue_publisher is None:
        raise HTTPException(status_code=503, detail="Library refresh queue is not configured.")

    run_id = await queue_publisher.publish_library_refresh(claims.sub)
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    try:
        await audit_repository.log(claims.sub, ACTION_LIBRARY_REFRESH_REQUESTED, run_id)
    except Exception:
        logger.exception(
            "Failed to write account_action_log entry (sub=%s, action=%s)", claims.sub, ACTION_LIBRARY_REFRESH_REQUESTED
        )
    return LibraryRefreshResponse(run_id=run_id)


@router.get("/refresh/{run_id}")
async def get_library_refresh_status(
    request: Request, run_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> LibraryRefreshStatusResponse:
    """Poll the status of a previously queued library-refresh job.

    :returns: The run's current :class:`LibraryRefreshStatusResponse`.
    :raises fastapi.HTTPException: 404, if ``run_id`` doesn't exist or isn't the caller's own run.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    run = await job_runs_repository.get(run_id)
    if run is None or run.kind != "library_refresh" or run.identity_sub != claims.sub:
        raise HTTPException(status_code=404, detail="Library refresh run not found.")

    return LibraryRefreshStatusResponse(
        run_id=run.run_id, status=run.status, error=run.error, result_summary=run.result_summary
    )
