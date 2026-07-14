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

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from azure.servicebus.aio import ServiceBusClient
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse
from psycopg_pool import AsyncConnectionPool

from curator.catalog.repository import CatalogRepository
from curator.catalog_routes import router as catalog_router
from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.repository import CollectionsRepository
from curator.collections_routes import router as collections_router
from curator.consoles_routes import router as consoles_router
from curator.enrichment.enrichment_service import EnrichmentService
from curator.enrichment.opencritic_client import OpenCriticClient
from curator.enrichment.rawg_client import RawgClient
from curator.enrichment.repository import EnrichmentRepository
from curator.enrichment_routes import router as enrichment_router
from curator.jobs import ENRICHMENT_QUEUE, LIBRARY_REFRESH_QUEUE
from curator.jobs.queue_consumer import QueueConsumer
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.library.ingestion_service import IngestionService
from curator.library.library_build_orchestrator import LibraryBuildOrchestrator
from curator.library.repository import LibraryRepository
from curator.library_routes import router as library_router
from curator.link_service import AgentFactory, PsnAgentLike
from curator.me_routes import router as me_router
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.repository import Repository
from curator.psn.account_client import AccountClient
from curator.psn.catalog_client import CatalogClient
from curator.psn.library_client import LibraryClient
from curator.psn.session import PsnSession
from curator.psn_routes import router as psn_router
from curator.settings import Settings
from curator.telemetry import configure_telemetry
from curator.token_validation import JwtValidator, TokenValidatorLike


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
    :returns: The configured :class:`~fastapi.FastAPI` app.
    """
    settings = settings or Settings.from_config()
    owns_pool = repository is None and pool is None
    pool = pool or (AsyncConnectionPool(settings.database_url, open=False) if repository is None else None)
    shared_pool = cast(AsyncConnectionPool, pool)

    repository = repository or Repository(shared_pool)
    token_crypto = token_crypto or TokenCrypto.from_config(settings.token_key)
    agent_factory = agent_factory or _default_agent_factory(repository, token_crypto)
    token_validator = token_validator or JwtValidator(settings.oidc_authority)
    catalog_repository = catalog_repository or CatalogRepository(shared_pool)
    enrichment_repository = enrichment_repository or EnrichmentRepository(shared_pool)
    library_repository = library_repository or LibraryRepository(shared_pool)
    collections_repository = collections_repository or CollectionsRepository(shared_pool)
    collection_orchestrator = CollectionOrchestrator(collections_repository)
    job_runs_repository = job_runs_repository or JobRunsRepository(shared_pool)

    http_client = httpx.AsyncClient()
    rawg_client = RawgClient(http_client, settings.rawg_api_key or "")
    opencritic_client = OpenCriticClient(http_client, settings.opencritic_rapidapi_key or "")
    # No catalog_client here: the official PSN-catalog signal needs a per-user authenticated PsnSession,
    # unlike RAWG/OpenCritic -- this singleton only ever calls refresh_opencritic_cache() (admin-scoped
    # global re-scrape, PSN-free), never enrich_game(). The library-refresh job handler below builds its
    # own per-user EnrichmentService with a real catalog_client instead.
    enrichment_service = EnrichmentService(
        rawg_client=rawg_client, opencritic_client=opencritic_client, repository=enrichment_repository
    )

    service_bus_client = (
        ServiceBusClient.from_connection_string(settings.service_bus_connection_string)
        if settings.service_bus_connection_string
        else None
    )
    queue_publisher: QueuePublisher | None = None
    queue_consumer: QueueConsumer | None = None
    if service_bus_client is not None:
        queue_publisher = QueuePublisher(
            library_refresh_sender=service_bus_client.get_queue_sender(LIBRARY_REFRESH_QUEUE),
            enrichment_sender=service_bus_client.get_queue_sender(ENRICHMENT_QUEUE),
            job_runs_repository=job_runs_repository,
        )
        queue_consumer = QueueConsumer(
            library_refresh_receiver=service_bus_client.get_queue_receiver(LIBRARY_REFRESH_QUEUE),
            enrichment_receiver=service_bus_client.get_queue_receiver(ENRICHMENT_QUEUE),
            on_library_refresh=_library_refresh_handler(
                repository=repository,
                token_crypto=token_crypto,
                catalog_repository=catalog_repository,
                enrichment_repository=enrichment_repository,
                library_repository=library_repository,
                rawg_client=rawg_client,
                opencritic_client=opencritic_client,
            ),
            on_enrichment_run=_enrichment_run_handler(enrichment_service),
            job_runs_repository=job_runs_repository,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owns_pool and pool is not None:
            await pool.open()
        if queue_consumer is not None:
            queue_consumer.start()
        try:
            yield
        finally:
            if queue_consumer is not None:
                await queue_consumer.stop()
            if service_bus_client is not None:
                await service_bus_client.close()
            await http_client.aclose()
            if owns_pool and pool is not None:
                await pool.close()

    app = FastAPI(title="Curator", lifespan=lifespan)

    app.state.settings = settings
    app.state.repository = repository
    app.state.token_crypto = token_crypto
    app.state.agent_factory = agent_factory
    app.state.token_validator = token_validator
    app.state.catalog_repository = catalog_repository
    app.state.enrichment_repository = enrichment_repository
    app.state.library_repository = library_repository
    app.state.collections_repository = collections_repository
    app.state.collection_orchestrator = collection_orchestrator
    app.state.job_runs_repository = job_runs_repository
    app.state.queue_publisher = queue_publisher
    app.state.queue_consumer = queue_consumer

    app.include_router(me_router)
    app.include_router(psn_router)
    app.include_router(catalog_router)
    app.include_router(enrichment_router)
    app.include_router(library_router)
    app.include_router(collections_router)
    app.include_router(consoles_router)

    @app.get("/health")
    async def health() -> PlainTextResponse:
        """Fleet-convention health probe: plain-text ``"Healthy"``, no auth required."""
        return PlainTextResponse("Healthy")

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


def _enrichment_run_handler(enrichment_service: EnrichmentService) -> Callable[[], Coroutine[Any, Any, None]]:
    """Adapt ``EnrichmentService.refresh_opencritic_cache`` (takes an optional arg, returns a count) to the
    queue consumer's ``on_enrichment_run`` shape (no args, no return value)."""

    async def handle() -> None:
        await enrichment_service.refresh_opencritic_cache()

    return handle


