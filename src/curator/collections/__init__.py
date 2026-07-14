"""Curator's collections slice: generates a ranked/filtered/(optionally capacity-)packed set of a user's
owned games on demand, from a :class:`~curator.collections.collection_spec.CollectionSpec`.

Replaces ``ps_assign_ps5.py``/``ps_assign_ps4.py``'s two hardcoded named drives (PS5 / PS4 Criterion / PS4
Blockbuster) with one reusable pipeline: a console-capacity-constrained bin-pack (``capacity_fill``) and an
unconstrained genre/score/tier filter (``filter_list``) are two strategies over the same scored candidate
pool, driven by data (a saved or inline ``CollectionSpec``) instead of two fixed scripts.
"""

from __future__ import annotations
