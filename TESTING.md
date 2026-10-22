# Testing

## Test tiers

| Tier | Tool | Location | Needs | Runs in CI |
|---|---|---|---|---|
| Lint, format and types | Ruff, mypy `--strict` | `src/`, `tests/`, `app.py`, `dev_server.py` | nothing | every push and pull request |
| Unit | pytest, hand-written fakes | `tests/` | nothing: no database, network or secret | every push and pull request |
| Schema (integration) | pytest against PostgreSQL | `tests/test_schema.py` | `CURATOR_TEST_DATABASE_URL` naming a `*_test` database; skipped without it | every push and pull request, against a `postgres:17` service container |

## Running tests locally

```powershell
$env:POETRY_VIRTUALENVS_CREATE = 'false'
poetry install --extras dev
python -m ruff check src tests app.py dev_server.py
python -m ruff format --check src tests app.py dev_server.py
$env:MYPYPATH = 'src'
python -m mypy src tests app.py dev_server.py
python -m pytest
```

`python -m pytest` runs the unit tier and, with `CURATOR_TEST_DATABASE_URL` in the environment, the schema tier as
well; any `skipped` count means the schema tier did not run. Each tier is described below.

## Lint, format, and type checking

Ruff (lint + format) and mypy (strict) run against `src/` and `tests/`, configured in `pyproject.toml`
(`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`). Run them locally the same way CI does:

```powershell
$env:POETRY_VIRTUALENVS_CREATE = 'false'
poetry install --extras dev

python -m ruff check src tests app.py dev_server.py
python -m ruff format --check src tests app.py dev_server.py   # drop --check to apply formatting
$env:MYPYPATH = "src"
python -m mypy src tests app.py dev_server.py
```

`mypy` is strict on `src/curator` (no untyped defs, no implicit `Any`); `[[tool.mypy.overrides]]` in
`pyproject.toml` relaxes a few checks for `tests/` (hand-written fake collaborators use structural, not
nominal, typing — see "Unit tests" below).

## Unit tests

The whole suite under `tests/` runs fully offline: no live database, no network, no live PSN or identity-provider
calls. Backends (the psycopg connection/cursor protocol, the `Repository`, the PSN agent, and
`curator.token_validation.JwtValidator`) are stood in for with hand-written fake classes — never
`unittest.mock`. `tests/test_token_validation.py` is the one place that exercises the *real*
`JwtValidator`: it generates a local RSA key with joserfc, signs canned tokens, and serves the
discovery/JWKS documents through an injected fake `fetch_json` — no network access even there.

Curator is a pure JWT Bearer resource server — there is no session, no cookie, no login route — so every
protected-route test presents an `Authorization: Bearer <token>` header; `tests/test_routes.py`'s
`FakeTokenValidator` maps known token strings to canned `TokenClaims` and raises `TokenError` for anything
else.

**Hand-written fakes, never `unittest.mock`.** A `Mock` creates any attribute you touch, so a misspelled
method on the production side returns another `Mock` and the test passes. A hand-written fake has only the
methods someone wrote, so a mismatch with the real collaborator fails at the call site with an
`AttributeError`, and a test reads as "given this data, assert this transformation".

**`typing.Protocol` over `abc.ABC` for injected collaborators.** `curator.psn.session.TokenStore`,
`curator.link_service.PsnAgentLike` and similar contracts are structural: a class satisfies one by having
the right methods, not by inheriting. That keeps modules decoupled (`curator.persistence.DbTokenStore`
satisfies `TokenStore` without either importing the other) and lets every fake satisfy a contract with no
base class. The usual risk of a `Protocol`, a mismatch found only at run time, is closed by `mypy --strict`
over `src`, `tests`, `app.py` and `dev_server.py`, which fails a missing or wrong-signature method before a
test runs.

`tests/test_authz.py` exercises this offline (`tests/test_routes.py`'s fakes, reused by importing them
rather than duplicating — pytest's rootdir-relative import puts `tests/` on `sys.path`, so a bare
`from test_routes import ...` resolves) but proves a structural property rather than individual status
codes: every bearer-required route rejects a missing `Authorization` header and a garbage token (401) and
answers 503 when the identity provider cannot be reached; two established callers (user A, user B) never leak, since A's
requests only ever read and write A's row in the fake repository and B's is provably untouched; and the
set of path parameter names across every effective route equals a pinned allowlist in which `sub` is the
only name that identifies a user.

