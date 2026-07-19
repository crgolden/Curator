"""uvicorn loop-factory hook forcing a selector-based event loop on Windows.

Dev-only (see ``dev_server.py`` for why): psycopg's async mode waits on the connection socket via
``loop.add_reader()``/``add_writer()``, which Windows' ``ProactorEventLoop`` does not implement.

uvicorn 0.36+ builds its loop directly from ``Config.get_loop_factory()``
(``asyncio_run(self.serve(...), loop_factory=self.config.get_loop_factory())`` in
``uvicorn.server.Server.run``) rather than consulting the ambient
``asyncio.get_event_loop_policy()``, so the previously-used
``asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`` -- itself deprecated, slated
for removal in Python 3.16 -- stopped having any effect once uvicorn was upgraded past that line (see
https://github.com/Kludex/uvicorn/discussions/2749). ``uvicorn.loops.asyncio.asyncio_loop_factory``, the
built-in the ``--loop asyncio`` / default ``"auto"`` setting resolves to, returns
``asyncio.ProactorEventLoop`` on ``win32`` unconditionally. This module is passed instead, as
``uvicorn.run(..., loop="curator._windows_event_loop:selector_loop_factory")`` -- uvicorn's documented
custom-loop extension point.

Note the asymmetry with uvicorn's *built-in* factories (``uvicorn.loops.asyncio.asyncio_loop_factory``,
etc.): those take a ``use_subprocess`` keyword and *return a callable* (``Config.get_loop_factory`` calls
them once, up front, as ``loop_factory(use_subprocess=self.use_subprocess)``, then hands the callable they
return to ``asyncio_run``/``Runner``). A *custom* ``module:callable`` string skips that call entirely --
``Config.get_loop_factory`` does ``return import_from_string(self.loop)`` directly for anything not in its
``LOOP_FACTORIES`` map -- so the imported object itself must already be the zero-argument
``() -> AbstractEventLoop`` that ``Runner`` invokes. Giving this function the built-ins' two-step shape
(taking ``use_subprocess`` and returning another callable) makes ``Runner`` store the *unwrapped* class as
its loop and call unbound methods on it, e.g. ``BaseEventLoop.create_task() missing 1 required positional
argument: 'coro'`` -- confirmed by actually starting the dev server with that shape before landing this.
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a fresh selector-based event loop, matching the zero-arg shape uvicorn's ``Runner`` expects
    from a custom ``loop=`` target.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
