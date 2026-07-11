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

from typing import Callable, Optional

import psycopg
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from psnpy.client import PsnAgent

from curator.link_service import PsnAgentLike
from curator.me_routes import router as me_router
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.repository import Repository
from curator.psn_routes import router as psn_router
from curator.settings import Settings
from curator.token_validation import JwtValidator, TokenValidatorLike


def create_app(
    settings: Optional[Settings] = None,
    *,
    repository: Optional[Repository] = None,
    token_crypto: Optional[TokenCrypto] = None,
    agent_factory: Optional[Callable[..., PsnAgentLike]] = None,
    token_validator: Optional[TokenValidatorLike] = None,
) -> FastAPI:
    """Build a configured Curator :class:`~fastapi.FastAPI` app.

    Every collaborator defaults to a real implementation built from ``settings``; tests inject
    hand-written fakes for all of them instead of monkeypatching. Each collaborator is stashed on
    ``app.state`` so route handlers (which see only ``request``) can reach it.

    :param settings: Resolved application settings; defaults to :meth:`Settings.from_config`.
    :param repository: The data-access layer; defaults to a real :class:`Repository` over
        ``settings.database_url``.
    :param token_crypto: The token-encryption helper; defaults to a real :class:`TokenCrypto` over
        ``settings.token_key``.
    :param agent_factory: Builds a PSN agent for a given ``sub`` (and optional ``npsso``); defaults to one
        backed by :class:`~curator.persistence.db_token_store.DbTokenStore` and
        :meth:`psnpy.client.PsnAgent.from_config`.
    :param token_validator: Validates bearer access tokens; defaults to a real
        :class:`~curator.token_validation.JwtValidator` over ``settings.oidc_authority``.
    :returns: The configured :class:`~fastapi.FastAPI` app.
    """
    settings = settings or Settings.from_config()
    repository = repository or Repository(lambda: psycopg.connect(settings.database_url))
    token_crypto = token_crypto or TokenCrypto.from_config(settings.token_key)
    agent_factory = agent_factory or _default_agent_factory(repository, token_crypto)
    token_validator = token_validator or JwtValidator(settings.oidc_authority)

    app = FastAPI(title="Curator")

    app.state.settings = settings
    app.state.repository = repository
    app.state.token_crypto = token_crypto
    app.state.agent_factory = agent_factory
    app.state.token_validator = token_validator

    app.include_router(me_router)
    app.include_router(psn_router)

    @app.get("/health")
    async def health() -> PlainTextResponse:
        """Fleet-convention health probe: plain-text ``"Healthy"``, no auth required."""
        return PlainTextResponse("Healthy")

    return app


def _default_agent_factory(
    repository: Repository, token_crypto: TokenCrypto,
) -> Callable[..., PsnAgentLike]:
    """Build the production ``agent_factory``: a real :class:`~psnpy.client.PsnAgent` per call, backed by a
    fresh :class:`~curator.persistence.db_token_store.DbTokenStore` for the given user.
    """

    def factory(sub: str, npsso: Optional[str] = None) -> PsnAgentLike:
        store = DbTokenStore(sub, repository, token_crypto)
        return PsnAgent.from_config(npsso=npsso, token_store=store)

    return factory