**`_BEARER_REQUIRED_HANDLERS` lists route handler functions, never paths.** Each test builds its request
from the route itself: the method it answers, and `url_path_for(handler.__name__, ...)` with every path
parameter generated. A route declares its path once, in its decorator. The list stays hand-written policy
rather than derived from the app, so a route that loses `Depends(require_bearer)` fails the three tests;
`test_every_route_behind_require_bearer_is_listed_in_bearer_required_handlers` fails naming any protected
route the list omits. When you add a protected route, add its handler in the same change.

**Walk `iter_route_contexts(app.routes)`, never `app.routes`**, which since FastAPI 0.137 holds the
included routers rather than their routes; a walk over it sees only `/health` and the docs routes and
passes with nothing checked. The path-parameter test compares for equality so a blind walk fails it.

Three properties of `tests/test_profile_routes.py` that its assertions cannot state for themselves:

- **`test_profile_body_declares_every_field_it_returns` pins the response's whole key set, and it is the
  only test that can catch an ungated new field.** Every other profile-body assertion in that module reads
  keys individually, so a field added to `PublicProfileResponse` without gating sails past all of them —
  including the private-profile test whose *name* claims it sees "only counts and follow status". Do not
  relax it into a subset check.
- **The count-suppression tests seed real, non-empty data on purpose.** `library_count`/`collections_count`
  returning `None` for a viewer of a private profile only proves suppression if the underlying library and
  collections are non-empty; against empty fixtures the same `None` proves nothing. The owner-side test
  reads the same fixtures and gets numbers, which is what makes the pair discriminate.
- **`test_created_at_is_returned_even_to_a_viewer_of_a_private_profile` is asserting a deliberate
  asymmetry, not an oversight.** `created_at` sits in the same response as the two counts the bullet
  above proves are suppressed, and it is ungated on purpose: it is first-party Curator data describing
  the account rather than its content, and the endpoint's existing 404-vs-200 on an unknown `sub`
  already discloses that the account exists. Follower counts are the older precedent for the same
  reasoning. Anyone tempted to "fix" the inconsistency by gating it should change this test knowingly.

Run:

```powershell
$env:POETRY_VIRTUALENVS_CREATE = 'false'
poetry install --extras dev
python -m pytest
```

That is CI's install (`poetry config virtualenvs.create false`, then `poetry install --extras dev`): the
locked dependencies go into the interpreter that runs `python`, and Curator itself is never installed,
because `pyproject.toml` sets `package-mode = false` and `pip install -e .` fails with "Building a package
is not possible in non-package mode". **Poetry still uses a project virtualenv it already created**, even
with creation disabled, so on a machine where `poetry env list` names one, the install lands there and
`python` stays without the dependencies.

`pyproject.toml` sets `pythonpath = ["src"]` and `testpaths = ["tests"]`, so `python -m pytest` from the
repo root picks up `src/curator` without an editable install of Curator itself. If running from a
different working directory, either `Set-Location` into the repo root first or pass the tests directory
and `-o pythonpath=<repo>/src` explicitly (or set the `PYTHONPATH` env var to `<repo>/src`).

`pytest-xdist` is required, not optional: `pyproject.toml`'s `addopts` passes `-n auto --dist loadgroup`, so
without it every `pytest` invocation fails with `error: unrecognized arguments: -n --dist`. The suite runs
in parallel by default because its cost is diffuse per-test overhead rather than any one slow test.

To run serially, reading output in source order, pass `-n 0` to the whole suite:

```powershell
python -m pytest -q -n 0
```

`-p no:xdist` does **not** work for this: disabling the plugin leaves the `addopts` flags unrecognized.

**Always run the whole suite, never one file or one `-k` expression.** A narrowed run is not a gate.

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
unset (the default for a plain local `python -m pytest` run), every test in the module is skipped rather
than run against a fake. CI does set it, against the `postgres:17` service container described below.

When set, a **session**-scoped fixture applies every `db/migrations/*.sql` file in filename order, once,
inside a transaction that is never committed. Each test then runs inside a nested `SAVEPOINT` that is
rolled back in teardown, and the session-wide transaction is rolled back when the module finishes — so a
correctly-configured database is left exactly as it started, and nothing here ever commits.

Replaying all the migrations per test instead cost roughly 11 seconds per test; building the schema once
takes the module from ~286 s to under 2 s.

The target does **not** have to be empty. `run_migrations.py` records applied filenames in
`schema_migrations` and skips them, so the fixture is idempotent against a database that already holds
part or all of the schema — the same property the deploy job relies on.

The module carries an `xdist_group` mark so `--dist loadgroup` keeps all of its tests on one worker,
rather than several workers racing to apply the same migrations.

## Two local databases, two jobs — do not mix them

`Curator/.env` defines both, and choosing correctly is the whole of it. **No superuser is needed for
either, and nothing here should ever create or drop a database.**

