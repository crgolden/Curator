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

RAWG/OpenCritic enrichment is **bring-your-own-key**: Curator never provisions a shared RAWG/OpenCritic key
for every user's library (it doesn't scale), so a user optionally supplies their own key for either or both
providers (`GET/PUT/DELETE /me/enrichment-keys/{rawg|opencritic}`), encrypted at rest and used only for
their own enrichment. See "Security considerations" below.

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

PSN data-harvesting is also gated per user, independent of scope/email checks: `psn_links` carries four
boolean opt-in flags (`harvest_trophies`, `harvest_identity`, `harvest_presence`, `harvest_devices`), all
`false` by default, settable only by the linked user themselves via `GET`/`PUT /me/psn-preferences`.
`curator.deps.require_preference` 404s if the caller has no PSN link at all, 403s if the specific category's
flag is off, and is called inline at the top of `trophy_routes.py`/`identity_routes.py`/
`presence_routes.py`/`devices_routes.py`'s handlers before any PSN call is made — enforcement lives in the
API, not just hidden in a UI toggle.

## Social profile

Every user has a public profile, keyed by their Identity `sub`, that other users can view and follow.
A profile is **private by default** (`user_profiles.is_public = false`) until the owner turns it on from
their own profile settings page — no row exists until a user visits that page for the first time, and a
missing row behaves exactly like a private, all-toggles-off profile rather than a 404.

An owner controls four independent display toggles, each shown on their profile only if it's on:
`show_library`, `show_collections`, `show_trophies`, `show_identity`. None of these toggles works alone.
`show_library`/`show_collections` also require `is_public`. `show_trophies`/`show_identity` require both
`is_public` **and** the matching PSN data-harvesting flag (`psn_links.harvest_trophies`/`harvest_identity`)
— a user has to opt in twice, once to harvest the data at all and once to show it publicly, before a
stranger ever sees it. The owner viewing their own profile always sees their own sections, governed by
their own `show_*`/`harvest_*` settings, never by `is_public` — `is_public` only controls what *other*
viewers see.

Following is a simple, first-party directed graph (`follows`), unrelated to PSN. Follow/unfollow is
idempotent — following someone already followed, or unfollowing someone not followed, is a no-op, not an
error. **Follower/following counts and lists are always visible on any profile, regardless of whether
that profile is public.** They're Curator's own data, not PSN-derived, so there's nothing to gate: you can
see who follows a private profile and who it follows, even though you can't see that profile's library,
collections, trophies, or PSN identity unless the owner has made them public.

That split — follow graph always visible, PSN/library/collections content gated — is deliberate. A
private profile still participates fully in the social graph (findable, followable, with visible
follower/following lists); it just doesn't expose anything PSN-derived or curation-derived until the
owner opts in.

## Security considerations

- **`PUT /me/enrichment-keys/{provider}` validates the key against the real provider before persisting
  anything** — one cheap request (RAWG: `GET /genres?page_size=1`; OpenCritic: one page of the same
  non-search catalog listing every other OpenCritic call in this codebase uses) confirms the key is
  accepted. A rejected key (401/403) is a 400 and never written to the database; a provider that can't be
  reached at all is a 503, also never persisted — either way the caller finds out immediately and gets a
  chance to correct it, instead of only discovering a bad key when a later library refresh silently fails.
- **Per-user BYOK keys are encrypted with the same Fernet key as PSN tokens** (`curator.persistence.crypto.TokenCrypto`,
  keyed by `CURATOR_TOKEN_KEY`) — no separate encryption key was introduced for this feature. `GET
  /me/enrichment-keys` never returns a key value, only whether one is configured and when it was added;
  `PUT`/`DELETE` never echo the value back either.
