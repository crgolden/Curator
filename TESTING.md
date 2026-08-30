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

**`_BEARER_REQUIRED_ROUTES` is a hand-maintained list, and a route missing from it is silently unswept.**
The four `/me/profile-link*` routes shipped without entries and so had nothing proving they reject a
missing or garbage token. When you add a protected route, add it there in the same change.

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
python -m pip install pytest pytest-asyncio pytest-xdist httpx fastapi uvicorn joserfc cryptography "psycopg[binary]" psycopg-pool redis azure-servicebus pycountry
python -m pytest tests -q
```

`pytest-xdist` is not optional in that list: `pyproject.toml`'s `addopts` passes `-n auto --dist loadgroup`,
so without it every `pytest` invocation fails with `error: unrecognized arguments: -n --dist`. The suite
runs in parallel by default because its cost is diffuse per-test overhead rather than any one slow test.

To run serially — debugging a single test, or reading output in source order — pass `-n 0`:

```powershell
python -m pytest tests/test_routes.py -q -n 0
```

`-p no:xdist` does **not** work for this: disabling the plugin leaves the `addopts` flags unrecognized.

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
| `CURATOR_TEST_DATABASE_URL` | `curator_test` | **Automated testing only.** `test_schema.py` and `Functions.Tests.Integration` both target it. Nothing in it is precious, and anything that seeds it is expected to clean up after itself. |

**A suite that empties tables guards on the database name, never on configuration.** Identity's E2E fixture
is the reference: `PlaywrightFixture.CleanupDatabaseAsync` deletes every table's rows after the run, but
returns early unless `db.Database.GetDbConnection().Database.EndsWith("Test", StringComparison.Ordinal)`.
Point it at a non-`*Test` database and the cleanup simply does not run. Copy that shape rather than
trusting that the right connection string was supplied.

**The `*_test` suffix binds CI too, and its service container is named `curator_test` for that reason
alone.** Nothing about a container that is created and destroyed inside one job needs a careful name — but
the guard reads the name, not the lifetime, so an arbitrary one fails. It was `curator_ci` and every schema
test errored, taking Package, Migrate and Deploy down as skipped. **The failure is invisible locally by
construction**: the local target already ends in `_test`, so the guard's rejecting branch is the one branch
a local run can never take. Whenever a guard's predicate reads a value CI supplies and the developer
supplies separately, only CI exercises half of it.

**When that happens the guard is the thing to keep, and the supplied value is the thing to change.**
Relaxing the suffix would have deleted the only barrier between a suite that commits and the exploratory
database, to fix a name that was free to change.

**Most of what went wrong on 2026-08-28 was this distinction not being written down.** A scratch database
was invented, a superuser was used to create it, `curator_test` was swept while another suite depended on
it, and a credential ended up in a transcript — none of which was necessary, because both URLs were
already in `.env` and neither needs elevated rights.

**Locally, use the `CURATOR_TEST_DATABASE_URL` already in `Curator/.env`** — it points at `curator_test`.
There is no scratch database to create and no superuser to connect as. Never point it at the `curator`
dev database or at production: **this module commits.** The fixture applies migrations through
`db/run_migrations.py`, the deploy job's own runner, so the target is left *migrated* rather than
pristine. That is deliberate — `Functions.Tests.Integration` shares `curator_test` and requires the
schema present — and it is why the older "create a throwaway database, then drop it" recipe is gone.

What it checks: every table the migration is expected to create exists; representative CHECK constraints
reject an out-of-enum value (`game_assignments.collection_status`, `user_consoles.platform`,
`exclusion_rules.rule_type`); `game_measured_sizes` upserts per (game_id, platform) rather than
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
# CURATOR_TEST_DATABASE_URL comes from Curator/.env; load it into the environment without echoing it.
$line = Select-String -Path .env -Pattern '^CURATOR_TEST_DATABASE_URL=' -Raw
$env:CURATOR_TEST_DATABASE_URL = ($line -split '=', 2)[1].Trim()

python -m pytest tests/test_schema.py -q -n0
```

**Never put the connection string on a command line.** It carries the password, and any tool that fails
to connect prints the whole conninfo — which is how a credential reached a session transcript on
2026-08-28. Read it into the environment as above; `psycopg` and `psql` both pick it up from there.

The application role deliberately lacks `CREATEDB`, and it no longer needs it: the fixture migrates
whatever `CURATOR_TEST_DATABASE_URL` names rather than requiring a freshly created, empty database.
Resetting a schema with `DROP SCHEMA public CASCADE` is no longer part of running these tests — if you
find yourself doing it, the target is wrong, not the database.

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

That gap is not theoretical. Migration `0039` was rehearsed against production inside a rolled-back
transaction, passed, and still broke `test_deleting_a_user_cascades_every_per_user_table` by adding a
`NOT NULL` column the test's `INSERT` did not supply. Running it locally is what surfaced that, and it
also surfaced five *pre-existing* failures in this suite that nobody had seen — the cost of a suite that
only ran when someone opted in.

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

## CI

`.github/workflows/main.yml` runs on push to `main`, on pull requests, and on `workflow_dispatch`. It
installs Curator's runtime and dev dependencies directly (rather than `pip install -e .`) so the job
doesn't depend on any cross-repo checkout.

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
version-specific rules and hides ones that only apply to the floor. Note CI installs 3.14 only, so the
declared 3.10 floor is analysed but never actually executed — narrow `requires-python` or add a matrix if
that floor is meant to be a real promise.

`sonar.host.url` and `sonar.scanner.skipJreProvisioning=true` are already set in the scanner's global
`conf/sonar-scanner.properties`, so neither belongs on the command line: the CLI ships its own JRE 21 and
uses it even though `java` is not on PATH.

Two things mislead when you go to read the result. A **green CI run is not a passed quality gate** — the
workflow does not set `sonar.qualitygate.wait`, so the Sonar step submits the analysis and reports success
whatever the gate later says. And a **just-scanned project can under-report**: security findings are
published on a lag behind the main analysis, so `ce/task` returning SUCCESS does not mean the issue list is
complete. When the measures endpoint and the issue list disagree, believe the measures — that is what the
gate evaluates — and re-query rather than calling the project clean.
