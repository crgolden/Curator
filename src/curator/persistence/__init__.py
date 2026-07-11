"""Curator's persistence layer: PostgreSQL configuration, encryption, and data access.

Re-exports the public surface so callers can write ``from curator.persistence import Repository`` etc.
without reaching into individual modules.
"""

from __future__ import annotations

from curator.persistence.config import ConfigError
from curator.persistence.connection import resolve_database_url
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.repository import LinkRecord, Repository

__all__ = [
    "ConfigError",
    "resolve_database_url",
    "TokenCrypto",
    "DbTokenStore",
    "LinkRecord",
    "Repository",
]
