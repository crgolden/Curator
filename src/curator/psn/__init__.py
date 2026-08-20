"""Curator's PSN client.

Every submodule here corresponds to one cohesive PSN concern (auth engine, account, library, catalog,
trophies, presence, social, chat, the mutation-safety wall) rather than one monolithic client class, and
every I/O-touching method is ``async def`` built on :class:`httpx.AsyncClient`.
"""

from __future__ import annotations
