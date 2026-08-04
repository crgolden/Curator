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
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from azure.servicebus.management import ServiceBusAdministrationClient
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from curator.audit.repository import ACTION_ENRICHMENT_KEY_REJECTED, AccountActionLogRepository
from curator.catalog.franchise_assigner import fingerprint_franchise_rules
from curator.catalog.repository import CatalogRepository
from curator.catalog_routes import router as catalog_router
from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.repository import CollectionsRepository
from curator.collections_routes import router as collections_router
from curator.consoles_routes import router as consoles_router
from curator.devices_routes import router as devices_router
from curator.enrichment.enrichment_service import (
    EnrichmentAuthError,
    EnrichmentRateLimitError,
    EnrichmentService,
    next_rate_limit_backoff_seconds,
)
from curator.enrichment.opencritic_client import OpenCriticClient, OpenCriticClientProtocol
from curator.enrichment.publisher_tier import PublisherTierRule, fingerprint_publisher_tier_rules
from curator.enrichment.rawg_client import RawgClient, RawgClientProtocol, RotatingRawgClient
from curator.enrichment.repository import EnrichmentRepository
from curator.enrichment_keys_routes import router as enrichment_keys_router
from curator.enrichment_routes import router as enrichment_router
from curator.identity_routes import router as identity_router
from curator.jobs import ENRICHMENT_QUEUE, LIBRARY_REFRESH_CONTINUATION_QUEUE, LIBRARY_REFRESH_QUEUE
from curator.jobs.queue_consumer import QueueConsumer, RateLimitRetryScheduled
from curator.jobs.queue_depth_monitor import QueueDepthMonitor
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.library.ingestion_service import IngestionService
from curator.library.library_build_orchestrator import LibraryBuildOrchestrator, enrich_games
from curator.library.repository import LibraryRepository
from curator.library_routes import router as library_router
from curator.link_service import AgentFactory, PsnAgentLike
from curator.me_routes import router as me_router
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository
from curator.persistence.follow_repository import FollowRepository
from curator.persistence.profile_repository import ProfileRepository
from curator.persistence.repository import Repository
from curator.preferences_routes import router as preferences_router
from curator.presence_routes import router as presence_router
from curator.profile_routes import router as profile_router
from curator.psn.account_client import AccountClient, AccountClientFactory
from curator.psn.catalog_client import CatalogClient
from curator.psn.library_client import LibraryClient
from curator.psn.presence_client import PresenceClient, PresenceClientFactory
from curator.psn.rate_limiter import RedisRateLimiter
from curator.psn.session import PsnSession, RateLimiter
from curator.psn.social_client import SocialClient, SocialClientFactory
from curator.psn.trophy_cache import CachedTrophyClient
from curator.psn.trophy_client import TrophyClient, TrophyClientFactory
from curator.psn_routes import router as psn_router
from curator.public_collections_routes import router as public_collections_router
from curator.redis_client import RedisAdapter, build_redis_client
from curator.settings import Settings
from curator.storage_devices_routes import router as storage_devices_router
from curator.telemetry import configure_telemetry, shutdown_telemetry
from curator.token_validation import JwtValidator, TokenValidatorLike
from curator.trophy_routes import router as trophy_router

logger = logging.getLogger("curator")


