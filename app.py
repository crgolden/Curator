"""Azure App Service entry point.

Oryx's generated startup script runs gunicorn from wherever it extracted the deploy payload, so a
startup command must not assume an absolute application path or survive shell-quoting of a factory
call (``create_app()`` in an ``eval``'d command line broke both ways). This shim removes those
failure modes: it lives at the payload root, bootstraps ``sys.path`` relative to itself, and exposes
a ready ASGI ``app`` object, so the startup command reduces to
``gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0 app:app``.

Local development does not use this module — run ``uvicorn --factory curator.app:create_app`` from
``src/`` instead (see README).

**Bootstrap logging, mirroring the fleet's .NET apps' two-stage Serilog pattern (a minimal logger active
before the host/DI container exists, so a startup failure is never silent).** A crash here happens before
``curator.telemetry.configure_telemetry`` ever runs — including an import-time ``ModuleNotFoundError`` in
any module ``curator.app`` pulls in, which is exactly what reached production once (a dependency present
in the dev manifest but missing from the deploy one; see ``AGENTS/DEPLOYMENT.md``'s F1 quota-deadlock
section). Curator's normal Elasticsearch logging leg is *inside* ``create_app``, so it can't report a
failure in itself or anything upstream of it. This block only wraps the two lines below and only ever adds
a best-effort log line — it never suppresses the original exception, and it deliberately does not import
``curator.telemetry`` or the ``elasticsearch`` package, both of which could be implicated in exactly the
failure it exists to report; it uses only the standard library plus raw environment variables. It does
**not** cover a failure inside ``create_app``'s ASGI ``lifespan`` (e.g. ``pool.open()``) — that runs later,
after gunicorn considers startup complete, and is a separate, still-open gap (see
``curator.telemetry``/``Curator/AGENTS.md``'s note on uvicorn's ``propagate=False`` breaking the chain to
the root logger for post-startup failures).
"""

import base64
import contextlib
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

_ES_DATA_STREAM = "logs-app-curator"


def _report_bootstrap_failure(exc: BaseException) -> None:
    """Best-effort: ship one doc describing a bootstrap failure straight to Elasticsearch via raw HTTP.

    Deliberately stdlib-only (``urllib.request``, not the ``elasticsearch`` package) — that package, or
    anything else this process would need to import to log properly, could itself be the reason bootstrap
    failed. Every failure mode here (missing/partial env vars, an unreachable node, a bad response) is
    swallowed; this function must never raise, and it never changes whether the original exception
    propagates.

    :param exc: The exception that aborted bootstrap.
    """
    node = os.environ.get("ElasticsearchNode")  # noqa: SIM112
    username = os.environ.get("ElasticsearchUsername")  # noqa: SIM112
    password = os.environ.get("ElasticsearchPassword")  # noqa: SIM112
    if not (node and username and password):
        return

    doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Bootstrap failed before create_app() returned",
        "log.level": "Fatal",
        "service.name": "curator",
        "logger.name": "app.bootstrap",
        "error.stack_trace": "".join(traceback.format_exception(exc)),
    }
    body = json.dumps(doc).encode("utf-8")
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(
        f"{node.rstrip('/')}/{_ES_DATA_STREAM}/_doc?op_type=create&require_data_stream=true",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with contextlib.suppress(urllib.error.URLError, OSError, ValueError):
        urllib.request.urlopen(request, timeout=5)


try:
    from curator.app import create_app

    app = create_app()
except BaseException as exc:
    _report_bootstrap_failure(exc)
    raise
