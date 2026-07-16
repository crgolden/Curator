# Curator

[![Build and deploy Python app to Azure Web App - crgolden-curator](https://github.com/crgolden/Curator/actions/workflows/main_crgolden-curator.yml/badge.svg)](https://github.com/crgolden/Curator/actions/workflows/main_crgolden-curator.yml)

[![Quality gate](https://sonarcloud.io/api/project_badges/quality_gate?project=crgolden_Curator)](https://sonarcloud.io/summary/new_code?id=crgolden_Curator)

A multi-user PlayStation library curation API. Every user authenticates elsewhere — through Duende
IdentityServer — and identifies themselves to Curator with a bearer access token; Curator itself is a
pure **JWT Bearer resource server**, matching how the workspace's `Directory` API interacts with Identity.
Once authenticated, a user links their PSN account through Curator's own in-repo `curator.psn` client
(`curator.psn.session.PsnSession` and friends), and Curator ingests their entitlements, merges them into a
shared game catalog, enriches that catalog (RAWG, OpenCritic, PS Store), and derives a per-user library,
exclusion set, console inventory, and rotation/assignment plan. All of it is persisted in PostgreSQL.

This repository builds the API in stages. The scaffold, full database schema, and persistence layer
(config resolution, connection URL, token encryption, the account-table DAO, and a PSN token store backed
by it) came first. This stage adds the FastAPI application itself: settings resolution, JWT Bearer
validation against Identity's JWKS, and the PSN link/unlink service and routes built on `curator.psn`.

## Auth model

Curator never issues tokens, never redirects a browser through a login flow, and holds no session or
cookie of its own — there is no OIDC client registration anywhere in this codebase. The sole intended
client is the future BFF (or any other caller holding a valid access token); it presents an
`Authorization: Bearer <token>` header on every request.

Identity issues JWT access tokens. A `curator` ApiScope is configured with an `email` user claim, so any
access token carrying `scope: curator` also carries the user's `email` claim; `sub` is the user's
Identity GUID. `curator.token_validation.JwtValidator` validates every presented token:

- RS256 signature, verified against Identity's published JWKS (fetched from
  `{OIDC_AUTHORITY}/.well-known/openid-configuration` → `jwks_uri`, cached and refetched on an unrecognized
  `kid` to cover key rotation)
- `iss` equals the configured authority
- `exp`/`nbf` (not expired, not used before its validity window)
- `curator` present in the `scope` claim (Duende emits JWT scopes as a JSON array; a legacy
  space-delimited string is accepted too)

**Audience is not validated** — this mirrors `Directory`'s `ValidateAudience = false`; Identity issues
these tokens with no Curator-specific audience.

`curator.deps.require_bearer` is the dependency every protected route uses: 401 (with
`WWW-Authenticate: Bearer`) on a missing/malformed/invalid token, 403 if the token lacks the `curator`
scope. Routes that compare emails (`/me`'s re-verify, `POST`/`DELETE /psn/link`) additionally depend on
`curator.deps.require_verified_caller`, which further 403s a token missing the `email` claim — a verified
Identity email is mandatory for those, never treated as an absent-but-fine value.

## Design conventions

**Hand-written fakes, never `unittest.mock`.** Every test double in this repo (`FakeRepository`,
`FakeSession`, `FakeTokenStore`, ...) is a plain class with real methods, not a `Mock()`/`MagicMock()`. A
`Mock` auto-creates any attribute you touch, so a typo'd method name on the production side just returns
another `Mock` instead of failing the test. A hand-written fake has no such attribute unless you wrote it,
so a mismatch between the fake and the real collaborator's interface surfaces as a normal `AttributeError`
at the call site, not a silently-passing test. It also means a test reads as "given this fake data, assert
this real transformation" instead of a string of `.return_value`/`.assert_called_with` configuration calls
scattered through the test body.

**`typing.Protocol` over `abc.ABC` for injected collaborators.** `curator.psn.session.TokenStore`,
`curator.link_service.PsnAgentLike`, and similar contracts are structural (`Protocol`), not nominal
(`ABC`) — a class satisfies them by having the right async methods, not by inheriting from anything. This
keeps modules that shouldn't know about each other decoupled (`curator.persistence.DbTokenStore` satisfies
`curator.psn.session.TokenStore` without either module importing the other) and lets every hand-written
fake satisfy a contract for free, with no `class FakeTokenStore(TokenStore):` boilerplate or
`@abstractmethod` ceremony. The risk a `Protocol` normally carries — a mismatched fake or implementation
isn't caught until it actually runs — is closed here by `mypy --strict` running in CI on every module: a
missing or wrong-signature method fails type-checking before a test ever executes it. An `ABC` would be the
better choice if a contract needed shared base-class behavior (not just a shared shape), or if this repo
didn't already enforce strict mypy — under those conditions the earlier, class-definition-time failure an
`ABC` gives you would be worth the added coupling.

## Telemetry

Two independent, optional legs, wired up in `curator.telemetry.configure_telemetry` and invoked once from
`create_app`:

- **Traces + metrics**: OTLP gRPC to Grafana Alloy, enabled by setting `AlloyEndpoint`. Resource
  `service.name` is `curator`; the FastAPI app, psycopg, and outbound `httpx` calls (covers `curator.psn`'s
  calls to Sony) are all instrumented. `/health` is excluded from tracing, matching the fleet convention.
- **Structured logging**: root-logger documents shipped to Elasticsearch, enabled by setting
  `ElasticsearchNode` together with `ElasticsearchUsername`/`ElasticsearchPassword`. Each document carries
  `service.name: "curator"` and a flat `log.level` field translated to the fleet's Serilog/ECS vocabulary
  (`Information`/`Warning`/`Error`/... rather than Python's own `INFO`/`WARNING`/`ERROR`) — mirroring what
  the `Churches` Node app ships — written into the `logs-app-curator` data stream (`op_type="create"`,
  matching the Grafana Elasticsearch datasource pattern `logs-app-*` and Elasticsearch's built-in `logs`
  index template, so it rolls over/retains under the same managed ILM policy as every other app instead of
  accumulating forever in an unmanaged index). Shipping runs on a background thread (a
  `QueueHandler`/`QueueListener` pair), so a slow or unreachable Elasticsearch node never blocks a request.

Both legs are **no-op when their settings are unset** — the default for local dev and CI — and neither can
ever prevent the app from starting: every telemetry init path is wrapped in its own broad `except Exception`
that logs to stderr and continues. Each leg's global state (the OTel providers, the library instrumentors,
the Elasticsearch log handler) is registered at most once per process, so calling `create_app` more than
once — as the test suite and, incidentally, each gunicorn worker's own factory call do — never stacks a
second provider on top of the first.

## Quick start

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

The unit test suite is fully offline (hand-written fakes, no live database, no network, no live PSN/Identity
calls). See [TESTING.md](TESTING.md) for the full testing approach, including the opt-in, env-var-gated
schema integration tests that apply the migration to a real (disposable) PostgreSQL instance.

To run the app itself locally (not needed just to run the test suite), copy [`.env.example`](.env.example)
to `.env` and fill in real values — every field `Settings.from_config` resolves is documented there, with
required vs. optional called out. Point `CURATOR_DATABASE_URL` at your own local PostgreSQL instance (not
the shared production server) and apply [`db/migrations/0001_initial.sql`](db/migrations/0001_initial.sql)
to it via `psql` before starting the app.

## CI

`.github/workflows/main.yml` runs Ruff lint, Ruff format check, mypy, the offline unit test suite (with
coverage), and a SonarCloud analysis on push to `main`, on pull requests, and on `workflow_dispatch`. It
never sets `CURATOR_TEST_DATABASE_URL`, so the schema integration tests always auto-skip there. See
[TESTING.md](TESTING.md#ci) for the local lint/type-check commands.

Run the app itself locally (once `.env` is filled in — see Quick Start above) with:

```powershell
python dev_server.py
```

[`dev_server.py`](dev_server.py) is the local-only entry point (`app.py` is the separate Azure App Service
entry point — see Deployment below): it starts `uvicorn` with `--reload` against `curator.app:create_app`.
It also works around a **Windows-only gotcha**: `psycopg`'s async mode waits on the connection socket via
`loop.add_reader()`/`add_writer()`, which Windows' default `ProactorEventLoop` (used since Python 3.8)
doesn't implement — it raises `NotImplementedError` the first time a real query runs. The event-loop policy
that fixes this must be set *before* the event loop is created, i.e. before `uvicorn` starts — too early for
anything inside `curator.app` itself to set it, which is why this lives in a dedicated entry point rather
than a one-liner a developer has to remember to paste in. It no-ops on macOS/Linux. The unit test suite
never hits this at all (every repository test uses a hand-written fake pool, never a real
`AsyncConnectionPool`); it only matters when running the app against a real Postgres connection on Windows.
Production runs on Linux App Service and is unaffected.

## Deployment

`main.yml`'s `deploy` job (runs after `test` passes, only on push to `main`) deploys to the Azure Linux Web
App `crgolden-curator` via `azure/webapps-deploy`, authenticating over OIDC federated credentials with
`azure/login` — no client secret. `SCM_DO_BUILD_DURING_DEPLOYMENT=true` is set on the app, so Oryx installs
[`requirements.txt`](requirements.txt) during deployment; `pyproject.toml` remains the dev/test manifest and
is not used at deploy time.

The deploy package itself contains only `src/`, `db/`, `requirements.txt`, and `app.py` — no tests, no
`.github/`, no local caches.

Required repository secrets:

| Secret | Purpose |
|---|---|
| `AZUREAPPSERVICE_CLIENTID_C4CF7EE65BC442259601FFDB3B86513D` | `azure/login` client id (federated credential) |
| `AZUREAPPSERVICE_TENANTID_D1FC42A8E15547A18F5A397D64F179D0` | `azure/login` tenant id |
| `AZUREAPPSERVICE_SUBSCRIPTIONID_C6E3B3EB281E4D4C85FC7D1501EE1170` | `azure/login` subscription id |

There is deliberately no module-level `app = create_app()` instance — building the real app resolves every
setting (OIDC authority, token key, database URL) at construction time, which import alone must never
require.

## Schema

The database schema lives in [`db/migrations/`](db/migrations/) as plain SQL, applied by hand via
`psql` — there is no migration-runner dependency. `0001_initial.sql` has a header comment explaining the
design (account layer, per-user append-only ingestion, the shared global catalog, curation-rule
config-as-data, and the per-user library/console/assignment tables).

## Layout

```
src/curator/
  settings.py           # Settings: resolves OIDC authority/token/database config
  token_validation.py    # JwtValidator: RS256 bearer-token validation against Identity's JWKS
  deps.py                  # require_bearer / require_verified_caller: the auth gates every route depends on
  reverify.py               # reverify_link(): re-check a stored PSN link against the caller's token
  app.py                     # create_app(): FastAPI factory, DI seams on app.state
  telemetry.py                # configure_telemetry(): optional OTLP traces/metrics + Elasticsearch logging
  me_routes.py                # GET /me
  link_service.py               # link()/unlink(): PSN account linking, email verification rules
  psn_routes.py                   # POST/DELETE /psn/link
  persistence/
    config.py          # generic arg -> env var -> .env resolution
    connection.py       # PostgreSQL connection URL resolution
    crypto.py            # TokenCrypto: Fernet encryption for tokens at rest
    repository.py        # Repository: psycopg 3 DAO over app_users / psn_links
    db_token_store.py    # DbTokenStore: curator.psn.session.TokenStore contract, backed by Repository
db/migrations/
  0001_initial.sql        # full schema, applied manually via psql
tests/                     # offline pytest suite, plus the gated tests/test_schema.py
.github/workflows/
  main.yml                # CI: offline unit tests only, on push/PR/workflow_dispatch
```