| variable | database | what it is for |
|---|---|---|
| `CURATOR_DATABASE_URL` | `curator` | **Manual and exploratory work.** Running a new migration by hand, driving the app, Playwright CLI sessions. It holds the local fixture data you want to keep — linked accounts, libraries, collections — so it is worth something and must not be swept. |
| `CURATOR_TEST_DATABASE_URL` | `curator_test` | **Automated testing only.** `test_schema.py` targets it, and so may another service's integration tests that read and write this schema. Nothing in it is precious, and anything that seeds it is expected to clean up after itself. |

**A suite that empties or commits to tables guards on the database name, never on configuration.**
`tests/disposable_database.py` refuses any database whose name does not end in `_test` (or `_triage`),
compared case-insensitively. Point the suite at any other name and it refuses before writing.

**The `*_test` suffix binds CI too, and its service container is named `curator_test` for that reason
alone.** Nothing about a container created and destroyed inside one job needs a careful name, but the guard
reads the name, not the lifetime, so any other name fails every schema test and, with them, the Package,
Migrate and Deploy jobs behind them. **The failure is invisible locally by construction**: a local target
already ends in `_test`, so the guard's rejecting branch is the one branch a local run never takes. When CI
and a developer supply a guarded value separately, only CI exercises half of it, and the fix is always the
supplied value, never the guard.

**Locally, use the `CURATOR_TEST_DATABASE_URL` in `.env`**, pointing at `curator_test`. There is no scratch
database to create and no superuser to connect as. Never point it at the `curator` dev database or at
production: **this module commits.** The fixture applies migrations through `db/run_migrations.py`, the
deploy job's own runner, so the target is left *migrated* rather than pristine; other suites that share
`curator_test` rely on the schema being present.

