from __future__ import annotations

from typing import Final, Literal

SortDirection = Literal["asc", "desc"]

AAA_TIER_PARAM: Final = "aaaTier"
CATEGORY_ID_PARAM: Final = "categoryId"
FRANCHISE_PARAM: Final = "franchise"
GENRE_PARAM: Final = "genre"
HIDDEN_PARAM: Final = "hidden"
LIMIT_PARAM: Final = "limit"
OFFSET_PARAM: Final = "offset"
SORT_PARAM: Final = "sort"
SORT_DIR_PARAM: Final = "sortDir"

SORT_ASC: Final[SortDirection] = "asc"
SORT_DESC: Final[SortDirection] = "desc"