class ServiceBusLockRenewer:
    """Thin :class:`~curator.jobs.queue_consumer.LockRenewer` adapter over
    :class:`azure.servicebus.aio.AutoLockRenewer` -- the real implementation, wired only here (production
    ``create_app()``), never in :class:`~curator.jobs.queue_consumer.QueueConsumer` itself, which stays
    Azure-agnostic and testable against hand-written fakes (see ``curator.jobs.queue_consumer.NullLockRenewer``,
    the default every existing test implicitly uses).

    :param max_lock_renewal_duration: Maximum total seconds to keep renewing one message's lock.
    """

    def __init__(self, *, max_lock_renewal_duration: int) -> None:
        self._renewer = AutoLockRenewer(max_lock_renewal_duration=max_lock_renewal_duration)

    def register(self, receiver: Any, message: Any) -> None:
        """Start auto-renewing ``message``'s lock on ``receiver``."""
        self._renewer.register(receiver, message)

    async def close(self) -> None:
        """Stop renewing every registered message and release the renewer's resources."""
        await self._renewer.close()


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    token_crypto: TokenCrypto | None = None,
    agent_factory: AgentFactory | None = None,
    token_validator: TokenValidatorLike | None = None,
    pool: AsyncConnectionPool | None = None,
    catalog_repository: CatalogRepository | None = None,
    enrichment_repository: EnrichmentRepository | None = None,
    library_repository: LibraryRepository | None = None,
    collections_repository: CollectionsRepository | None = None,
    job_runs_repository: JobRunsRepository | None = None,
    audit_repository: AccountActionLogRepository | None = None,
    enrichment_keys_repository: EnrichmentKeysRepository | None = None,
    profile_repository: ProfileRepository | None = None,
    follow_repository: FollowRepository | None = None,
    redis_client: Redis | None = None,
    trophy_client_factory: TrophyClientFactory | None = None,
    identity_client_factory: AccountClientFactory | None = None,
    presence_client_factory: PresenceClientFactory | None = None,
    social_client_factory: SocialClientFactory | None = None,
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
    :param enrichment_repository: The enrichment repository; defaults to a real
        :class:`~curator.enrichment.repository.EnrichmentRepository` over ``pool``.
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
    enrichment_repository = enrichment_repository or EnrichmentRepository(shared_pool)
    library_repository = library_repository or LibraryRepository(shared_pool)
    collections_repository = collections_repository or CollectionsRepository(shared_pool)
    collection_orchestrator = CollectionOrchestrator(collections_repository)
    job_runs_repository = job_runs_repository or JobRunsRepository(shared_pool)
    audit_repository = audit_repository or AccountActionLogRepository(shared_pool)
    enrichment_keys_repository = enrichment_keys_repository or EnrichmentKeysRepository(shared_pool)
    profile_repository = profile_repository or ProfileRepository(shared_pool)
    follow_repository = follow_repository or FollowRepository(shared_pool)

    owns_http_client = http_client is None
    # httpx's implicit default (5s across connect/read/write/pool) is what actually killed a real library
    # refresh in production -- request #12 of ~1045 to RAWG hit it under real load (see WP6/WP7 notes in
    # AGENTS/Curator.md). An explicit, slightly more generous budget makes that kind of blip survivable
    # without masking a truly hung connection forever; it does not retry -- see AGENTS/Curator.md's WP7
    # section for why a retry-with-backoff library was evaluated and not adopted in this same pass.
    http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    # Admin-only catalog-wide singleton, built from Settings.rawg_api_keys/opencritic_rapidapi_keys -- its
    # ONLY caller is _enrichment_run_handler (POST /enrichment/runs, require_admin-gated), which uses it
    # both for refresh_opencritic_cache() and, for still-unenriched catalog games, enrich_game() itself.
    # Per-user library refreshes never use this: Curator provisions no shared/fallback RAWG/OpenCritic
    # key (it doesn't scale to every user's library) -- _library_refresh_handler below builds its own
    # per-user clients from that user's own stored keys instead, via enrichment_keys_repository. No
    # catalog_client here either: the official PSN-catalog signal needs a per-user authenticated
    # PsnSession, unlike RAWG/OpenCritic, so this singleton's enrich_game() calls always pass
    # title_id=None and skip the PSN-native genre/rating supplement.
    #
    # More than one configured RAWG key wraps in a rotating client (advances to the next key on a
    # 401/403/429 from the current one) so a full-catalog run isn't capped at a single key's daily quota --
    # per-user BYOK is unaffected, always exactly one key. A single key skips the wrapper entirely.
    # OpenCritic's admin rotation lives in EnrichmentService itself instead (every configured client passed
    # through directly), since it needs to re-read the shared pagination cursor between key attempts -- see
    # EnrichmentService._refresh_opencritic_platform.
    admin_rawg_clients: list[RawgClientProtocol] = [RawgClient(http_client, key) for key in settings.rawg_api_keys]
    admin_rawg_client: RawgClientProtocol | None = (
        admin_rawg_clients[0]
        if len(admin_rawg_clients) == 1
        else RotatingRawgClient(admin_rawg_clients)
        if admin_rawg_clients
        else None
    )
    admin_opencritic_clients: list[OpenCriticClientProtocol] = [
        OpenCriticClient(http_client, key) for key in settings.opencritic_rapidapi_keys
    ]
    enrichment_service = EnrichmentService(
        rawg_client=admin_rawg_client,
        opencritic_client=None,
        opencritic_admin_clients=tuple(admin_opencritic_clients),
        repository=enrichment_repository,
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
        # The Python SDK has no async administration client -- QueueDepthMonitor runs this sync client's
        # calls via asyncio.to_thread. Only meaningful against the real namespace (Manage claims, RBAC),
        # same as why this is gated on service_bus_namespace and not service_bus_connection_string below.
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
    queue_consumer: QueueConsumer | None = None
    lock_renewer: ServiceBusLockRenewer | None = None
    if service_bus_client is not None:
        queue_publisher = QueuePublisher(
            library_refresh_sender=service_bus_client.get_queue_sender(LIBRARY_REFRESH_QUEUE),
            library_refresh_continuation_sender=service_bus_client.get_queue_sender(LIBRARY_REFRESH_CONTINUATION_QUEUE),
            enrichment_sender=service_bus_client.get_queue_sender(ENRICHMENT_QUEUE),
            job_runs_repository=job_runs_repository,
        )
        # 15 minutes: generous enough to cover a large library at RAWG's ~1 req/sec per-user throttle
        # (see _library_refresh_handler), well past the queue's 1-minute LockDuration, while still bounding
        # worst-case duplicate-redelivery exposure if a message somehow hangs forever.
        lock_renewer = ServiceBusLockRenewer(max_lock_renewal_duration=900)
        queue_consumer = QueueConsumer(
            library_refresh_receiver=service_bus_client.get_queue_receiver(LIBRARY_REFRESH_QUEUE),
            library_refresh_continuation_receiver=service_bus_client.get_queue_receiver(
                LIBRARY_REFRESH_CONTINUATION_QUEUE
            ),
            enrichment_receiver=service_bus_client.get_queue_receiver(ENRICHMENT_QUEUE),
            on_library_refresh=_library_refresh_handler(
                repository=repository,
                token_crypto=token_crypto,
                catalog_repository=catalog_repository,
                enrichment_repository=enrichment_repository,
                library_repository=library_repository,
                enrichment_keys_repository=enrichment_keys_repository,
                job_runs_repository=job_runs_repository,
                queue_publisher=queue_publisher,
                http_client=http_client,
                rate_limiter=rate_limiter,
                redis_adapter=redis_adapter,
                audit_repository=audit_repository,
            ),
            on_library_refresh_continuation=_library_refresh_continuation_handler(
                repository=repository,
                token_crypto=token_crypto,
                catalog_repository=catalog_repository,
                enrichment_repository=enrichment_repository,
                library_repository=library_repository,
                enrichment_keys_repository=enrichment_keys_repository,
                job_runs_repository=job_runs_repository,
                queue_publisher=queue_publisher,
                http_client=http_client,
                rate_limiter=rate_limiter,
                redis_adapter=redis_adapter,
                audit_repository=audit_repository,
            ),
            on_enrichment_run=_enrichment_run_handler(enrichment_service, catalog_repository, enrichment_repository),
            job_runs_repository=job_runs_repository,
            lock_renewer=lock_renewer,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owns_pool and pool is not None:
            await pool.open()
        if queue_consumer is not None:
            queue_consumer.start()
        if queue_depth_monitor is not None:
            queue_depth_monitor.start()
        try:
            yield
        finally:
            if queue_consumer is not None:
                await queue_consumer.stop()
            if queue_depth_monitor is not None:
                await queue_depth_monitor.stop()
            if lock_renewer is not None:
                await lock_renewer.close()
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
    app.state.enrichment_repository = enrichment_repository
    app.state.library_repository = library_repository
    app.state.collections_repository = collections_repository
    app.state.collection_orchestrator = collection_orchestrator
    app.state.job_runs_repository = job_runs_repository
    app.state.audit_repository = audit_repository
    app.state.enrichment_keys_repository = enrichment_keys_repository
    app.state.profile_repository = profile_repository
    app.state.follow_repository = follow_repository
    app.state.queue_publisher = queue_publisher
    app.state.queue_consumer = queue_consumer
    app.state.queue_depth_monitor = queue_depth_monitor

    app.include_router(me_router)
    app.include_router(psn_router)
    app.include_router(catalog_router)
    app.include_router(enrichment_router)
    app.include_router(library_router)
    app.include_router(collections_router)
    app.include_router(consoles_router)
    app.include_router(storage_devices_router)
    app.include_router(trophy_router)
    app.include_router(preferences_router)
    app.include_router(identity_router)
    app.include_router(presence_router)
    app.include_router(devices_router)
    app.include_router(enrichment_keys_router)
    app.include_router(profile_router)
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

    # Telemetry (OTLP traces/metrics to Grafana Alloy, Elasticsearch structured logging) is configured last,
    # after routes are registered, so FastAPI instrumentation sees the full route table. Each gunicorn
    # worker calls this factory independently, so per-worker init comes for free here -- do not move this to
    # module import time (breaks fork-safety) or call it more than once per app. It is a no-op per leg when
    # that leg's settings are absent, and never raises: a telemetry failure must never prevent app startup.
    configure_telemetry(app, settings)

    # require_bearer/require_verified_caller/require_admin read the Authorization header manually (there's
    # no session, no OIDC client, so FastAPI's fastapi.security.HTTPBearer dependency injection isn't used
    # anywhere) -- which means FastAPI can't auto-discover a security scheme for the generated OpenAPI
    # document the way it would if a route depended on HTTPBearer directly. Declaring it here once is what
    # makes /docs's "Authorize" button work for every protected route below, matching the OpenAPI-discipline
    # convention this migration's plan calls for (see Manuals/Products/Directory, which all expose the same
    # bearer scheme).
    # FastAPI's own documented pattern for customizing the generated schema.
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


async def _run_opencritic_refresh_pass(enrichment_service: EnrichmentService) -> dict[str, Any]:
    """Run ``EnrichmentService.refresh_opencritic_cache()``, reporting the outcome instead of letting a
    missing/rejected/rate-limited key raise out of the caller -- see problem #1/#2 in the plan this
    implements: a missing or bad OpenCritic key must never block the franchise/tier/enrichment passes
    that don't need it.
    """
    if not enrichment_service.has_opencritic_client:
        return {"status": "not_configured"}
    try:
        games_fetched = await enrichment_service.refresh_opencritic_cache()
    except EnrichmentAuthError as exc:
        return {"status": "auth_error", "detail": str(exc)}
    except EnrichmentRateLimitError as exc:
        return {"status": "rate_limited", "retry_after_seconds": exc.retry_after_seconds}
    return {"status": "ok", "games_fetched": games_fetched}


async def _run_enrichment_pass(
    enrichment_service: EnrichmentService,
    catalog_repository: CatalogRepository,
    enrichment_repository: EnrichmentRepository,
    publisher_tier_rules: list[PublisherTierRule],
) -> dict[str, Any]:
    """Best-effort full enrichment (franchise/tier/genre/scores) for games nobody has enriched yet, using
    the admin ``enrichment_service`` singleton (built from ``Settings.rawg_api_keys``/
    ``opencritic_rapidapi_keys`` -- optional admin-level keys, independent of any user's BYOK key).

    Reports each provider's availability (``psn`` is always ``"not_configured"`` here -- the official-PSN-
    catalog signal needs a per-user authenticated session, which this admin context never has) and stops
    early on an auth/rate-limit error from either configured provider, recording exactly how far it got --
    games saved before the stop stay saved; the rest are picked up again by ``get_unenriched_game_ids`` on
    the next run, no special resume bookkeeping needed since this pass is always a full worklist rebuild.
    """
    providers = {
        "rawg": "ok" if enrichment_service.has_rawg_client else "not_configured",
        "opencritic": "ok" if enrichment_service.has_opencritic_client else "not_configured",
        "psn": "not_configured",
    }

    all_games = await catalog_repository.list_all_game_ids_and_titles()
    unenriched = set(await enrichment_repository.get_unenriched_game_ids([game_id for game_id, _ in all_games]))
    if not unenriched:
        return {
            "providers": providers,
            "attempted_count": 0,
            "enriched_count": 0,
            "remaining_count": 0,
            "stopped_provider": None,
            "stopped_reason": None,
        }

    genre_rows = await enrichment_repository.get_active_genres()
    genre_priorities = {name.lower(): priority for _, name, priority in genre_rows}
    genre_ids_by_name = {name.lower(): genre_id for genre_id, name, _ in genre_rows}
    size_estimates = await catalog_repository.get_size_estimates()

    enriched_count = 0
    stopped_provider: str | None = None
    stopped_reason: str | None = None
    for game_id, title in all_games:
        if game_id not in unenriched:
            continue
        try:
            result, _size = await enrichment_service.enrich_game(
                title,
                title_id=None,
                is_ps5=False,
                genre_priorities=genre_priorities,
                publisher_tier_rules=publisher_tier_rules,
                size_estimates=size_estimates,
            )
        except EnrichmentAuthError as exc:
            stopped_provider, stopped_reason = exc.provider, "auth_error"
            break
        except EnrichmentRateLimitError as exc:
            stopped_provider, stopped_reason = exc.provider, "rate_limited"
            break
        genre_id = genre_ids_by_name.get(result.genre.lower())
        subgenre_id = genre_ids_by_name.get(result.subgenre.lower())
        await enrichment_repository.save_game_enrichment(game_id, genre_id, subgenre_id, result)
        enriched_count += 1

    return {
        "providers": providers,
        "attempted_count": enriched_count + (1 if stopped_reason else 0),
        "enriched_count": enriched_count,
        "remaining_count": len(unenriched) - enriched_count,
        "stopped_provider": stopped_provider,
        "stopped_reason": stopped_reason,
    }


def _enrichment_run_handler(
    enrichment_service: EnrichmentService,
    catalog_repository: CatalogRepository,
    enrichment_repository: EnrichmentRepository,
) -> Callable[[], Coroutine[Any, Any, dict[str, Any] | None]]:
    """Build the ``POST /enrichment/runs`` admin action: a full catalog-wide re-enrichment pass, not
    just an OpenCritic cache refresh.

    Four passes, in order, each reported in the returned result summary rather than raising -- a provider
    being unconfigured, rejected, or rate-limited never fails the whole job (see the module's Follow-up
    plan): the run is fully idempotent (this pass's own cursor/no-op-write/worklist-rebuild behavior means
    a restart can always pick up exactly where a stop left off), so there is nothing here a genuinely
    failed run couldn't just as easily redo from scratch.

    1. ``refresh_opencritic_cache()`` -- pages OpenCritic's PS4/PS5 catalog into ``opencritic_cache``.
    2. Franchise reclassification for every game -- pure title-regex matching against ``franchise_rules``,
       no external API dependency. Skipped when ``franchise_rules`` hasn't changed since the last pass
       that actually ran (see :func:`~curator.catalog.franchise_assigner.fingerprint_franchise_rules`) --
       every newly-canonicalized game is already classified with the current rules at ingestion time, so
       this pass only exists to retroactively fix pre-existing games after a rule edit.
    3. Tier reclassification for every already-enriched ``game_enrichment`` row -- reclassifies
       ``aaa_tier`` from the publisher/developer that enrichment already resolved and stored, against
       the current ``publisher_tiers``. Needed because ``get_unenriched_game_ids`` means an
       already-enriched game is never revisited by the normal per-user refresh path. Skipped on an
       unchanged rule-set fingerprint, same reasoning as pass 2.
    4. Best-effort full enrichment for games nobody has enriched yet (see :func:`_run_enrichment_pass`).
    """

    async def handle() -> dict[str, Any]:
        opencritic_summary = await _run_opencritic_refresh_pass(enrichment_service)

        franchise_rules = await catalog_repository.list_franchise_rules()
        franchise_fingerprint = fingerprint_franchise_rules(franchise_rules)
        previous_franchise_fingerprint = await catalog_repository.get_franchise_rules_fingerprint()
        if franchise_fingerprint != previous_franchise_fingerprint:
            updated = await catalog_repository.reclassify_franchise(franchise_rules)
            await catalog_repository.set_franchise_rules_fingerprint(franchise_fingerprint)
            franchise_summary: dict[str, Any] = {"status": "ran", "updated_count": updated}
        else:
            franchise_summary = {"status": "skipped_unchanged"}

        publisher_tier_rules = await enrichment_repository.list_publisher_tier_rules()
        tier_fingerprint = fingerprint_publisher_tier_rules(publisher_tier_rules)
        previous_tier_fingerprint = await enrichment_repository.get_publisher_tier_rules_fingerprint()
        if tier_fingerprint != previous_tier_fingerprint:
            updated = await enrichment_repository.reclassify_tier(publisher_tier_rules)
            await enrichment_repository.set_publisher_tier_rules_fingerprint(tier_fingerprint)
            tier_summary: dict[str, Any] = {"status": "ran", "updated_count": updated}
        else:
            tier_summary = {"status": "skipped_unchanged"}

        enrichment_summary = await _run_enrichment_pass(
            enrichment_service, catalog_repository, enrichment_repository, publisher_tier_rules
        )

        return {
            "opencritic_cache_refresh": opencritic_summary,
            "franchise_reclassification": franchise_summary,
            "tier_reclassification": tier_summary,
            "enrichment": enrichment_summary,
        }

    return handle


_RAWG_USER_MAX_REQUESTS = 1
_RAWG_USER_WINDOW_SECONDS = 1


def _rate_limited_result_summary(
    *,
    rawg_enriched_titles: list[str],
    opencritic_enriched_titles: list[str],
    opencritic_topup_incomplete: bool,
    rate_limited_provider: str,
    retry_after_seconds: float,
    remaining_count: int,
    rejected_providers: list[str],
) -> dict[str, Any]:
    """Build the ``result_summary`` ``JobRunsRepository.mark_rate_limited`` records -- the same shape
    ``mark_succeeded`` gets, plus the three fields ``GET /library/refresh/{run_id}`` needs to answer "how
    many succeeded" and "when will it resume" for a still-in-progress, rate-limited run.
    """
    return {
        "rawg_enriched_titles": rawg_enriched_titles,
        "opencritic_enriched_titles": opencritic_enriched_titles,
        "opencritic_topup_incomplete": opencritic_topup_incomplete,
        "rate_limited_provider": rate_limited_provider,
        "retry_after_seconds": retry_after_seconds,
        "remaining_count": remaining_count,
        "rejected_providers": rejected_providers,
    }


async def _record_rejected_providers(
    identity_sub: str,
    rejected_providers: list[str],
    *,
    enrichment_keys_repository: EnrichmentKeysRepository,
    audit_repository: AccountActionLogRepository,
) -> None:
    """Persist and audit-log every provider newly rejected during one enrichment call.

    ``rejected_providers`` must be scoped to the single :func:`~curator.library.library_build_orchestrator
    .enrich_games` call that just ran (never a merged/cumulative list spanning a rate-limit continuation
    chain) -- otherwise a provider already known-rejected from an earlier message in the chain would be
    re-marked and re-logged on every resume, which is both redundant and would pollute the audit trail with
    one entry per continuation message instead of one per actual rejection event.
    """
    for provider in rejected_providers:
        if provider == "rawg":
            await enrichment_keys_repository.mark_rawg_key_rejected(identity_sub)
        elif provider == "opencritic":
            await enrichment_keys_repository.mark_opencritic_key_rejected(identity_sub)
        try:
            await audit_repository.log(identity_sub, ACTION_ENRICHMENT_KEY_REJECTED, provider)
        except Exception:
            logger.exception(
                "Failed to write account_action_log entry (sub=%s, action=%s, provider=%s)",
                identity_sub,
                ACTION_ENRICHMENT_KEY_REJECTED,
                provider,
            )


def _build_per_user_rawg_client(
    identity_sub: str, rawg_key: str, *, http_client: httpx.AsyncClient, redis_adapter: RedisAdapter | None
) -> RawgClient:
    rawg_rate_limiter = (
        RedisRateLimiter(
            redis_adapter,
            key=f"curator:rawg:{identity_sub}",
            max_requests=_RAWG_USER_MAX_REQUESTS,
            window_seconds=_RAWG_USER_WINDOW_SECONDS,
        )
        if redis_adapter is not None
        else None
    )
    return RawgClient(http_client, rawg_key, rate_limiter=rawg_rate_limiter)


async def _build_per_user_enrichment_clients(
    identity_sub: str,
    *,
    enrichment_keys_repository: EnrichmentKeysRepository,
    token_crypto: TokenCrypto,
    http_client: httpx.AsyncClient,
    redis_adapter: RedisAdapter | None,
) -> tuple[RawgClient | None, OpenCriticClient | None]:
    """Build a user's own RAWG/OpenCritic clients from their stored BYOK keys (see
    ``curator.enrichment_keys_routes``) -- ``None`` for either a user hasn't configured. Curator never
    provisions a shared/fallback key for either provider here; shared by :func:`_library_refresh_handler`
    and :func:`_library_refresh_continuation_handler` so both build identical per-user clients.
    """
    rawg_key_enc, opencritic_key_enc = await enrichment_keys_repository.get_decrypted_key_material(identity_sub)
    user_rawg_client: RawgClient | None = None
    if rawg_key_enc is not None:
        rawg_key = token_crypto.decrypt(rawg_key_enc).decode()
        user_rawg_client = _build_per_user_rawg_client(
            identity_sub, rawg_key, http_client=http_client, redis_adapter=redis_adapter
        )
    user_opencritic_client: OpenCriticClient | None = None
    if opencritic_key_enc is not None:
        opencritic_key = token_crypto.decrypt(opencritic_key_enc).decode()
        user_opencritic_client = OpenCriticClient(http_client, opencritic_key)
    return user_rawg_client, user_opencritic_client


def _library_refresh_handler(
    *,
    repository: Repository,
    token_crypto: TokenCrypto,
    catalog_repository: CatalogRepository,
    enrichment_repository: EnrichmentRepository,
    library_repository: LibraryRepository,
    enrichment_keys_repository: EnrichmentKeysRepository,
    job_runs_repository: JobRunsRepository,
    queue_publisher: QueuePublisher,
    http_client: httpx.AsyncClient,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
    audit_repository: AccountActionLogRepository,
) -> Callable[[str, str], Coroutine[Any, Any, dict[str, Any] | None]]:
    """Build the ``on_library_refresh`` handler the queue consumer dispatches to.

    Unlike the module-level ``enrichment_service`` singleton, a library refresh needs a PSN catalog
    signal scoped to the refreshing user's own linked account -- so this closure builds a fresh
    :class:`~curator.psn.session.PsnSession`/:class:`~curator.psn.catalog_client.CatalogClient`/
    :class:`~curator.enrichment.enrichment_service.EnrichmentService`/
    :class:`~curator.library.library_build_orchestrator.LibraryBuildOrchestrator` per job instead of
    reusing one global instance. It also looks up the refreshing user's own RAWG/OpenCritic keys (see
    ``curator.enrichment_keys_routes``) and builds per-user clients from them -- there is deliberately no
    fallback to any shared/global key here; a provider a user hasn't configured is simply skipped
    (:class:`~curator.enrichment.enrichment_service.EnrichmentService` tolerates a ``None`` client for
    either).

    If enrichment stops early on a RAWG/OpenCritic rate limit, this performs its own
    ``job_runs_repository.mark_rate_limited`` + ``queue_publisher.publish_library_refresh_continuation``
    and raises :class:`~curator.jobs.queue_consumer.RateLimitRetryScheduled`, instead of returning a result
    dict for the queue consumer's default ``mark_succeeded`` path -- the run isn't actually done yet.

    :param rate_limiter: The shared distributed PSN rate limiter (``None`` throttles nothing); passed
        through to the fresh :class:`~curator.psn.session.PsnSession` so a library refresh's PSN calls
        count against the same fleet-wide budget as every other client.
    :param redis_adapter: The shared Redis adapter backing the access-token cache (``None`` disables it;
        see :class:`~curator.persistence.db_token_store.DbTokenStore`) and the per-user RAWG rate limiter
        below (``None`` disables throttling entirely, matching the fleet's ``NullRateLimiter`` philosophy).
    """

    async def handle(run_id: str, identity_sub: str) -> dict[str, Any] | None:
        token_store = DbTokenStore(identity_sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {identity_sub!r}; cannot refresh library.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        library_client = LibraryClient(session)
        catalog_client = CatalogClient(session)
        ingestion_service = IngestionService(library_client, catalog_repository)

        user_rawg_client, user_opencritic_client = await _build_per_user_enrichment_clients(
            identity_sub,
            enrichment_keys_repository=enrichment_keys_repository,
            token_crypto=token_crypto,
            http_client=http_client,
            redis_adapter=redis_adapter,
        )

        per_user_enrichment_service = EnrichmentService(
            rawg_client=user_rawg_client,
            opencritic_client=user_opencritic_client,
            catalog_client=catalog_client,
            repository=enrichment_repository,
        )
        orchestrator = LibraryBuildOrchestrator(
            ingestion_service=ingestion_service,
            catalog_repository=catalog_repository,
            enrichment_service=per_user_enrichment_service,
            enrichment_repository=enrichment_repository,
            library_repository=library_repository,
        )

        # Reuses this same job's PsnSession rather than a fresh one -- this is exactly the
        # trophy_client_factory-built client's own construction, just against a session already open.
        # None (skipping curator.library.library_build_orchestrator.LibraryBuildOrchestrator
        # .match_trophies entirely) unless the user has actually opted into harvest_trophies.
        trophy_client: TrophyClient | CachedTrophyClient | None = None
        link = await repository.get_link(identity_sub)
        if link is not None and link.harvest_trophies:
            trophy_client = TrophyClient(session)
            if redis_adapter is not None:
                trophy_client = CachedTrophyClient(trophy_client, redis_adapter)

        publisher_tier_rules = await enrichment_repository.list_publisher_tier_rules()
        size_estimates = await catalog_repository.get_size_estimates()
        result = await orchestrator.build(
            identity_sub,
            publisher_tier_rules=publisher_tier_rules,
            size_estimates=size_estimates,
            trophy_client=trophy_client,
        )

        if result.rejected_providers:
            await _record_rejected_providers(
                identity_sub,
                result.rejected_providers,
                enrichment_keys_repository=enrichment_keys_repository,
                audit_repository=audit_repository,
            )

        if result.rate_limited_provider is not None:
            assert result.retry_after_seconds is not None
            result_summary = _rate_limited_result_summary(
                rawg_enriched_titles=result.rawg_enriched_titles,
                opencritic_enriched_titles=result.opencritic_enriched_titles,
                opencritic_topup_incomplete=result.opencritic_topup_incomplete,
                rate_limited_provider=result.rate_limited_provider,
                retry_after_seconds=result.retry_after_seconds,
                remaining_count=len(result.remaining_game_ids),
                rejected_providers=result.rejected_providers,
            )
            new_seq = await job_runs_repository.mark_rate_limited(run_id, result_summary)
            await queue_publisher.publish_library_refresh_continuation(
                run_id,
                identity_sub,
                result.remaining_game_ids,
                result.rate_limited_provider,
                result.retry_after_seconds,
                seq=new_seq,
            )
            raise RateLimitRetryScheduled

        return {
            "rawg_enriched_titles": result.rawg_enriched_titles,
            "opencritic_enriched_titles": result.opencritic_enriched_titles,
            "opencritic_topup_incomplete": result.opencritic_topup_incomplete,
            "rejected_providers": result.rejected_providers,
        }

    return handle


def _library_refresh_continuation_handler(
    *,
    repository: Repository,
    token_crypto: TokenCrypto,
    catalog_repository: CatalogRepository,
    enrichment_repository: EnrichmentRepository,
    library_repository: LibraryRepository,
    enrichment_keys_repository: EnrichmentKeysRepository,
    job_runs_repository: JobRunsRepository,
    queue_publisher: QueuePublisher,
    http_client: httpx.AsyncClient,
    rate_limiter: RateLimiter | None,
    redis_adapter: RedisAdapter | None,
    audit_repository: AccountActionLogRepository,
) -> Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any] | None]]:
    """Build the ``on_library_refresh_continuation`` handler the queue consumer dispatches to.

    Resumes a library refresh's remaining games after a RAWG/OpenCritic rate limit stopped it early (see
    :func:`_library_refresh_handler`'s rate-limit branch). Looks the remaining games up straight from
    ``library_entries`` via :meth:`~curator.library.repository.LibraryRepository.get_games_for_continuation`
    -- no re-ingestion/re-canonicalization from PSN needed -- then rebuilds the same per-user PSN-catalog/
    RAWG/OpenCritic clients :func:`_library_refresh_handler` would, and runs the shared per-game enrichment
    loop (:func:`~curator.library.library_build_orchestrator.enrich_games`) over just those games.

    On success, merges into the run's already-recorded result summary and returns it for the queue
    consumer's default ``mark_succeeded`` path. On another rate limit, performs its own
    ``mark_rate_limited`` + republish and raises
    :class:`~curator.jobs.queue_consumer.RateLimitRetryScheduled`, so the queue consumer completes the
    message without touching the run's status again -- both side effects already happened here.
    """

    async def handle(payload: dict[str, Any]) -> dict[str, Any] | None:
        run_id = payload["run_id"]
        identity_sub = payload["identity_sub"]
        remaining_game_ids = payload["remaining_game_ids"]
        previous_provider = payload["provider"]
        previous_retry_after_seconds = payload["retry_after_seconds"]

        token_store = DbTokenStore(identity_sub, repository, token_crypto, redis_adapter)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {identity_sub!r}; cannot resume library refresh.")

        session = await PsnSession.restore(None, token_store, rate_limiter=rate_limiter)
        catalog_client = CatalogClient(session)

        user_rawg_client, user_opencritic_client = await _build_per_user_enrichment_clients(
            identity_sub,
            enrichment_keys_repository=enrichment_keys_repository,
            token_crypto=token_crypto,
            http_client=http_client,
            redis_adapter=redis_adapter,
        )

        # A fresh EnrichmentService is built per continuation message, but one run's retry chain spans
        # many messages -- seeding the provider that just rate-limited us with an already-doubled backoff
        # is what makes the wait escalate across the whole chain instead of resetting to the 1h default on
        # every resume (see EnrichmentService's rate_limit_backoff_seconds docstring).
        per_user_enrichment_service = EnrichmentService(
            rawg_client=user_rawg_client,
            opencritic_client=user_opencritic_client,
            catalog_client=catalog_client,
            repository=enrichment_repository,
            rate_limit_backoff_seconds={
                previous_provider: next_rate_limit_backoff_seconds(previous_retry_after_seconds)
            },
        )

        continuation_games = await library_repository.get_games_for_continuation(identity_sub, remaining_game_ids)
        games_by_id = {game.game_id: game for game in continuation_games}
        games = [
            (
                game_id,
                games_by_id[game_id].title,
                games_by_id[game_id].product_id,
                games_by_id[game_id].title_id,
                games_by_id[game_id].native_ps5,
            )
            for game_id in remaining_game_ids
            if game_id in games_by_id
        ]

        publisher_tier_rules = await enrichment_repository.list_publisher_tier_rules()
        size_estimates = await catalog_repository.get_size_estimates()
        enrich_result = await enrich_games(
            per_user_enrichment_service,
            enrichment_repository,
            games,
            publisher_tier_rules=publisher_tier_rules,
            size_estimates=size_estimates,
        )

        if enrich_result.rejected_providers:
            # Scoped to just this call's own rejections, not the merged/cumulative list below -- see
            # _record_rejected_providers's docstring for why re-marking an already-known rejection on every
            # continuation resume would be wrong.
            await _record_rejected_providers(
                identity_sub,
                enrich_result.rejected_providers,
                enrichment_keys_repository=enrichment_keys_repository,
                audit_repository=audit_repository,
            )

        existing_run = await job_runs_repository.get(run_id)
        existing_summary = (existing_run.result_summary if existing_run is not None else None) or {}
        merged_rawg_titles = [*existing_summary.get("rawg_enriched_titles", []), *enrich_result.rawg_enriched_titles]
        merged_opencritic_titles = [
            *existing_summary.get("opencritic_enriched_titles", []),
            *enrich_result.opencritic_enriched_titles,
        ]
        opencritic_topup_incomplete = (
            existing_summary.get("opencritic_topup_incomplete", False)
            or per_user_enrichment_service.opencritic_topup_incomplete
        )
        merged_rejected_providers = sorted(
            {*existing_summary.get("rejected_providers", []), *enrich_result.rejected_providers}
        )

        if enrich_result.rate_limited_provider is not None:
            assert enrich_result.retry_after_seconds is not None
            result_summary = _rate_limited_result_summary(
                rawg_enriched_titles=merged_rawg_titles,
                opencritic_enriched_titles=merged_opencritic_titles,
                opencritic_topup_incomplete=opencritic_topup_incomplete,
                rate_limited_provider=enrich_result.rate_limited_provider,
                retry_after_seconds=enrich_result.retry_after_seconds,
                remaining_count=len(enrich_result.remaining_game_ids),
                rejected_providers=merged_rejected_providers,
            )
            new_seq = await job_runs_repository.mark_rate_limited(run_id, result_summary)
            await queue_publisher.publish_library_refresh_continuation(
                run_id,
                identity_sub,
                enrich_result.remaining_game_ids,
                enrich_result.rate_limited_provider,
                enrich_result.retry_after_seconds,
                seq=new_seq,
            )
            raise RateLimitRetryScheduled

        return {
            "rawg_enriched_titles": merged_rawg_titles,
            "opencritic_enriched_titles": merged_opencritic_titles,
            "opencritic_topup_incomplete": opencritic_topup_incomplete,
            "rejected_providers": merged_rejected_providers,
        }

    return handle


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
    :raises RuntimeError: If the caller has no stored PSN link (mirrors ``_library_refresh_handler``).
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
