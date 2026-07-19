"""Local development entry point: ``python dev_server.py`` from the repo root.

Not used in production -- see ``app.py`` for the Azure App Service entry point (gunicorn on Linux never
hits the issue this works around). psycopg's async mode waits on the connection socket via
``loop.add_reader()``/``add_writer()``, which Windows' default ``ProactorEventLoop`` does not implement --
it raises ``NotImplementedError`` the first time a real query runs. ``loop=`` below points uvicorn at
``curator._windows_event_loop.selector_loop_factory``, which forces ``asyncio.SelectorEventLoop`` on
Windows (see that module's docstring for why the deprecated ``asyncio.set_event_loop_policy`` approach this
replaced stopped working once uvicorn started building its loop from ``Config.get_loop_factory()``
directly). ``reload_dirs`` is scoped to ``_SRC_DIR`` so file-watching doesn't pick up unrelated repo noise
(``.venv``, ``.git``, ``.mypy_cache``, ...).

``reload=True`` spawns subprocess workers on file changes; those inherit ``PYTHONPATH`` (an environment
variable) but not this process's ``sys.path`` mutation, so both are set here.
"""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, _SRC_DIR)
os.environ["PYTHONPATH"] = _SRC_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

import uvicorn  # noqa: E402 -- must follow the sys.path setup above

if __name__ == "__main__":
    uvicorn.run(
        "curator.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[_SRC_DIR],
        loop="curator._windows_event_loop:selector_loop_factory" if sys.platform == "win32" else "auto",
    )
