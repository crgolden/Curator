"""Cross-concept product-id + display-name merge, extracted from ``ps_curate.py``'s ``canonicalize()``.

Sony sometimes assigns a *different* concept id to what is actually the same purchasable product (e.g. a
later PS5-native release reusing the original PS4 product's product id under a brand-new concept id), so
concept-id-based grouping alone can under-merge one real game into two rows. Product ID is Sony's own
catalog/store identifier, so two groups sharing an identical non-empty product id are merge candidates --
but product id alone is NOT sufficient: Sony's raw data has also been observed pointing two genuinely
*different* games at the same wrong product id (e.g. "BioShock: The Collection" and "Bioshock Infinite:
The Complete Edition" both carrying the same product id), which a bare product-id merge would incorrectly
conflate. Require BOTH signals to agree -- identical product id AND identical display name -- before
merging, per the entity-resolution principle of never trusting one signal alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from curator.catalog.canonicalization_service import GroupedEntry


def merge_by_product_id_and_name(groups: dict[str, list[GroupedEntry]]) -> dict[str, list[GroupedEntry]]:
    """Merge concept-id groups that share both an identical product id and an identical display name.

    A same-product-id group whose names disagree is left unmerged (its groups pass through unchanged
    under their original keys) -- that mismatch is a real data-quality signal, not something to silently
    guess at.

    :param groups: Concept-id (or name) keyed groups, as built by
        :func:`~curator.catalog.canonicalization_service.canonicalize`.
    :returns: The same groups, with any product-id+name-agreeing pairs merged into one, keyed
        ``"pid:<product_id>:<lowercased name>"``.
    """
    product_id_to_keys: dict[str, list[str]] = {}
    for key, entries in groups.items():
        product_id = entries[0].product_id
        if product_id:
            product_id_to_keys.setdefault(product_id, []).append(key)

    merged: dict[str, list[GroupedEntry]] = {}
    absorbed_keys: set[str] = set()
    for product_id, keys in product_id_to_keys.items():
        if len(keys) < 2:
            continue
        by_name: dict[str, list[str]] = {}
        for key in keys:
            by_name.setdefault(groups[key][0].name.lower(), []).append(key)
        for name_key, same_name_keys in by_name.items():
            if len(same_name_keys) > 1:
                merged_key = f"pid:{product_id}:{name_key}"
                merged[merged_key] = [entry for key in same_name_keys for entry in groups[key]]
                absorbed_keys.update(same_name_keys)

    for key, entries in groups.items():
        if key not in absorbed_keys:
            merged[key] = entries

    return merged
