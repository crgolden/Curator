"""Field names of a PlayStation Store product or concept node.

Both gateways publish the same node shape: the anonymous storefront's ``categoryGridRetrieve`` product
(:mod:`curator.psn.store_client`) and the authenticated mobile gateway's universal-search result
(:mod:`curator.psn.social_client`). The names live here once so the two readers cannot drift apart.
"""

from __future__ import annotations

from typing import Final

ID_KEY: Final = "id"
TYPENAME_KEY: Final = "__typename"
NAME_KEY: Final = "name"
INVARIANT_NAME_KEY: Final = "invariantName"
PLATFORMS_KEY: Final = "platforms"
MEDIA_KEY: Final = "media"
CLASSIFICATION_KEY: Final = "localizedStoreDisplayClassification"
DEFAULT_PRODUCT_KEY: Final = "defaultProduct"
NP_TITLE_ID_KEY: Final = "npTitleId"

PRICE_KEY: Final = "price"
BASE_PRICE_KEY: Final = "basePrice"
DISCOUNTED_PRICE_KEY: Final = "discountedPrice"
DISCOUNT_TEXT_KEY: Final = "discountText"
IS_FREE_KEY: Final = "isFree"
IS_TIED_TO_SUBSCRIPTION_KEY: Final = "isTiedToSubscription"
