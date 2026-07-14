"""Curator's folded-in PSN client: the full ``psnpy`` capability surface, ported onto async I/O.

Replaces the external ``psnpy`` package entirely (see the migration plan's "Full psnpy capability audit"
section). Every submodule here corresponds to one cohesive PSN concern (auth engine, account, library,
catalog, trophies, presence, social, chat, the mutation-safety wall) rather than one monolithic client
class, and every I/O-touching method is ``async def`` built on :class:`httpx.AsyncClient`.
"""

from __future__ import annotations
