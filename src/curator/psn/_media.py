"""Cover-art selection over a PSN ``media`` array.

Both gateways publish the same ``{role, type, url}`` entries drawn from the same store role vocabulary:
the anonymous storefront's ``categoryGridRetrieve`` product node (``curator.psn.store_client``) and the
authenticated mobile app's universal search result (``curator.psn.social_client``). The preference order
therefore lives here once. Two independently-written copies of the same idea silently diverge -- see
``AGENTS/CODE-STYLE.md``'s "Model the payload first" section on the three PSN clients that each grew their
own JSON helpers.
"""

from __future__ import annotations

from typing import Any, Final

GAMEHUB_COVER_ART_ROLE: Final = "GAMEHUB_COVER_ART"
EDITION_KEY_ART_ROLE: Final = "EDITION_KEY_ART"
PORTRAIT_BANNER_ROLE: Final = "PORTRAIT_BANNER"
BACKGROUND_ROLE: Final = "BACKGROUND"

COVER_ART_ROLE_PREFERENCE: Final[tuple[str, ...]] = (
    GAMEHUB_COVER_ART_ROLE,
    EDITION_KEY_ART_ROLE,
    PORTRAIT_BANNER_ROLE,
    BACKGROUND_ROLE,
)

IMAGE_MEDIA_TYPE: Final = "IMAGE"

ROLE_KEY: Final = "role"
TYPE_KEY: Final = "type"
URL_KEY: Final = "url"


def cover_image_url(media: Any) -> str | None:
    """The best available cover art from a product/concept ``media`` array, or ``None``.

    :param media: The raw ``media`` array as the gateway sent it; anything that is not a list of mappings
        yields ``None`` rather than raising, since neither gateway guarantees the key is present.
    :returns: The URL of the highest-preference :data:`COVER_ART_ROLE_PREFERENCE` role present, or ``None``
        when the array carries no image in any of those roles.
    """
    if not isinstance(media, list):
        return None
    by_role: dict[str, str] = {
        item[ROLE_KEY]: item[URL_KEY]
        for item in media
        if isinstance(item, dict)
        and item.get(TYPE_KEY) == IMAGE_MEDIA_TYPE
        and isinstance(item.get(ROLE_KEY), str)
        and isinstance(item.get(URL_KEY), str)
        and item[URL_KEY]
    }
    for role in COVER_ART_ROLE_PREFERENCE:
        if role in by_role:
            return by_role[role]
    return None