def _library_refresh_handler(
    *,
    repository: Repository,
    token_crypto: TokenCrypto,
    catalog_repository: CatalogRepository,
    enrichment_repository: EnrichmentRepository,
    library_repository: LibraryRepository,
    rawg_client: RawgClient,
    opencritic_client: OpenCriticClient,
) -> Callable[[str], Coroutine[Any, Any, None]]:
    """Build the ``on_library_refresh`` handler the queue consumer dispatches to.

    Unlike the module-level ``enrichment_service`` singleton, a library refresh needs a PSN catalog
    signal scoped to the refreshing user's own linked account -- so this closure builds a fresh
    :class:`~curator.psn.session.PsnSession`/:class:`~curator.psn.catalog_client.CatalogClient`/
    :class:`~curator.enrichment.enrichment_service.EnrichmentService`/
    :class:`~curator.library.library_build_orchestrator.LibraryBuildOrchestrator` per job instead of
    reusing one global instance.
    """

    async def handle(identity_sub: str) -> None:
        token_store = DbTokenStore(identity_sub, repository, token_crypto)
        saved = await token_store.load()
        if saved is None:
            raise RuntimeError(f"No PSN link for user {identity_sub!r}; cannot refresh library.")

        session = await PsnSession.restore(None, token_store)
        library_client = LibraryClient(session)
        catalog_client = CatalogClient(session)
        ingestion_service = IngestionService(library_client, catalog_repository)
        per_user_enrichment_service = EnrichmentService(
            rawg_client=rawg_client,
            opencritic_client=opencritic_client,
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

        publisher_tier_rules = await enrichment_repository.list_publisher_tier_rules()
        size_estimates = await catalog_repository.get_size_estimates()
        await orchestrator.build(identity_sub, publisher_tier_rules=publisher_tier_rules, size_estimates=size_estimates)

    return handle


def _default_agent_factory(repository: Repository, token_crypto: TokenCrypto) -> AgentFactory:
    """Build the production ``agent_factory``: a real :class:`~curator.psn.account_client.AccountClient`
    per call, backed by a fresh :class:`~curator.persistence.db_token_store.DbTokenStore` for the given
    user and a :class:`~curator.psn.session.PsnSession` restored (or freshly bootstrapped from ``npsso``)
    against it.
    """

    async def factory(sub: str, npsso: str | None = None) -> PsnAgentLike:
        token_store = DbTokenStore(sub, repository, token_crypto)
        session = await PsnSession.restore(npsso, token_store)
        return AccountClient(session)

    return factory
