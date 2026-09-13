"""Role-named generators for test data, so a test proves its behaviour for a shape rather than for one
specimen (``AGENTS/CODE-STYLE.md`` rule 11). Title-id and product-id shapes mirror PSN's own formats, which
production branches on; everything else is opaque."""

from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timedelta, timezone

_LOWER_FIRST_HALF = string.ascii_lowercase[:13]
_LOWER_SECOND_HALF = string.ascii_lowercase[13:]


def lowercase_token(length: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def token_from_first_half_of_alphabet(length: int = 8) -> str:
    return "".join(random.choice(_LOWER_FIRST_HALF) for _ in range(length))


def token_from_second_half_of_alphabet(length: int = 8) -> str:
    return "".join(random.choice(_LOWER_SECOND_HALF) for _ in range(length))


def new_identity_sub() -> str:
    return str(uuid.uuid4())


def new_game_id() -> str:
    return str(uuid.uuid4())


def new_category_id() -> str:
    return str(uuid.uuid4())


def new_walk_id() -> str:
    return str(uuid.uuid4())


def new_definition_id() -> str:
    return str(uuid.uuid4())


def new_console_id() -> str:
    return str(uuid.uuid4())


def new_device_id() -> str:
    return lowercase_token(16)


def new_ps4_title_id() -> str:
    return f"CUSA{random.randint(0, 99999):05d}_00"


def new_ps5_title_id() -> str:
    return f"PPSA{random.randint(0, 99999):05d}_00"


def new_store_product_id(title_id: str | None = None) -> str:
    return f"UP{random.randint(0, 9999):04d}-{title_id or new_ps4_title_id()}-{lowercase_token(16).upper()}"


def new_concept_id() -> str:
    return str(random.randint(10000000, 99999999))


def new_game_title() -> str:
    return f"{lowercase_token(6).capitalize()} {lowercase_token(7).capitalize()}"


def new_reporting_name(prefix: str) -> str:
    return f"{prefix}_{lowercase_token(6).upper()}"


def new_online_id() -> str:
    return lowercase_token(random.randint(3, 16))


def new_account_id() -> str:
    return str(random.randint(10**15, 10**19))


def new_group_id() -> str:
    return f"{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}-{random.randint(1, 999999)}"


def new_share_slug() -> str:
    return lowercase_token(12)


def new_cover_image_url() -> str:
    return f"https://image.api.playstation.com/{lowercase_token(10)}.png"


def new_utc_instant() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=random.randint(60, 10**7))


def new_price_cents() -> int:
    return random.randint(1, 9999)


def new_positive_count(ceiling: int = 500) -> int:
    return random.randint(1, ceiling)
