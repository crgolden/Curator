"""Process-wide TLS trust store for this app's ``httpx`` clients.

Every :class:`httpx.AsyncClient` built without an explicit ``verify`` argument constructs its own
:class:`ssl.SSLContext`, which reads and parses the whole CA bundle from disk. That parse is the dominant
cost of building one client, so a codebase that constructs several clients pays it several times.
:func:`shared_ssl_context` builds one context per process and hands the same object to every client.

The context comes from :func:`httpx.create_ssl_context` itself rather than a hand-rolled
:func:`ssl.create_default_context` call, so the trust store, the ``SSL_CERT_FILE``/``SSL_CERT_DIR``
environment handling and the verification mode are byte-for-byte what an un-configured client would have
used. Sharing changes when the bundle is read, never what is trusted.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx


@lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    """Return this process's single :class:`ssl.SSLContext`, building it on first call.

    :returns: The shared context, suitable for ``httpx.AsyncClient(verify=...)``.
    """
    return httpx.create_ssl_context()
