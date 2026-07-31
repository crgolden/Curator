"""Ingestion: capture the caller's own PSN entitlements into a new entitlement pull.

Calls :meth:`curator.psn.library_client.LibraryClient.entitlements` -- self-only, per PSN's own API
constraints -- then hands the raw entitlements to
:meth:`curator.catalog.repository.CatalogRepository.record_pull`, which owns
``entitlement_pulls``/``entitlement_snapshots`` (see that repository's docstring for why: they're
catalog-aggregate tables, not a separate ingestion-specific store).
"""

from __future__ import annotations

from curator.catalog.canonicalization_service import EntitlementSnapshot
from curator.catalog.repository import CatalogRepository
from curator.psn.library_client import LibraryClient
from curator.psn.models import Entitlement


def _to_snapshot(entitlement: Entitlement) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        entitlement_id=entitlement.entitlement_id or "",
        concept_id=entitlement.concept_id,
        product_id=entitlement.product_id,
        title_id=entitlement.title_id,
        game_meta_name=entitlement.game_meta_name,
        concept_meta_name=entitlement.concept_meta_name,
        title_meta_name=entitlement.title_meta_name,
        package_type=entitlement.package_type,
        active=entitlement.active,
        sku_id=entitlement.sku_id,
        active_date=entitlement.active_date,
        title_image_url=entitlement.title_image_url,
        game_icon_url=entitlement.game_icon_url,
        concept_icon_url=entitlement.concept_icon_url,
        is_game=entitlement.is_game,
        platform_ids=entitlement.platform_ids,
    )


class IngestionService:
    """Pulls and persists the caller's own PSN entitlements.

    :param library_client: The PSN library client (``entitlements()`` is self-only).
    :param repository: The catalog repository (owns ``entitlement_pulls``/``entitlement_snapshots``).
    """

    def __init__(self, library_client: LibraryClient, repository: CatalogRepository) -> None:
        self._library_client = library_client
        self._repository = repository

    async def ingest(self, identity_sub: str, *, limit: int | None = None) -> tuple[str, list[EntitlementSnapshot]]:
        """Fetch and persist the caller's current entitlements as a new pull.

        :param identity_sub: The Curator user id (Identity's ``sub``) this pull belongs to.
        :param limit: Maximum number of entitlements to fetch, or ``None`` (the default) to fetch every
            entitlement PSN reports -- see :meth:`~curator.psn.library_client.LibraryClient.entitlements`.
        :returns: ``(pull_id, snapshots)`` -- the new pull's id and the snapshots just recorded, ready for
            :class:`~curator.library.library_build_orchestrator.LibraryBuildOrchestrator` to canonicalize.
        """
        entitlements = await self._library_client.entitlements_with_raw(limit=limit)
        snapshots = [_to_snapshot(entitlement) for entitlement, _raw in entitlements]
        # Hand the verbatim PSN entries through so entitlement_snapshots.raw is actually populated. That
        # column exists precisely so a mapping bug here never permanently loses a field PSN sent; before
        # this argument was passed it stored '{}' for every row, discarding roughly two-thirds of the
        # payload (including every artwork URL) with no way to recover it short of a fresh pull.
        raw_by_entitlement_id = {entitlement.entitlement_id or "": raw for entitlement, raw in entitlements}
        pull_id = await self._repository.record_pull(identity_sub, "curator-live", snapshots, raw=raw_by_entitlement_id)
        return pull_id, snapshots
