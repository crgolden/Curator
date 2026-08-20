"""Curator's collections slice: generates a ranked/filtered/(optionally capacity-)packed set of a user's
owned games on demand, from a :class:`~curator.collections.collection_spec.CollectionSpec`.

A console-capacity-constrained bin-pack (``capacity_fill``) and an unconstrained genre/score/tier filter
(``filter_list``) are two strategies over the same scored candidate pool, driven by data (a saved or
inline ``CollectionSpec``).
"""

from __future__ import annotations
