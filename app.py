"""Azure App Service entry point.

Oryx's generated startup script runs gunicorn from wherever it extracted the deploy payload, so a
startup command must not assume an absolute application path or survive shell-quoting of a factory
call (``create_app()`` in an ``eval``'d command line broke both ways). This shim removes those
failure modes: it lives at the payload root, bootstraps ``sys.path`` relative to itself, and exposes
a ready ASGI ``app`` object, so the startup command reduces to
``gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0 app:app``.

Local development does not use this module — run ``uvicorn --factory curator.app:create_app`` from
``src/`` instead (see README).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from curator.app import create_app

app = create_app()
