"""Tests for pick_genre_subgenre(), ported from ps_genre.py's implicit behavior (no dedicated legacy test
file existed for this module -- net-new coverage per the migration plan)."""

from __future__ import annotations

from curator.scoring.genre_service import pick_genre_subgenre

_GENRE_PRIORITY = [
    "Shooter",
    "Fighting",
    "RPG",
    "MOBA",
    "Sport",
    "Sports",
    "Driving/Racing",
    "Racing",
    "Platformer",
    "Puzzle",
    "Rhythm",
    "Horror",
    "Strategy",
    "Adventure",
    "Party",
    "Board Games",
    "Card",
    "Arcade",
    "Action",
    "Simulation",
    "Massively Multiplayer",
    "Casual",
    "Family",
    "Indie",
    "Multiplayer",
]
_PRIORITIES = {name.lower(): i for i, name in enumerate(_GENRE_PRIORITY)}


def test_empty_tags_returns_empty_strings():
    assert pick_genre_subgenre([], _PRIORITIES) == ("", "")


def test_single_tag_has_no_subgenre():
    assert pick_genre_subgenre(["RPG"], _PRIORITIES) == ("RPG", "")


def test_prefers_specific_genre_over_generic_simulation():
    # RAWG tags sports/racing sims as ["Simulation", "Sports", "Racing"] -- Sports/Racing must win.
    assert pick_genre_subgenre(["Simulation", "Sports", "Racing"], _PRIORITIES) == ("Sports", "Racing")


def test_family_and_simulation_ordering():
    # PS Store tags Overcooked! All You Can Eat as ["Family", "Simulation"] -- Simulation outranks Family.
    assert pick_genre_subgenre(["Family", "Simulation"], _PRIORITIES) == ("Simulation", "Family")


def test_unranked_tags_fall_below_every_ranked_tag():
    genre, subgenre = pick_genre_subgenre(["Some Unknown Tag", "RPG"], _PRIORITIES)
    assert genre == "RPG"
    assert subgenre == "Some Unknown Tag"


def test_ties_keep_original_relative_order():
    # Neither tag is in the priority table -- stable sort keeps input order.
    assert pick_genre_subgenre(["Zeta Tag", "Alpha Tag"], _PRIORITIES) == ("Zeta Tag", "Alpha Tag")


def test_case_insensitive_matching():
    assert pick_genre_subgenre(["simulation", "sports"], _PRIORITIES) == ("sports", "simulation")


def test_empty_priorities_preserves_input_order():
    assert pick_genre_subgenre(["Beta", "Alpha"], {}) == ("Beta", "Alpha")
