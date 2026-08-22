"""Curator's FastAPI application: resource-server wiring and route registration.

There is deliberately no module-level ``app = create_app()``. Building the real app resolves every
Curator setting (OIDC authority, token key, database URL) at construction time via
:meth:`~curator.settings.Settings.from_config`, which isn't guaranteed to succeed at import time (a test
collection pass, a linter run, ``python -c "import curator.app"`` with no ``.env`` present, ...). Run the
real app with ``uvicorn --factory curator.app:create_app`` instead, which calls the factory lazily once
the process actually starts serving.

Curator is a pure JWT Bearer resource server: it validates access tokens Duende IdentityServer minted
(``curator.token_validation.JwtValidator``) and never issues one, redirects a browser through a login
flow, or holds a session of its own -- no server-side session store, no OIDC client registration, no
cookie.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.management import ServiceBusAdministrationClient
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from curator.audit.repository import AccountActionLogRepository
from curator.catalog.repository import CatalogRepository
from curator.catalog.store_backfill_service import StoreBackfillService
from curator.catalog_routes import router as catalog_router
from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.repository import CollectionsRepository
from curator.collections_routes import router as collections_router
from curator.consoles_routes import router as consoles_router
from curator.devices_routes import router as devices_router
from curator.enrichment_keys_routes import router as enrichment_keys_router
from curator.enrichment_routes import router as enrichment_router
from curator.http_client import shared_ssl_context
from curator.identity_routes import router as identity_router
from curator.jobs import (
    ENRICHMENT_QUEUE,
    LIBRARY_REFRESH_CONTINUATION_QUEUE,
    LIBRARY_REFRESH_QUEUE,
    SCHEDULED_REFRESH_QUEUE,
)
from curator.jobs.queue_depth_monitor import QueueDepthMonitor
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.library.repository import LibraryRepository
from curator.library_routes import router as library_router
from curator.link_service import AgentFactory, PsnAgentLike
from curator.me_routes import router as me_router
from curator.measured_sizes_routes import router as measured_sizes_router
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository
from curator.persistence.follow_repository import FollowRepository
from curator.persistence.profile_link_repository import ProfileLinkRepository
from curator.persistence.profile_repository import ProfileRepository
from curator.persistence.refresh_schedules_repository import RefreshSchedulesRepository
from curator.persistence.repository import Repository
from curator.preferences_routes import router as preferences_router
from curator.presence_routes import router as presence_router
from curator.profile_routes import router as profile_router
from curator.psn.account_client import AccountClient, AccountClientFactory
from curator.psn.mutation_service import MutationService, MutationServiceFactory
from curator.psn.presence_client import PresenceClient, PresenceClientFactory
from curator.psn.rate_limiter import RedisRateLimiter
from curator.psn.repository import PinnedAccountRepository
from curator.psn.safety import MutationGuard
from curator.psn.session import PsnSession, RateLimiter
from curator.psn.social_client import SocialClient, SocialClientFactory
from curator.psn.store_client import CATEGORY_GRID_RETRIEVE_HASHES, StoreCatalogClient
from curator.psn.trophy_cache import CachedTrophyClient
from curator.psn.trophy_client import TrophyClient, TrophyClientFactory
from curator.psn_routes import router as psn_router
from curator.public_collections_routes import router as public_collections_router
from curator.redis_client import RedisAdapter, build_redis_client
from curator.refresh_schedules_routes import router as refresh_schedules_router
from curator.settings import Settings
from curator.social_routes import router as social_router
from curator.storage_devices_routes import router as storage_devices_router
from curator.telemetry import configure_telemetry, shutdown_telemetry
from curator.token_validation import JwtValidator, TokenValidatorLike
from curator.trophy_routes import router as trophy_router

logger = logging.getLogger("curator")


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    token_crypto: TokenCrypto | None = None,
    agent_factory: AgentFactory | None = None,
    token_validator: TokenValidatorLike | None = None,
    pool: AsyncConnectionPool | None = None,
    catalog_repository: CatalogRepository | None = None,
    library_repository: LibraryRepository | None = None,
    collections_repository: CollectionsRepository | None = None,
    job_runs_repository: JobRunsRepository | None = None,
    audit_repository: AccountActionLogRepository | None = None,
    enrichment_keys_repository: EnrichmentKeysRepository | None = None,
    profile_repository: ProfileRepository | None = None,
    profile_link_repository: ProfileLinkRepository | None = None,
    follow_repository: FollowRepository | None = None,
    refresh_schedules_repository: RefreshSchedulesRepository | None = None,
    redis_client: Redis | None = None,
    trophy_client_factory: TrophyClientFactory | None = None,
    identity_client_factory: AccountClientFactory | None = None,
    presence_client_factory: PresenceClientFactory | None = None,
    social_client_factory: SocialClientFactory | None = None,
    mutation_service_factory: MutationServiceFactory | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build a configured Curator :class:`~fastapi.FastAPI` app.

    Every collaborator defaults to a real implementation built from ``settings``; tests inject
    hand-written fakes for all of them instead of monkeypatching. Each collaborator is stashed on
    ``app.state`` so route handlers (which see only ``request``) can reach it.

    :param settings: Resolved application settings; defaults to :meth:`Settings.from_config`.
    :param repository: The account-linking data-access layer; defaults to a real :class:`Repository` over
        a shared :class:`~psycopg_pool.AsyncConnectionPool` opened in this app's lifespan (see ``pool``
        below). Tests that inject their own fake ``repository`` never need a real ``pool`` at all.
    :param token_crypto: The token-encryption helper; defaults to a real :class:`TokenCrypto` over
        ``settings.token_key``.
    :param agent_factory: Builds a PSN agent for a given ``sub`` (and optional ``npsso``); defaults to one
        backed by :class:`~curator.persistence.db_token_store.DbTokenStore` and
        :class:`~curator.psn.account_client.AccountClient` over a restored
        :class:`~curator.psn.session.PsnSession`.
    :param token_validator: Validates bearer access tokens; defaults to a real
        :class:`~curator.token_validation.JwtValidator` over ``settings.oidc_authority``.
    :param pool: The shared connection pool backing every default repository (account-linking, catalog,
        enrichment, library, collections); only used when ``repository`` is not supplied. Opened/closed in
        the app's lifespan when this factory creates it.
    :param catalog_repository: The shared-catalog/canonicalization repository; defaults to a real
        :class:`~curator.catalog.repository.CatalogRepository` over ``pool``.
    :param library_repository: The per-user library repository; defaults to a real
        :class:`~curator.library.repository.LibraryRepository` over ``pool``.
    :param collections_repository: The collections repository; defaults to a real
        :class:`~curator.collections.repository.CollectionsRepository` over ``pool``.
    :param job_runs_repository: The background-job status repository; defaults to a real
        :class:`~curator.jobs.repository.JobRunsRepository` over ``pool``.
    :param audit_repository: The defensive account-action-log repository; defaults to a real
        :class:`~curator.audit.repository.AccountActionLogRepository` over ``pool``. Deliberately kept
        separate from ``repository`` -- see that class's docstring.
    :param enrichment_keys_repository: The per-user BYOK RAWG/OpenCritic key repository; defaults to a
        real :class:`~curator.persistence.enrichment_keys_repository.EnrichmentKeysRepository` over
        ``pool``.
    :param profile_repository: The per-user public-profile display-settings repository; defaults to a real
        :class:`~curator.persistence.profile_repository.ProfileRepository` over ``pool``.
    :param profile_link_repository: The declared-PS-profile-links repository; defaults to a real
        :class:`~curator.persistence.profile_link_repository.ProfileLinkRepository` over ``pool``.
    :param follow_repository: The follow-graph repository; defaults to a real
        :class:`~curator.persistence.follow_repository.FollowRepository` over ``pool``.
    :param redis_client: The shared Redis client backing the distributed PSN rate limiter
        (:class:`~curator.psn.rate_limiter.RedisRateLimiter`) and trophy-read caching
        (:class:`~curator.psn.trophy_cache.CachedTrophyClient`); defaults to
        :func:`~curator.redis_client.build_redis_client` over ``settings``, which is itself ``None`` when
        ``settings.redis_host`` is unset -- PSN calls still work with no Redis configured, just uncached
        and without a shared rate-limit budget.
    :param trophy_client_factory: Builds a trophy client for a given ``sub``; defaults to
        :func:`_default_trophy_client_factory` over the same collaborators as ``agent_factory``.
    :param identity_client_factory: Builds an :class:`~curator.psn.account_client.AccountClient` for a given
        (already-linked) ``sub``; defaults to :func:`_default_identity_client_factory`. Never cached.
    :param presence_client_factory: Builds a :class:`~curator.psn.presence_client.PresenceClient` for a
        given (already-linked) ``sub``; defaults to :func:`_default_presence_client_factory`. Never cached
        -- presence is live-only.
    :param mutation_service_factory: Builds a :class:`~curator.psn.mutation_service.MutationService` for a
        given (already-linked) ``sub``; defaults to :func:`_default_mutation_service_factory`. Never cached.
        Backs ``curator.social_routes``.
    :param social_client_factory: Builds a :class:`~curator.psn.social_client.SocialClient` for a given
        (already-linked) ``sub``; defaults to :func:`_default_social_client_factory`. Never cached. Backs
        both ``curator.devices_routes``'s self-only ``devices()`` call and ``curator.profile_routes``'s
        cross-user ``profile()``/``online_id()`` calls (built from the *viewer's* own sub, called with the
        *target's* ``account_id`` -- see that module's docstring).
    :param http_client: The shared outbound HTTP client used for the admin RAWG/OpenCritic singletons, the
        per-user library-refresh clients, and BYOK key-save validation; defaults to a real
        :class:`httpx.AsyncClient`. Tests inject one wired to an ``httpx.MockTransport`` instead of hitting
        the network.
    :returns: The configured :class:`~fastapi.FastAPI` app.
    """
    settings = settings or Settings.from_config()
    owns_pool = repository is None and pool is None
    pool = pool or (AsyncConnectionPool(settings.database_url, open=False) if repository is None else None)
    shared_pool = cast(AsyncConnectionPool, pool)

    owns_redis = redis_client is None
    redis_client = redis_client or build_redis_client(settings)
    redis_adapter = RedisAdapter(redis_client) if redis_client is not None else None
    rate_limiter: RateLimiter | None = RedisRateLimiter(redis_adapter) if redis_adapter is not None else None

    repository = repository or Repository(shared_pool)
    token_crypto = token_crypto or TokenCrypto.from_config(settings.token_key)
    agent_factory = agent_factory or _default_agent_factory(repository, token_crypto, rate_limiter, redis_adapter)
    trophy_client_factory = trophy_client_factory or _default_trophy_client_factory(
        repository, token_crypto, rate_limiter, redis_adapter
    )
    identity_client_factory = identity_client_factory or _default_identity_client_factory(
        repository, token_crypto, rate_limiter, redis_adapter
    )
    presence_client_factory = presence_client_factory or _default_presence_client_factory(
        repository, token_crypto, rate_limiter, redis_adapter
    )
    social_client_factory = social_client_factory or _default_social_client_factory(
        repository, token_crypto, rate_limiter, redis_adapter
    )
    token_validator = token_validator or JwtValidator(settings.oidc_authority)
    catalog_repository = catalog_repository or CatalogRepository(shared_pool)
    library_repository = library_repository or LibraryRepository(shared_pool)
    collections_repository = collections_repository or CollectionsRepository(shared_pool)
    collection_orchestrator = CollectionOrchestrator(collections_repository)
    store_backfill_service = StoreBackfillService(
        StoreCatalogClient(
            httpx.AsyncClient(timeout=45.0, verify=shared_ssl_context()),
            query_hashes=(*settings.store_query_hashes, *CATEGORY_GRID_RETRIEVE_HASHES),
        ),
        catalog_repository,
    )
    job_runs_repository = job_runs_repository or JobRunsRepository(shared_pool)
    audit_repository = audit_repository or AccountActionLogRepository(shared_pool)
    enrichment_keys_repository = enrichment_keys_repository or EnrichmentKeysRepository(shared_pool)
    profile_repository = profile_repository or ProfileRepository(shared_pool)
    profile_link_repository = profile_link_repository or ProfileLinkRepository(shared_pool)
    follow_repository = follow_repository or FollowRepository(shared_pool)
    refresh_schedules_repository = refresh_schedules_repository or RefreshSchedulesRepository(shared_pool)
    mutation_service_factory = mutation_service_factory or _default_mutation_service_factory(
        repository, token_crypto, rate_limiter, redis_adapter, shared_pool, audit_repository
    )

    owns_http_client = http_client is None
    http_client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0), verify=shared_ssl_context()
    )

    service_bus_credential: DefaultAzureCredential | None = None
    service_bus_admin_credential: SyncDefaultAzureCredential | None = None
    queue_depth_monitor: QueueDepthMonitor | None = None
    if settings.service_bus_namespace:
        service_bus_credential = DefaultAzureCredential()
        service_bus_client = ServiceBusClient(
            fully_qualified_namespace=settings.service_bus_namespace,
            credential=service_bus_credential,
        )
        service_bus_admin_credential = SyncDefaultAzureCredential()
        queue_depth_monitor = QueueDepthMonitor(
            admin_client=ServiceBusAdministrationClient(
                fully_qualified_namespace=settings.service_bus_namespace,
                credential=service_bus_admin_credential,
            ),
            queue_names=[LIBRARY_REFRESH_QUEUE, LIBRARY_REFRESH_CONTINUATION_QUEUE, ENRICHMENT_QUEUE],
        )
    elif settings.service_bus_connection_string:
        service_bus_client = ServiceBusClient.from_connection_string(settings.service_bus_connection_string)
    else:
        service_bus_client = None
    queue_publisher: QueuePublisher | None = None
    if service_bus_client is not None:
        queue_publisher = QueuePublisher(
            library_refresh_sender=service_bus_client.get_queue_sender(LIBRARY_REFRESH_QUEUE),
            enrichment_sender=service_bus_client.get_queue_sender(ENRICHMENT_QUEUE),
            scheduled_refresh_sender=service_bus_client.get_queue_sender(SCHEDULED_REFRESH_QUEUE),
            job_runs_repository=job_runs_repository,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owns_pool and pool is not None:
            await pool.open()
        if queue_depth_monitor is not None:
            queue_depth_monitor.start()
        try:
            yield
        finally:
            if queue_depth_monitor is not None:
                await queue_depth_monitor.stop()
            if service_bus_client is not None:
                await service_bus_client.close()
            if service_bus_credential is not None:
                await service_bus_credential.close()
            if service_bus_admin_credential is not None:
                service_bus_admin_credential.close()
            if owns_http_client:
                await http_client.aclose()
            if owns_redis and redis_client is not None:
                await redis_client.aclose()
            if owns_pool and pool is not None:
                await pool.close()
            shutdown_telemetry()

    app = FastAPI(title="Curator", lifespan=lifespan)

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.repository = repository
    app.state.token_crypto = token_crypto
    app.state.agent_factory = agent_factory
    app.state.trophy_client_factory = trophy_client_factory
    app.state.identity_client_factory = identity_client_factory
    app.state.presence_client_factory = presence_client_factory
    app.state.social_client_factory = social_client_factory
    app.state.redis_client = redis_client
    app.state.redis_adapter = redis_adapter
    app.state.token_validator = token_validator
    app.state.catalog_repository = catalog_repository
    app.state.store_backfill_service = store_backfill_service
    app.state.library_repository = library_repository
    app.state.collections_repository = collections_repository
    app.state.collection_orchestrator = collection_orchestrator
    app.state.job_runs_repository = job_runs_repository
    app.state.audit_repository = audit_repository
    app.state.enrichment_keys_repository = enrichment_keys_repository
    app.state.profile_repository = profile_repository
    app.state.profile_link_repository = profile_link_repository
    app.state.follow_repository = follow_repository
    app.state.refresh_schedules_repository = refresh_schedules_repository
    app.state.mutation_service_factory = mutation_service_factory
    app.state.queue_publisher = queue_publisher
    app.state.queue_depth_monitor = queue_depth_monitor

    app.include_router(me_router)
    app.include_router(psn_router)
    app.include_router(catalog_router)
    app.include_router(enrichment_router)
    app.include_router(library_router)
    app.include_router(collections_router)
    app.include_router(consoles_router)
    app.include_router(storage_devices_router)
    app.include_router(measured_sizes_router)
    app.include_router(trophy_router)
    app.include_router(preferences_router)
    app.include_router(identity_router)
    app.include_router(presence_router)
    app.include_router(devices_router)
    app.include_router(enrichment_keys_router)
    app.include_router(profile_router)
    app.include_router(refresh_schedules_router)
    app.include_router(social_router)
    app.include_router(public_collections_router)

    @app.get("/health")
    async def health() -> PlainTextResponse:
        """Fleet-convention health probe: plain-text ``"Healthy"``, no auth required."""
        return PlainTextResponse("Healthy")

    @app.exception_handler(Exception)
    async def _log_unhandled_exception(request: Request, exc: Exception) -> PlainTextResponse:
        """Log every otherwise-unhandled exception through the ``curator`` logger before responding.

        Gunicorn's ``UvicornWorker`` and uvicorn's own ASGI protocol layer already log unhandled
        exceptions via the ``uvicorn.error`` logger, but that logger's ancestor ``uvicorn`` sets
        ``propagate=False`` in uvicorn's default logging config -- so those records never reach the root
        logger, and therefore never reach the Elasticsearch handler :func:`curator.telemetry
        ._configure_elasticsearch_logging` attaches to root. Logging explicitly here, through a logger
        with no such propagation break, is what actually gets a stack trace shipped to Elasticsearch.
        Starlette always re-raises the exception after this handler runs (see
        ``ServerErrorMiddleware.__call__``), so OpenTelemetry's FastAPI instrumentation still records the
        exception on the span exactly as before -- this handler only adds logging, and reproduces the same
        plain-text 500 Starlette's own default handler would have returned.
        """
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
        return PlainTextResponse("Internal Server Error", status_code=500)

    configure_telemetry(app, settings)

    app.openapi = lambda: _openapi_schema_with_bearer_auth(app)  # type: ignore[method-assign]

    return app


def _openapi_schema_with_bearer_auth(app: FastAPI) -> dict[str, Any]:
    """Build (and cache) Curator's OpenAPI schema with a ``BearerAuth`` security scheme applied to every
    route except the anonymous ``/health`` probe.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    for path, path_item in schema.get("paths", {}).items():
        if path == "/health":
            continue
        for operation in path_item.values():
            operation["security"] = [{"BearerAuth": []}]

    app.openapi_schema = schema
    return schema


def _default_agent_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
) -> AgentFactory:
    """Build the production ``agent_factory``: a real :class:`~curator.psn.account_client.AccountClient`
    per call, backed by a fresh :class:`~curator.persistence.db_token_store.DbTokenStore` for the given
    user and a :class:`~curator.psn.session.PsnSession` restored (or freshly bootstrapped from ``npsso``)
    against it.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter backing the access-token cache (``None`` disables it;
        see :class:`~curator.persistence.db_token_store.DbTokenStore`).
    """

    async def factory(sub: str, npsso: str | None = None) -> PsnAgentLike:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        session = await PsnSession.restore(npsso, token_store, rate_limiter=rate_limiter)
        return AccountClient(session)

    return factory


def _default_trophy_client_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
) -> TrophyClientFactory:
    """Build the production ``trophy_client_factory``: a real :class:`~curator.psn.trophy_client.TrophyClient`
    per call, backed by a fresh :class:`~curator.persistence.db_token_store.DbTokenStore`/
    :class:`~curator.psn.session.PsnSession` for the given (already-linked) user, wrapped in
    :class:`~curator.psn.trophy_cache.CachedTrophyClient` when Redis is configured.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter (``None`` disables both trophy-read caching and the
        access-token cache).
    :raises RuntimeError: If the caller has no stored PSN link.
    """

    async def factory(sub: str) -> TrophyClient | CachedTrophyClient:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch trophies.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        client = TrophyClient(session)
        if redis_adapter is None:
            return client
        return CachedTrophyClient(client, redis_adapter)

    return factory


def _default_identity_client_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
) -> AccountClientFactory:
    """Build the production ``identity_client_factory``: a real
    :class:`~curator.psn.account_client.AccountClient` per call, backed by a fresh
    :class:`~curator.persistence.db_token_store.DbTokenStore`/:class:`~curator.psn.session.PsnSession` for
    the given (already-linked) user. Never wrapped in a cache.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter (``None`` disables the access-token cache).
    :raises RuntimeError: If the caller has no stored PSN link (mirrors ``_default_trophy_client_factory``).
    """

    async def factory(sub: str) -> AccountClient:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch identity.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        return AccountClient(session)

    return factory


def _default_presence_client_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
) -> PresenceClientFactory:
    """Build the production ``presence_client_factory``: a real
    :class:`~curator.psn.presence_client.PresenceClient` per call, backed by a fresh
    :class:`~curator.persistence.db_token_store.DbTokenStore`/:class:`~curator.psn.session.PsnSession` for
    the given (already-linked) user. Never wrapped in a cache -- presence is live-only, no caching client
    exists for it.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter (``None`` disables the access-token cache).
    :raises RuntimeError: If the caller has no stored PSN link (mirrors ``_default_trophy_client_factory``).
    """

    async def factory(sub: str) -> PresenceClient:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch presence.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        return PresenceClient(session)

    return factory


def _default_mutation_service_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
    pool: AsyncConnectionPool,
    audit_repository: AccountActionLogRepository,
) -> MutationServiceFactory:
    """Build the production ``mutation_service_factory``: a real
    :class:`~curator.psn.mutation_service.MutationService` per call, guarded by a
    :class:`~curator.psn.safety.MutationGuard` reading the caller's live PSN link and mutation count.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter (``None`` disables the access-token cache).
    :raises RuntimeError: If the caller has no stored PSN link (mirrors ``_default_social_client_factory``).
    """

    async def factory(sub: str) -> MutationService:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot build a mutation service.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        guard = MutationGuard(sub, PinnedAccountRepository(pool), repository, audit_repository)
        return MutationService(session, guard)

    return factory


def _default_social_client_factory(
    repository: Repository,
    token_crypto: TokenCrypto,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
) -> SocialClientFactory:
    """Build the production ``social_client_factory``: a real
    :class:`~curator.psn.social_client.SocialClient` per call, backed by a fresh
    :class:`~curator.persistence.db_token_store.DbTokenStore`/:class:`~curator.psn.session.PsnSession` for
    the given (already-linked) user. Never wrapped in a cache.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing).
    :param redis_adapter: The shared Redis adapter (``None`` disables the access-token cache).
    :raises RuntimeError: If the caller has no stored PSN link (mirrors ``_default_trophy_client_factory``).
    """

    async def factory(sub: str) -> SocialClient:
        token_store = DbTokenStore(sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot build a social client.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        return SocialClient(session)

    return factory
