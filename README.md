# Curator

A multi-user PlayStation library curation API. Every user authenticates elsewhere — through Duende
IdentityServer — and identifies themselves to Curator with a bearer access token; Curator itself is a
pure **JWT Bearer resource server**, matching how the workspace's `Directory` API interacts with Identity.
Once authenticated, a user links their PSN account through the sibling
[`psnpy`](https://github.com/crgolden/psnpy) agent, and Curator ingests their entitlements, merges them
into a shared game catalog, enriches that catalog (RAWG, OpenCritic, PS Store), and derives a per-user
library, exclusion set, console inventory, and rotation/assignment plan. All of it is persisted in
PostgreSQL.

This repository builds the API in stages. The scaffold, full database schema, and persistence layer
(config resolution, connection URL, token encryption, the account-table DAO, and a PSN token store backed
by it) came first. This stage adds the FastAPI application itself: settings resolution, JWT Bearer
validation against Identity's JWKS, and the PSN link/unlink service and routes built on the sibling
`psnpy` agent.

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

## Quick start

```powershell
python -m pip install -e ".[dev]"
python -m pip install -e ../psnpy   # editable install of the sibling psnpy agent
python -m pytest
```

The unit test suite is fully offline (hand-written fakes, no live database, no network, no live PSN/Identity
calls). See [TESTING.md](TESTING.md) for the full testing approach, including the opt-in, env-var-gated
schema integration tests that apply the migration to a real (disposable) PostgreSQL instance.

## CI

`.github/workflows/main.yml` runs the offline unit test suite on push to `main`, on pull requests, and on
`workflow_dispatch`. It never sets `CURATOR_TEST_DATABASE_URL`, so the schema integration tests always
auto-skip there. See [TESTING.md](TESTING.md#ci) for how it gets the sibling `psnpy` dependency without a
published release yet.

Run the app itself (once settings are resolvable — see `curator.settings.Settings.from_config`) with:

```powershell
uvicorn --factory curator.app:create_app
```

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
  me_routes.py                # GET /me
  link_service.py               # link()/unlink(): PSN account linking, email verification rules
  psn_routes.py                   # POST/DELETE /psn/link
  persistence/
    config.py          # generic arg -> env var -> .env resolution
    connection.py       # PostgreSQL connection URL resolution
    crypto.py            # TokenCrypto: Fernet encryption for tokens at rest
    repository.py        # Repository: psycopg 3 DAO over app_users / psn_links
    db_token_store.py    # DbTokenStore: psnpy TokenStore contract, backed by Repository
db/migrations/
  0001_initial.sql        # full schema, applied manually via psql
tests/                     # offline pytest suite, plus the gated tests/test_schema.py
.github/workflows/
  main.yml                # CI: offline unit tests only, on push/PR/workflow_dispatch
```