- **A user's own key is never exposed to anyone else** — but the *game metadata* it retrieves (title,
  genre, score) is written into the shared `rawg_cache`/`opencritic_cache` tables so future lookups for
  that game, by any user or the admin's own catalog-wide re-scrape, don't need to call RAWG/OpenCritic
  again. This is a deliberate, disclosed trade-off (see Librarian's `/faq` and `/privacy` pages) — only the
  retrieved data is shared, never the key.
- **No display-name field exists anywhere in the social-profile feature.** The only identity a profile
  shows is its PSN account id (if linked and shown) or "Unlinked user" — there was never a free-text name
  field to sanitize, spoof-check, or moderate.
- **Region is structurally absent from the profile identity response, not just hidden in the UI.**
  `ProfileIdentityResponse` has no `region` field at all — PSN only ever exposes an account's region to
  that account itself, which makes it meaningless for a cross-user lookup like this one (see below), and
  it isn't useful for browsing or curation anyway. Librarian also removed the region display from the
  existing `/psn` identity card, so region no longer appears anywhere in the product, even for a user's
  own account.
- **Cross-user PSN lookups always use the viewer's own token, never the profile owner's.** When viewer B
  looks at owner A's public profile, Curator builds B's own `TrophyClient`/`SocialClient` — the same
  client B's own requests already use — and calls it with A's `psn_account_id` as the target. A's stored
  token is never touched on B's behalf; B's own live PSN session is simply looking up A's already-public
  PSN account, the same way friend/profile lookups against an arbitrary account id already work. If B has
  no PSN link of their own, or PSN rejects B's stored token, the trophy/identity sections are silently
  omitted from the response rather than erroring — another user's PSN state can never break a profile page
  render.
- **Self-follow is rejected at both the database and the route layer.** The `follows` table has a
  `follows_no_self_follow` CHECK constraint, and the follow route additionally checks `sub == claims.sub`
  itself before that constraint would ever fire, so a self-follow attempt gets a clean 400 instead of a
  raw database error.
- **Follow/unfollow actions are recorded in `account_action_log`** (`followed`/`unfollowed`), the same
  defensive audit trail used for linking, unlinking, and library refreshes. The logged detail is the other
  user's `sub` only — never PSN data.
- **A cautionary tale for any future external API integration that puts a secret in a URL query
  parameter**: RAWG's API key (`?key=...`) was found leaking into `job_runs.error`, the browser, Elasticsearch
  logs, and Tempo spans during this feature's own live testing, before BYOK even shipped. Closed at three
  independent points, all required together: (1) `curator.enrichment.rawg_client.RawgApiError` re-raises
  `from None` so a sanitized message (`"RAWG request failed with status {code}"`) is the only thing a
  downstream `logger.exception(...)` can render — the original `httpx.HTTPStatusError`'s message, which
  embeds the full URL, is fully suppressed from the exception chain; (2) `curator.telemetry`'s httpx
  instrumentation `async_request_hook` strips the `key` query param from the OTel span attribute after the
  instrumentor sets it (OTel's own built-in query-param redaction only covers a fixed, unrelated allowlist —
  `AWSAccessKeyId`/`Signature`/`sig`/`X-Goog-Signature` — not `key`); (3) `curator.jobs.error_messages.friendly_job_error`
  maps every caught job exception to short, category-based, safe text before it's ever persisted to
  `job_runs.error` or returned from `GET /library/refresh/{run_id}`, so even a future exception type nobody
  thought to sanitize can't leak anything through that path.

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
doesn't implement — it raises `NotImplementedError` the first time a real query runs. On Windows,
`dev_server.py` points `uvicorn.run(..., loop=...)` at
[`curator._windows_event_loop.selector_loop_factory`](src/curator/_windows_event_loop.py), which builds a
`SelectorEventLoop` instead. This replaces an older approach that called
`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` before starting `uvicorn`: that API
is deprecated (removal slated for Python 3.16) and, separately, stopped having any effect once uvicorn 0.36
started building its loop straight from `Config.get_loop_factory()` rather than consulting the ambient
policy — see `_windows_event_loop.py`'s module docstring for the full history. It no-ops on macOS/Linux. The
unit test suite never hits this at all (every repository test uses a hand-written fake pool, never a real
`AsyncConnectionPool`); it only matters when running the app against a real Postgres connection on Windows.
Production runs on Linux App Service and is unaffected.

## Deployment

`main_crgolden-curator.yml`'s `deploy` job (runs after `test` passes, only on push to `main`) applies
pending database migrations (see Schema above), then deploys to the Azure Linux Web App `crgolden-curator`
via `azure/webapps-deploy`, authenticating over OIDC federated credentials with `azure/login` — no client
secret. `SCM_DO_BUILD_DURING_DEPLOYMENT=true` is set on the app, so Oryx installs
[`requirements.txt`](requirements.txt) during deployment; `pyproject.toml` remains the dev/test manifest and
is not used at deploy time.

The deploy package itself contains only `src/`, `db/`, `requirements.txt`, and `app.py` — no tests, no
`.github/`, no local caches.

Required repository secrets:

| Secret | Purpose |
|---|---|
| `AZUREAPPSERVICE_CLIENTID_29C8B941EEC941B3B025951A74F5176F` | `azure/login` client id (federated credential) |
| `AZUREAPPSERVICE_TENANTID_7933356A53994B7987CBFABB49879C36` | `azure/login` tenant id |
| `AZUREAPPSERVICE_SUBSCRIPTIONID_1FCCBD0626644623AC4FF179F9E82B74` | `azure/login` subscription id |
| `CURATOR_DATABASE_URL` | Production Postgres connection string, used only by the pre-deploy migration step |

There is deliberately no module-level `app = create_app()` instance — building the real app resolves every
setting (OIDC authority, token key, database URL) at construction time, which import alone must never
require.

## Schema

The database schema lives in [`db/migrations/`](db/migrations/) as plain SQL. `0001_initial.sql` has a
header comment explaining the design (account layer, per-user append-only ingestion, the shared global
catalog, curation-rule config-as-data, and the per-user library/console/assignment tables). `0004_user_enrichment_keys.sql`
adds the per-user BYOK enrichment keys table, the `game_enrichment.rawg_enriched`/`opencritic_enriched`
checkmark columns, `job_runs.result_summary`, and `opencritic_pagination_cursor` (the shared, resumable
cursor both the admin catalog-wide re-scrape and per-user OpenCritic top-ups advance cooperatively).

[`db/run_migrations.py`](db/run_migrations.py) applies every migration file not yet recorded as applied,
tracked in a `schema_migrations` table (one row per filename) — the deploy job runs it against production
before every deploy (see Deployment below), so a new migration file lands automatically on the next push to
`main` instead of requiring a manual `psql` run. It's idempotent: rerunning it when nothing is new is a
no-op. For local development, either run it yourself (`python db/run_migrations.py "$CURATOR_DATABASE_URL"`)
or apply the files by hand via `psql` — either way ends at the same schema.

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
  preferences_routes.py             # GET/PUT /me/psn-preferences: per-category harvest opt-in flags
  enrichment_keys_routes.py           # GET/PUT/DELETE /me/enrichment-keys/{provider}: BYOK RAWG/OpenCritic keys
  trophy_routes.py                    # GET /trophies/summary|titles|titles/{id}|titles/{id}/groups
  identity_routes.py                    # GET /identity
  presence_routes.py                      # GET /presence
  devices_routes.py                         # GET /devices
  library_routes.py                           # GET /library, POST/GET /library/refresh
  profile_routes.py                             # GET/PUT /me/profile-settings, GET /users/{sub}/profile,
                                                 # POST/DELETE /users/{sub}/follow, followers/following,
                                                 # library/collections passthrough for another user's sub
  jobs/
    error_messages.py    # friendly_job_error(): sanitized, category-based job-failure text
    queue_consumer.py    # QueueConsumer: drains both job queues; LockRenewer keeps long runs' locks alive
    queue_publisher.py   # QueuePublisher: publishes library-refresh/enrichment job messages
    repository.py        # JobRunsRepository: job_runs DAO, incl. result_summary
  persistence/
    config.py          # generic arg -> env var -> .env resolution
    connection.py       # PostgreSQL connection URL resolution
    crypto.py            # TokenCrypto: Fernet encryption for tokens at rest
    repository.py        # Repository: psycopg 3 DAO over app_users / psn_links
    enrichment_keys_repository.py  # EnrichmentKeysRepository: psycopg 3 DAO over user_enrichment_keys
    profile_repository.py  # ProfileRepository: psycopg 3 DAO over user_profiles (display toggles)
    follow_repository.py   # FollowRepository: psycopg 3 DAO over follows (the follow graph)
    db_token_store.py    # DbTokenStore: curator.psn.session.TokenStore contract, backed by Repository
db/migrations/
  0001_initial.sql        # full schema
  0004_user_enrichment_keys.sql  # BYOK keys, enrichment checkmarks, job result_summary, OC pagination cursor
  0005_user_profiles.sql  # user_profiles: is_public + per-section show_* display toggles
  0006_follows.sql        # follows: the directed follow graph, plus account_action_log's followed/unfollowed actions
  run_migrations.py       # idempotent runner, applied automatically by the deploy job
tests/                     # offline pytest suite, plus the gated tests/test_schema.py
.github/workflows/
  main.yml                # CI: offline unit tests only, on push/PR/workflow_dispatch
```

## Known gaps / outstanding work

- ~~Library refresh queue not configured in production.~~ **Fixed.** `curator-library-refresh` and
  `curator-enrichment` queues exist on the shared `crgolden` Service Bus namespace; `crgolden-curator`'s
  system-assigned managed identity holds Data Sender + Data Receiver on that namespace; the App Service has
  a `ServiceBusNamespace` setting. `POST /library/refresh` and `POST /enrichment/runs` now publish for real,
  and the in-process `QueueConsumer` (started from `create_app()`'s lifespan — no separate Functions
  deployable) drains both queues, matching Identity/Directory/Functions' production convention of
  managed-identity-only access (the shared namespace has `DisableLocalAuth` enabled, so a connection string
  was never going to work there). `service_bus_connection_string` remains only as a local-dev/CI fallback.
- ~~Every library refresh 401s because no shared RAWG/OpenCritic key is configured.~~ **Fixed by design,
  not by provisioning a shared key.** Enrichment is bring-your-own-key — a user with no key configured for
  a provider simply gets no signal from that provider (not an error); RAWG/OpenCritic are cache-first
  against the shared `rawg_cache`/`opencritic_cache` tables regardless, so many titles enrich for free even
  with zero keys configured.
- ~~No endpoint returns the finished library.~~ **Fixed.** `GET /library` returns the caller's own library
  with per-provider (`rawg_enriched`/`opencritic_enriched`) checkmarks per game; `GET /library/refresh/{run_id}`
  additionally now returns a `result_summary` (newly-enriched titles per provider, whether the OpenCritic
  top-up stopped early) once a run succeeds.
- **No console list/create endpoints.** Only `PUT /consoles/{id}/installs/{gameId}` exists. Librarian's
  UI accepts a manually-typed `console_id` as a stopgap (an explicit, agreed-on decision, not a bug) — a
  404 from an unrecognized console is the expected path, not an error to chase.
- **`POST /enrichment/runs` has no UI.** It's an admin-only global operation, not a per-user feature, and
  intentionally out of scope for Librarian so far.
- **Only the trophy summary is wired into Librarian's UI, not the title-level endpoints.** Librarian's
  `/psn` preferences panel surfaces `GET /trophies/summary` as a compact, opt-in card (level/tier/progress/
  earned counts). `GET /trophies/titles`, `/trophies/titles/{id}`, and `/trophies/titles/{id}/groups` exist
  but have no UI yet — no drilldown was requested.
- **Production catalog is unseeded.** `games`/`game_enrichment` are empty in the production database, so
  Catalog/Collections pages correctly show "no results" rather than any real content — there is no
  catalog-ingestion run against production yet.
- **Elasticsearch structured logging had two separate live bugs, both now fixed but worth re-verifying
  end-to-end after the next deploy:** (1) the ES client's own transport logger re-shipping its own HTTP
  calls forever (a self-sustaining feedback loop that filled `logs-dotnet-curator` with 1M+ docs before
  dying); (2) unhandled-exception logs never reaching the root logger's Elasticsearch handler at all,
  because uvicorn's default logging config sets `propagate=False` on the `uvicorn` logger, silently
  breaking the chain from `uvicorn.error` up to root. Confirm a real production 500 now actually produces
  a document in `logs-app-curator`.
- **Production schema had drifted from `db/migrations/` before this was caught** (missing `genres` table
  and others) because nothing ever applied migrations to the live database. Now fixed by
  `db/run_migrations.py` running in the deploy job — but any other repo with a similar "migrations exist
  in source but nothing runs them" gap should be checked.
