"""Local development entry point: ``python dev_server.py`` from the repo root.

Not used in production -- see ``app.py`` for the Azure App Service entry point (gunicorn on Linux never
hits the issue this works around). psycopg's async mode waits on the connection socket via
``loop.add_reader()``/``add_writer()``, which Windows' default ``ProactorEventLoop`` (used since Python
3.8) does not implement -- it raises ``NotImplementedError`` the first time a real query runs.
``WindowsSelectorEventLoopPolicy`` must be installed *before* the event loop is created, which means before
``uvicorn.run()`` is called -- uvicorn creates its own loop internally, so setting this from within
``curator.app`` (e.g. at import time, or inside ``create_app()``) is always too late: with
``uvicorn --factory curator.app:create_app``, uvicorn's ``Server.run()`` calls ``asyncio.run()`` first and
only imports/calls the factory afterward. This script is the one place early enough to matter, and it
no-ops on macOS/Linux (the ``sys.platform`` guard below), so it's safe for every contributor to use
regardless of OS.

``reload=True`` spawns subprocess workers on file changes; those inherit ``PYTHONPATH`` (an environment
variable) but not this process's ``sys.path`` mutation, so both are set here.

``WindowsSelectorEventLoopPolicy`` itself is deprecated (slated for removal in Python 3.16, part of
asyncio's broader removal of the customizable event-loop-policy system) but still functions correctly as
of this writing. When it's actually removed, this needs revisiting -- check whether psycopg's async mode
has by then added native ``ProactorEventLoop`` support (the underlying blocker), or whether
``asyncio.Runner``/``uvicorn.run()`` has grown a ``loop_factory`` hook that achieves the same effect without
the deprecated policy API.
"""

from __future__ import annotations

import asyncio
import os
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, _SRC_DIR)
os.environ["PYTHONPATH"] = _SRC_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402 -- must follow the sys.path/event-loop-policy setup above

if __name__ == "__main__":
    uvicorn.run(
        "curator.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[_SRC_DIR],
    )
