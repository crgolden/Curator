"""The ``games.normalized_title`` key, shared by every catalog writer in this runtime."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_TRADEMARK_SYMBOLS = re.compile(r"[™®©]")
_TRADEMARK_LETTERS = re.compile(r"TM\b")
_EMPTY_PARENTHESES = re.compile(r"\(\s*\)")
_WHITESPACE = re.compile(r"\s+")
_NON_SPACING_MARK = "Mn"


def normalize_name(name: str | None) -> str | None:
    """Return the display form of a vendor title, or ``None`` when nothing is left of it.

    :param name: The title as the vendor published it.
    """
    if name is None or not name.strip():
        return None
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != _NON_SPACING_MARK)
    stripped = _TRADEMARK_SYMBOLS.sub("", without_marks)
    stripped = _TRADEMARK_LETTERS.sub("", stripped)
    stripped = _EMPTY_PARENTHESES.sub("", stripped)
    collapsed = _WHITESPACE.sub(" ", stripped).strip()
    return collapsed or None


def normalized_title(name: str) -> str:
    """Return the ``games.normalized_title`` key for a display form :func:`normalize_name` produced.

    :param name: A non-empty display form.
    """
    return name.strip().lower()


def edition_family(name: str, edition_keywords: Iterable[str]) -> str:
    """Return the name with every ``edition_ranks`` keyword removed, the key ``Functions`` groups a
    concept's products by (``CanonicalizationService.EditionFamily``).

    :param name: A display form or a ``games.normalized_title`` key.
    :param edition_keywords: The ``edition_ranks`` keywords, already lower-case.
    """
    family = name.lower()
    for keyword in edition_keywords:
        family = family.replace(keyword, "")
    return _WHITESPACE.sub(" ", family).strip()
