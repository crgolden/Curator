# Testing

## Lint, format, and type checking

Ruff (lint + format) and mypy (strict) run against `src/` and `tests/`, configured in `pyproject.toml`
(`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`). Run them locally the same way CI does:

```powershell
python -m pip install -e ".[dev]"

python -m ruff check src tests
python -m ruff format --check src tests   # drop --check to apply formatting
$env:MYPYPATH = "src"
python -m mypy src tests
```

`mypy` is strict on `src/curator` (no untyped defs, no implicit `Any`); `[[tool.mypy.overrides]]` in
`pyproject.toml` relaxes a few checks for `tests/` (hand-written fake collaborators use structural, not
nominal, typing — see "Unit tests" below).

## Unit tests

The whole suite under `tests/` runs fully offline: no live database, no network, no live PSN/Identity
calls. Backends (the psycopg connection/cursor protocol, the `Repository`, the PSN agent, and
`curator.token_validation.JwtValidator`) are stood in for with hand-written fake classes — never
`unittest.mock`. `tests/test_token_validation.py` is the one place that exercises the *real*
`JwtValidator`: it generates a local RSA key with joserfc, signs canned tokens, and serves the
discovery/JWKS documents through an injected fake `fetch_json` — no network access even there.

Curator is a pure JWT Bearer resource server — there is no session, no cookie, no login route — so every
protected-route test presents an `Authorization: Bearer <token>` header; `tests/test_routes.py`'s
`FakeTokenValidator` maps known token strings to canned `TokenClaims` and raises `TokenError` for anything
else.

`tests/test_authz.py` exercises this offline (`tests/test_routes.py`'s fakes, reused by importing them
rather than duplicating — pytest's rootdir-relative import puts `tests/` on `sys.path`, so a bare
`from test_routes import ...` resolves) but proves a structural property rather than individual status
codes: every bearer-required route (`GET /me`, `POST /psn/link`, `DELETE /psn/link`) rejects both a
missing `Authorization` header and a garbage/invalid token; two established callers (user A, user B) never
leak — A's requests only ever read/write A's row in the fake repository, B's is provably untouched; and no
route in the app exposes a path parameter at all (the obvious place a caller-supplied "target user"
identifier could sneak in), which the test locks in via introspecting `app.routes`.

Run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

`pyproject.toml` sets `pythonpath = ["src"]` and `testpaths = ["tests"]`, so `python -m pytest` from the
repo root picks up `src/curator` without an editable install of Curator itself. If running from a
different working directory, either `Set-Location` into the repo root first or pass the tests directory
and `-o pythonpath=<repo>/src` explicitly (or set the `PYTHONPATH` env var to `<repo>/src`).

If `pip install -e ".[dev]"` doesn't resolve every dependency in your environment, install the runtime
packages directly:

```powershell
python -m pip install pytest httpx fastapi uvicorn joserfc cryptography "psycopg[binary]" psycopg-pool redis azure-servicebus pycountry
python -m pytest tests -q
```

`tests/test_telemetry.py` covers `curator.telemetry`: both legs (OTLP traces/metrics, Elasticsearch
logging) stay no-op when their settings are absent; the module-level registration guards make repeated
`create_app` calls never stack a second provider or handler; `FastAPIInstrumentor.instrument_app` is called
with `/health` excluded; and the Elasticsearch log-document formatter produces the expected flat `log.level`
/ `service.name` keys. Every OTel/Elasticsearch collaborator (`TracerProvider`, `MeterProvider`, the OTLP
exporters, the psycopg/requests instrumentors, the `Elasticsearch` client, `QueueListener`) is a
hand-written fake swapped in via `monkeypatch.setattr` — no live OTLP collector, no live Elasticsearch node,
no `unittest.mock`.

## Integration tests (schema, gated — opt-in only)

`tests/test_schema.py` is the one place in this suite that touches a real PostgreSQL instance. It is
gated on the `CURATOR_TEST_DATABASE_URL` environment variable via a module-level `pytest.mark.skipif`:
unset (the default — nothing to configure for a plain `python -m pytest` run, and CI never sets it), every
test in the module is skipped rather than run against a fake. When set, it applies the full
`db/migrations/0001_initial.sql` migration and every insert inside one transaction per test, then rolls
that transaction back in teardown — so a correctly-configured database is left exactly as it started.

**Only ever point `CURATOR_TEST_DATABASE_URL` at a disposable, throwaway database created solely for this
purpose — never a shared or production database.** The rollback-per-test discipline above is what makes
that safe to do repeatedly, but it still assumes the target database is not something else's.

What it checks: every table the migration is expected to create exists; representative CHECK constraints
reject an out-of-enum value (`game_assignments.collection_status`, `user_consoles.platform`,
`exclusion_rules.rule_type`); `measured_sizes` retains history (two inserts for the same
user/game/platform at different `measured_at` both persist, rather than one overwriting the other); and no
column named anything like `%email%` or `%npsso%` exists anywhere in the schema (the hard privacy tenet
documented in the migration's own header comment).

```powershell
# Create a throwaway database (adjust host/user for your environment)
psql -h <host> -U postgres -d postgres -c "CREATE DATABASE curator_schema_test_scratch"

$env:CURATOR_TEST_DATABASE_URL = "postgresql://postgres@<host>:5432/curator_schema_test_scratch"
python -m pytest tests/test_schema.py -q

# Tear down when done
psql -h <host> -U postgres -d postgres -c "DROP DATABASE curator_schema_test_scratch"
```

## CI

`.github/workflows/main.yml` runs on push to `main`, on pull requests, and on `workflow_dispatch`. It
installs Curator's runtime and dev dependencies directly (rather than `pip install -e .`) so the job
doesn't depend on any cross-repo checkout.

The `test` job runs, in order: Ruff lint, Ruff format check, mypy, then the unit test suite with coverage
(`--cov=src/curator --cov-report=xml:coverage.xml`), then a SonarCloud analysis over `coverage.xml`. Each
lint/type-check step is its own named step so a failure is attributable at a glance. `CURATOR_TEST_DATABASE_URL`
is never set in the workflow, so `test_schema.py` auto-skips; there is no PostgreSQL service in this job.
