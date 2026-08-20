"""Re-encrypts secrets at rest from Fernet to AES-256-GCM, using ``CURATOR_TOKEN_KEY`` for both.

Usage::

    python db/reencrypt_tokens.py <database_url> [--dry-run]

Idempotent: run it before every app deploy. ``--dry-run`` reports and writes nothing. Exits non-zero if
any blob decrypts under neither scheme. See ``AGENTS/Curator.md`` for why this exists and when to run it.
"""

from __future__ import annotations

import enum
import sys
from dataclasses import dataclass, field
from typing import NamedTuple

import psycopg
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken as FernetInvalidToken

from curator.persistence.crypto import InvalidToken, TokenCrypto


class EncryptedColumns(NamedTuple):
    """A table holding secrets at rest, and the columns of it that do."""

    table: str
    primary_key: str
    columns: tuple[str, ...]


TARGETS: tuple[EncryptedColumns, ...] = (
    EncryptedColumns("psn_links", "identity_sub", ("token_response_enc",)),
    EncryptedColumns("user_enrichment_keys", "identity_sub", ("rawg_api_key_enc", "opencritic_api_key_enc")),
)


class Disposition(enum.Enum):
    """What a stored blob needs, as decided by :func:`classify`."""

    REKEY = "rekey"
    ALREADY_AES = "already_aes"
    EMPTY = "empty"
    UNREADABLE = "unreadable"


@dataclass
class Counts:
    """How each blob was classified, aggregated per column."""

    rekeyed: int = 0
    already_aes: int = 0
    empty: int = 0
    unreadable: list[str] = field(default_factory=list)


def reencrypt(database_url: str, *, dry_run: bool = False) -> dict[str, Counts]:
    """Re-encrypt every Fernet blob in :data:`TARGETS` under AES-256-GCM.

    :param database_url: The ``postgresql://`` connection string to re-key.
    :param dry_run: When true, classify every blob and report, but write nothing.
    :returns: Counts keyed by ``table.column``.
    """
    crypto = TokenCrypto.from_config()
    fernet = Fernet(_fernet_key())
    results: dict[str, Counts] = {}

    with psycopg.connect(database_url) as conn:
        for table, primary_key, columns in TARGETS:
            for column in columns:
                counts = Counts()
                results[f"{table}.{column}"] = counts
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {primary_key}, {column} FROM {table} WHERE {column} IS NOT NULL")
                    rows = cur.fetchall()

                for key, blob in rows:
                    disposition, plaintext = classify(bytes(blob), crypto, fernet)
                    if disposition is Disposition.EMPTY:
                        counts.empty += 1
                        continue
                    if disposition is Disposition.ALREADY_AES:
                        counts.already_aes += 1
                        continue
                    if disposition is Disposition.UNREADABLE or plaintext is None:
                        counts.unreadable.append(str(key))
                        continue

                    counts.rekeyed += 1
                    if not dry_run:
                        with conn.cursor() as cur:
                            cur.execute(
                                f"UPDATE {table} SET {column} = %s WHERE {primary_key} = %s",
                                (crypto.encrypt(plaintext), key),
                            )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    return results


def classify(blob: bytes, crypto: TokenCrypto, fernet: Fernet) -> tuple[Disposition, bytes | None]:
    """Decide what a stored blob needs, and recover its plaintext when it needs re-keying.

    :param blob: The stored bytes.
    :param crypto: The AES-256-GCM implementation the application now uses.
    :param fernet: Fernet under the same key, to read what the old implementation wrote.
    :returns: The disposition, and the plaintext when the disposition is ``REKEY``.
    """
    if not blob:
        return Disposition.EMPTY, None

    try:
        crypto.decrypt(blob)
    except InvalidToken:
        pass
    else:
        return Disposition.ALREADY_AES, None

    try:
        return Disposition.REKEY, fernet.decrypt(blob)
    except (FernetInvalidToken, ValueError):
        return Disposition.UNREADABLE, None


def _fernet_key() -> bytes:
    """:returns: ``CURATOR_TOKEN_KEY``, the key both schemes use.

    :raises SystemExit: If it is not set.
    """
    from curator.persistence.config import resolve_setting

    value = resolve_setting(None, env_names=("CURATOR_TOKEN_KEY",))
    if not value:
        raise SystemExit("CURATOR_TOKEN_KEY is not set; cannot read the existing Fernet blobs.")
    return value.encode("ascii")


def main() -> None:
    arguments = [argument for argument in sys.argv[1:] if argument != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    if len(arguments) != 1:
        print("Usage: python db/reencrypt_tokens.py <database_url> [--dry-run]", file=sys.stderr)
        raise SystemExit(2)

    results = reencrypt(arguments[0], dry_run=dry_run)

    prefix = "would re-key" if dry_run else "re-keyed"
    unreadable_total = 0
    for name, counts in results.items():
        unreadable_total += len(counts.unreadable)
        print(
            f"{name}: {prefix} {counts.rekeyed}, already AES-GCM {counts.already_aes}, "
            f"empty {counts.empty}, unreadable {len(counts.unreadable)}"
        )
        for key in counts.unreadable:
            print(f"  unreadable under both Fernet and AES-GCM: {name} {key}", file=sys.stderr)

    if unreadable_total:
        raise SystemExit(
            f"{unreadable_total} blob(s) decrypt under neither scheme. They were written under a "
            "different CURATOR_TOKEN_KEY; re-keying cannot recover them and the affected users must "
            "re-link. Resolve before deploying."
        )


if __name__ == "__main__":
    main()
