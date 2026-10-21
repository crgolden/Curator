# Curator

[![Build and deploy Python app to Azure Web App - crgolden-curator](https://github.com/crgolden/Curator/actions/workflows/main_crgolden-curator.yml/badge.svg)](https://github.com/crgolden/Curator/actions/workflows/main_crgolden-curator.yml)

[![Quality gate](https://sonarcloud.io/api/project_badges/quality_gate?project=crgolden_Curator)](https://sonarcloud.io/summary/new_code?id=crgolden_Curator)

A multi-user PlayStation library curation API. A user signs in with any OpenID Connect provider and calls
Curator with a bearer access token; Curator is a **JWT bearer resource server** and never issues tokens or
holds a session. Once signed in, a user links their PlayStation Network (PSN) account, and Curator ingests
their entitlements into a shared game catalog, enriches that catalog (RAWG, OpenCritic, the PS Store), and
derives a per-user library, console inventory and named **collections** a user can share by link.

The long-running work (pulling entitlements, enrichment, trophy matching) runs in a separate background
worker that reads the same database. Curator creates the `job_runs` row, publishes the job message to Azure
Service Bus, and serves the run's status, so `POST /library/refresh` and `POST /enrichment/runs` answer
with a `run_id` instead of a result.

How the parts fit together, the auth model, the privacy and security rules, and the data model:
[ARCHITECTURE.md](ARCHITECTURE.md). How to run every check and test tier: [TESTING.md](TESTING.md).

## Technologies

- Python 3.14 (CI's version; `pyproject.toml` admits 3.10 and later), FastAPI, served by uvicorn locally and
  by App Service's gunicorn on Azure App Service (Linux).
- PostgreSQL 17 through `psycopg` 3 and one `psycopg_pool.AsyncConnectionPool`: raw parameterized SQL, no
  ORM, schema as plain SQL migrations in [`db/migrations/`](db/migrations/).
- Redis (trophy-read cache and the shared PSN rate limiter), Azure Service Bus (job queues).
- `joserfc` JWT validation against the OIDC provider's JWKS; AES-256-GCM (`cryptography`) for secrets at rest.
- `httpx` with `tenacity` retries for outbound calls (PSN, the PS Store, RAWG, OpenCritic).
- OpenTelemetry (OTLP gRPC) and Elasticsearch structured logging, both optional off App Service.
- Poetry, pytest with `pytest-xdist` and `pytest-cov`, Ruff, mypy `--strict`, SonarCloud.

## Prerequisites

- Python 3.14 and Poetry (`python -m pip install poetry`).
- To run the unit tests: nothing else. The unit suite is offline.
- To run the schema tests: a PostgreSQL 17 database whose name ends in `_test`, owned by a login role
  without superuser rights ([TESTING.md](TESTING.md)).
- To run the app: PostgreSQL 17 with a second database for manual work, a reachable Redis, an Azure Service
  Bus namespace (or a connection string) with the queues `curator-library-refresh`,
  `curator-library-refresh-continuation`, `curator-enrichment` and `curator-scheduled-refresh`, and an OIDC
  provider that issues access tokens carrying the `curator` scope and an `email` claim.

## Clone and install

```powershell
git clone https://github.com/crgolden/Curator.git
cd Curator
$env:POETRY_VIRTUALENVS_CREATE = 'false'
poetry install --extras dev
```

This is CI's install. Leave out the first assignment to install into a Poetry-managed virtual environment
instead.

## Configuration

Every setting resolves from, in order, an explicit argument, an environment variable, then a `.env` file in
the repo root (`curator.persistence.config`). Copy [`.env.example`](.env.example) to `.env` (gitignored) and
fill it in; it documents each setting and which are required. A required setting that is missing stops
startup with a `ConfigError` naming it.

| Setting | Required | Purpose |
|---|---|---|
| `OIDC_AUTHORITY` | yes | the OIDC provider's base URL: the JWKS source and the expected `iss` |
| `CURATOR_TOKEN_KEY` | yes | AES-256-GCM key for secrets at rest; `.env.example` shows how to generate one |
| `CURATOR_DATABASE_URL` | yes | the app's PostgreSQL connection URL |
| `ServiceBusNamespace` or `ServiceBusConnectionString` | one of them | the job queues; the namespace form signs in with `DefaultAzureCredential` |
| `RedisHost`, `RedisPort`, `RedisSsl`; `RedisPassword` when the server has one | yes | cache and rate limiter; Curator pings Redis at startup |
| `StoreGraphqlEndpoint`, `StoreQueryHash__0` (and `__1`, ... as fallbacks) | yes | the anonymous PS Store storefront gateway |
| `AlloyEndpoint`; `ElasticsearchNode`, `ElasticsearchUsername`, `ElasticsearchPassword` | on App Service | telemetry; off when unset elsewhere |
| `CURATOR_TEST_DATABASE_URL` | for schema tests | the disposable `*_test` database ([TESTING.md](TESTING.md)) |
| `CURATOR_MIGRATIONS_DATABASE_URL` | no | the migration runner's target when no argument is given |

Never put a connection string on a command line: it carries the password, and a tool that fails to connect
prints it.

## Run locally

Apply the migrations to your manual-work database, then start the development server:

```powershell
python db/run_migrations.py "$env:CURATOR_DATABASE_URL"
python dev_server.py
```

`run_migrations.py` applies every file in `db/migrations/` not yet recorded in `schema_migrations`, so
rerunning it is a no-op. With no argument it reads `CURATOR_MIGRATIONS_DATABASE_URL`, which keeps the URL
off the command line.

[`dev_server.py`](dev_server.py) starts uvicorn with `--reload` against `curator.app:create_app`, and the
interactive API docs are at `/docs`. On Windows it builds a `SelectorEventLoop`
([`curator._windows_event_loop`](src/curator/_windows_event_loop.py)), because psycopg's async mode needs
`add_reader`/`add_writer`, which Windows' default `ProactorEventLoop` does not implement. `app.py` is the
App Service entry point and is not used locally.

Admin routes require the `curator.admin` claim in the access token.

## Tests

```powershell
python -m pytest
```

The unit suite needs no database, network or secrets. With `CURATOR_TEST_DATABASE_URL` set, the same run
also applies every migration to that database and runs the schema tests; any `skipped` count means they did
not run. [TESTING.md](TESTING.md) covers lint, format and type checks, both test tiers, and rehearsing a
migration.

## Gate

`gate.ps1` is the maintainers' gate. It loads a private toolkit from a folder beside this repo, so in a plain
clone it stops at once. To gate your own change, run the checks CI runs, in this order, from the repo root:

```powershell
poetry check --lock
python -m ruff check src tests app.py dev_server.py
python -m ruff format --check src tests app.py dev_server.py
$env:MYPYPATH = 'src'
python -m mypy src tests app.py dev_server.py
python -m pytest -x --cov=src/curator --cov-report=term -q
```

Run the last command with `CURATOR_TEST_DATABASE_URL` set, so the schema tier runs as it does in CI. Every
command must exit 0. CI additionally runs a SonarCloud analysis and fails on any open issue.

## Deployment

[`.github/workflows/main_crgolden-curator.yml`](.github/workflows/main_crgolden-curator.yml) runs on every
push and pull request. `Test` runs the checks above against a `postgres:17` service container named
`curator_test`, then SonarCloud. On a push to `main` three more jobs follow, each gated on the one before:

1. `Package` assembles `src/`, `db/`, `pyproject.toml`, `poetry.lock` and `app.py`, and imports `app` from a
   fresh virtual environment under placeholder settings to prove the package boots.
2. `Migrate` applies pending migrations to the CI test database and then to production, and dry-runs
   `db/reencrypt_tokens.py` to prove every secret at rest still decrypts.
3. `Deploy` signs in to Azure over OIDC federated credentials (no client secret) and deploys the package to
   the App Service app, which installs from `poetry.lock` during deployment.

| Repository secret or variable | Used by |
|---|---|
| `SONAR_TOKEN` (secret); `SONAR_PROJECT_KEY`, `SONAR_ORGANIZATION` (variables) | `Test` |
| `CURATOR_TEST_DATABASE_URL`, `CURATOR_DATABASE_URL`, `CURATOR_TOKEN_KEY` | `Migrate` |
| `AZUREAPPSERVICE_CLIENTID_*`, `AZUREAPPSERVICE_TENANTID_*`, `AZUREAPPSERVICE_SUBSCRIPTIONID_*` | `Deploy` |

There is no module-level `app = create_app()`: building the app resolves every required setting, which
importing a module must never require.

## Layout

| Path | What it holds |
|---|---|
| `src/curator/app.py`, `settings.py`, `deps.py`, `token_validation.py` | the app factory, settings, and the bearer-token gate every route depends on |
| `src/curator/*_routes.py` | one module per route group; route docstrings are the published OpenAPI descriptions |
| `src/curator/psn/` | the in-repo PSN client: npsso auth and session, trophies, social, presence, the storefront, the shared rate limiter |
| `src/curator/catalog/`, `library/`, `collections/`, `scoring/`, `enrichment/` | the shared catalog and store walks, per-user libraries, collection strategies, ranking and size estimation, the RAWG and OpenCritic key validators |
| `src/curator/jobs/` | queue publishing, `job_runs` status, the queue-depth monitor and the PS Plus walk scheduler |
| `src/curator/persistence/`, `audit/` | settings resolution, the database URL, `TokenCrypto`, the data-access classes, and the account action log |
| `src/curator/telemetry.py` | OTLP traces and metrics, Elasticsearch logging |
| `db/migrations/`, `db/run_migrations.py`, `db/reencrypt_tokens.py` | the schema, its runner, and the secrets-at-rest verifier |
| `tests/` | the offline suite plus the gated `tests/test_schema.py` |
| `app.py`, `dev_server.py` | the App Service entry point and the local one |
