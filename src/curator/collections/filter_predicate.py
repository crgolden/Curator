"""An OR-capable predicate tree for ``filter_list`` collections (WP8), replacing
``apply_filter_list``'s pure-AND flat fields for callers that need disjunction -- e.g. the legacy
"Criterion" genre-set classifier's rule, ``genre in SET or (genre == "action" and tier == "indie")``,
which the flat ``genre_filter``/``aaa_tier_filter`` pair cannot express at all.

Deliberately excludes trophy completion. ``CollectionSpec.min_percent_completed`` is only applied when
``completion_available`` is ``True`` (see ``filter_list_strategy``'s docstring) -- in a tree, "the leaf is
omitted" and "the leaf evaluates to False" diverge in opposite directions depending on the enclosing node
(dropping a leaf widens an ``And``, narrows an ``Or``), so there is no single evaluator rule that preserves
that invariant. It stays a flat, top-level, always-AND field instead.

No user-facing expression language and no operator-precedence parser -- just the tagged nodes a caller
builds directly, matching how the one concrete requirement (the Criterion shape above) actually needs to
be expressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curator.collections.game_candidate import GameCandidate


@dataclass(frozen=True, slots=True)
class GenreIn:
    """True when the candidate's genre (case-insensitive) is one of ``values``."""

    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TierIn:
    """True when the candidate's ``aaa_tier`` is one of ``values``."""

    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreAtLeast:
    """True when the candidate has a composite score and it meets ``threshold``."""

    threshold: float


@dataclass(frozen=True, slots=True)
class And:
    """True when every child node is true."""

    nodes: tuple[FilterPredicate, ...]


@dataclass(frozen=True, slots=True)
class Or:
    """True when at least one child node is true."""

    nodes: tuple[FilterPredicate, ...]


FilterPredicate = GenreIn | TierIn | ScoreAtLeast | And | Or


def evaluate(predicate: FilterPredicate, candidate: GameCandidate) -> bool:
    """Evaluate ``predicate`` against one candidate."""
    if isinstance(predicate, GenreIn):
        allowed = {value.lower() for value in predicate.values}
        return candidate.genre.lower() in allowed
    if isinstance(predicate, TierIn):
        return candidate.aaa_tier in predicate.values
    if isinstance(predicate, ScoreAtLeast):
        return candidate.composite_score is not None and candidate.composite_score >= predicate.threshold
    if isinstance(predicate, And):
        return all(evaluate(node, candidate) for node in predicate.nodes)
    return any(evaluate(node, candidate) for node in predicate.nodes)


def predicate_to_dict(predicate: FilterPredicate) -> dict[str, Any]:
    """Serialize ``predicate`` to the JSON shape stored in ``collection_definitions.filter_predicate`` and
    returned over the API. The inverse of :func:`parse_predicate`."""
    if isinstance(predicate, GenreIn):
        return {"op": "genre_in", "values": list(predicate.values)}
    if isinstance(predicate, TierIn):
        return {"op": "tier_in", "values": list(predicate.values)}
    if isinstance(predicate, ScoreAtLeast):
        return {"op": "score_at_least", "threshold": predicate.threshold}
    if isinstance(predicate, And):
        return {"op": "and", "nodes": [predicate_to_dict(node) for node in predicate.nodes]}
    return {"op": "or", "nodes": [predicate_to_dict(node) for node in predicate.nodes]}


def parse_predicate(raw: dict[str, Any]) -> FilterPredicate:
    """Parse the JSON shape (from a request body or a stored row) back into a :data:`FilterPredicate` tree.

    :raises ValueError: If ``raw`` is not a well-formed predicate -- an unrecognized ``op``, a leaf missing
        or misshaping its value, or a combinator with no child nodes. Route callers turn this into a 400.
    """
    if not isinstance(raw, dict) or "op" not in raw:
        raise ValueError("A filter predicate node must be an object with an 'op' field.")
    op = raw["op"]
    if op == "genre_in":
        return GenreIn(values=_string_list(raw, "values"))
    if op == "tier_in":
        return TierIn(values=_string_list(raw, "values"))
    if op == "score_at_least":
        threshold = raw.get("threshold")
        if not isinstance(threshold, int | float) or isinstance(threshold, bool):
            raise ValueError("score_at_least requires a numeric 'threshold'.")
        return ScoreAtLeast(threshold=float(threshold))
    if op in ("and", "or"):
        nodes = raw.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(f"'{op}' requires a non-empty 'nodes' list.")
        children = tuple(parse_predicate(node) for node in nodes)
        return And(nodes=children) if op == "and" else Or(nodes=children)
    raise ValueError(f"Unknown predicate op {op!r}.")


def _string_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    values = raw.get(key)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{raw.get('op')} requires a non-empty list of strings for '{key}'.")
    return tuple(values)