What it checks: every table the migration is expected to create exists and every dropped one is gone
(`exclusion_rules`, the two `data_quality_flags` tables, the vestigial `collection_items` columns);
representative CHECK constraints reject an out-of-enum value (`user_consoles.platform`,
`games.content_kind`, `job_runs.error_code`); the PS Plus rotation, store-hit linking and public-collection
SQL constants run against planted rows; `game_measured_sizes` upserts per (game_id, platform) rather than
accumulating history (a second `PUT` for the same pair overwrites, it doesn't add a row), and its
`recorded_by` survives its contributor's account deletion as `NULL` rather than cascading away (migration
0025); and no column named anything like `%email%` or `%npsso%` exists anywhere in the schema (the hard
privacy tenet documented in the migration's own header comment).

**One test here is a drift detector rather than a schema assertion, and it is the only thing holding a
hand-written Python constant to the database.** `curator.psn.title_platform.CONSOLE_PLATFORM_IDS` is a
`Literal`'s member list, so it cannot be built from a query at runtime;
`test_the_python_platform_vocabulary_matches_the_platforms_table` compares it against
`SELECT platform_id FROM platforms WHERE active ORDER BY sort_order`. Break it by adding a row to
`platforms` in a migration and not to the tuple: the route layer then rejects a platform the schema is
happy to store, and without this test nothing goes red.

```powershell
# CURATOR_TEST_DATABASE_URL comes from .env; load it into the environment without echoing it.
$line = Select-String -Path .env -Pattern '^CURATOR_TEST_DATABASE_URL=' -Raw
$env:CURATOR_TEST_DATABASE_URL = ($line -split '=', 2)[1].Trim()

python -m pytest -q -rs
```

With the variable set, the whole suite runs this module too, exactly as CI does. Any `skipped` count means
it did not run; `-rs` prints the reason.

**Never put the connection string on a command line.** It carries the password, and any tool that fails
to connect prints the whole conninfo. Read it into the environment as above; `psycopg` and `psql` both pick
it up from there.

The application role needs no `CREATEDB`: the fixture migrates whatever `CURATOR_TEST_DATABASE_URL` names
rather than requiring a freshly created, empty database. Running these tests never calls for
`DROP SCHEMA public CASCADE`; if it seems to, the target is wrong, not the database.

## Testing a migration before it reaches CI

**A migration is not done when the `.sql` file is written. It is done when it has run against the local
dev database.** Do not commit a migration that has not been applied locally.

**"Locally" means the `curator` database — `CURATOR_DATABASE_URL`, not the test one.** That is the only
place a migration meets a database built by every older migration *and* carrying real rows, which is what
the deploy job faces. Applying it there by hand is the true test; everything below is a backstop.

CI runs `test_schema.py` against a `postgres:17` service container (see the CI section below), so a
migration that breaks the schema contract is caught before deploy rather than by it. Because that
container starts empty, CI exercises the **fresh** path. Locally the same tests run against `curator_test`,
which is already migrated, so they exercise the **incremental** runner — but against a database holding
only what tests put there, not your real local data.

A migration rehearsed inside a rolled-back transaction can still break a schema test, for example by
adding a `NOT NULL` column that a test's `INSERT` does not supply; only running the suite with the
migration applied finds it.

Test both paths — they catch different things:

| Path | How | Catches |
|---|---|---|
| **Manual apply — the real one** | `python db/run_migrations.py` against **`curator`** (`CURATOR_DATABASE_URL`) | What the deploy job actually does: an `ALTER`/`DROP CONSTRAINT` naming an object that must already exist under an exact auto-generated name, and anything that breaks against **real rows** — a `NOT NULL` added to a populated table has nowhere to hide |
| **Automated, incremental** | `test_schema.py` against **`curator_test`** | The same runner against an already-migrated database, plus every schema assertion. Guarded to `*_test` because it commits |
| **Automated, fresh** | CI, service container | The schema still builds from empty — the rebuild path |

The incremental path is the one that matters most for a migration that modifies existing objects rather
than adding new ones: a `DROP CONSTRAINT <name>` can pass a fresh apply (where the constraint was created
seconds earlier by the same run) and still fail against a database built by an older migration.

Run the incremental path **first** — it is the realistic one, and it preserves whatever data the local
database holds. The fresh path destroys it.

Keeping the local database current is part of this: if it has drifted several migrations behind, the
incremental path stops resembling the deploy job it is supposed to imitate, and each new migration lands
on a base nobody has exercised.

## CI pipeline

`.github/workflows/main_crgolden-curator.yml` runs on push to `main`, on pull requests, and on
`workflow_dispatch`. The `test` job installs the locked runtime and dev dependencies with `poetry install
--extras dev` into the job's interpreter (`virtualenvs.create false`), after `poetry check --lock`.

The `test` job runs, in order: Ruff lint, Ruff format check, mypy, then the whole test suite with coverage
(`--cov=src/curator --cov-report=xml:coverage.xml`), then a SonarCloud analysis over `coverage.xml`. Each
lint/type-check step is its own named step so a failure is attributable at a glance.

**The job carries a `postgres:17` service container, so `test_schema.py` runs in CI rather than skipping.**
`POSTGRES_DB` gives it the empty database the fixture requires — the fixture applies every migration
itself once per session and rolls back each test's savepoint, so one container serves the whole suite. The credentials are throwaway:
the container is bound to localhost and destroyed with the job, so they live in the workflow rather than
in repository variables. They are declared once in the job's `env` block and referenced by both the
service and the test step, because the two would otherwise drift and the failure would read as an
authentication error rather than a typo.

`pg_isready` is the container's health check; without it the test step can start before Postgres accepts
connections and fail intermittently on a cold runner.

## Local SonarCloud analysis

Coverage has to exist before the scanner runs — it reads `coverage.xml`, it does not produce it. Run both
from the repo root:

The SonarScanner CLI reads `SONAR_TOKEN` from the environment; give it `sonar.host.url=https://sonarcloud.io`
in its global `conf/sonar-scanner.properties` or as one more `-D` argument.

```powershell
python -m pytest --cov=src/curator --cov-report=xml:coverage.xml -q

sonar-scanner `
  "-Dsonar.projectKey=crgolden_Curator" `
  "-Dsonar.organization=crgolden" `
  "-Dsonar.sources=src" `
  "-Dsonar.tests=tests" `
  "-Dsonar.python.coverage.reportPaths=coverage.xml" `
  "-Dsonar.python.version=3.10,3.11,3.12,3.13,3.14" `
  "-Dsonar.exclusions=**/__pycache__/**,**/*.pyc,.venv/**"
```

`sonar.python.version` mirrors `pyproject.toml`'s `requires-python = ">=3.10,<4.0"`. Without it every
analysis warns that the code is being checked against *all* Python 3 versions, which both weakens
version-specific rules and hides ones that only apply to the floor. CI runs 3.14 only, so the 3.10 floor is
analyzed but never executed.

CI passes `sonar.qualitygate.wait=true`, so the Sonar step fails when the quality gate fails, and the next
step, `Fail on open Sonar issues`, fails on any open issue on the branch or pull request whatever the gate
says. A **just-scanned project can under-report**: security findings are published on a lag behind the
main analysis, so `ce/task` returning SUCCESS does not mean the issue list is complete. When the measures
endpoint and the issue list disagree, believe the measures (that is what the gate evaluates) and re-query
rather than calling the project clean.
