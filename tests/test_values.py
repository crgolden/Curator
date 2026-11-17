"""Role-named generators for test data, so a test proves its behaviour for a shape rather than for one
specimen (``AGENTS/CODE-STYLE.md`` rule 11). Title-id and product-id shapes mirror PSN's own formats, which
production branches on; everything else is opaque."""

from __future__ import annotations

import random
import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import get_args

from curator.catalog.content_kind import CONTENT_KINDS, GAME_KIND, ContentKind
from curator.catalog.ps_plus_repository import PsPlusTier

_LOWER_FIRST_HALF = string.ascii_lowercase[:13]
_LOWER_SECOND_HALF = string.ascii_lowercase[13:]


def lowercase_token(length: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def new_blank() -> str:
    return "".join(random.choice(" \t") for _ in range(random.randint(1, 3)))


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
    return datetime.now(UTC) - timedelta(seconds=random.randint(60, 10**7))


def new_price_cents() -> int:
    return random.randint(1, 9999)


def new_positive_count(ceiling: int = 500) -> int:
    return random.randint(1, ceiling)


def new_percent_completed() -> int:
    return random.randint(1, 99)


def new_email_address() -> str:
    return f"{lowercase_token()}@{lowercase_token()}.{lowercase_token(3)}"


def new_opaque_token() -> str:
    return uuid.uuid4().hex


def new_sha256_hash() -> str:
    return f"{uuid.uuid4().hex}{uuid.uuid4().hex}"


def new_np_communication_id() -> str:
    return f"NPWR{random.randint(0, 99999):05d}_00"


def new_trophy_group_id() -> str:
    return f"{random.randint(1, 999):03d}"


def new_small_count() -> int:
    return random.randint(1, 9)


def new_review_score() -> float:
    return round(random.uniform(1, 100), 1)


def new_psn_rating() -> float:
    return round(random.uniform(1, 5), 2)


def new_genre_name() -> str:
    return lowercase_token().capitalize()


def new_facet_key() -> str:
    return lowercase_token().upper()


_PS_PLUS_TIERS: tuple[PsPlusTier, ...] = get_args(PsPlusTier)


def new_ps_plus_tier() -> PsPlusTier:
    return random.choice(_PS_PLUS_TIERS)


def new_non_game_kind() -> ContentKind:
    return random.choice([kind for kind in CONTENT_KINDS if kind != GAME_KIND])


def new_run_id() -> str:
    return str(uuid.uuid4())


def new_result_summary() -> dict[str, object]:
    return {lowercase_token(): {lowercase_token(): lowercase_token()}}


def new_flag() -> bool:
    return random.choice((True, False))


def new_size_gb() -> float:
    return round(random.uniform(1, 2000), 1)


def new_storage_device_id() -> str:
    return str(uuid.uuid4())


def new_short_interval() -> timedelta:
    return timedelta(seconds=random.randint(60, 600))


def new_rawg_game_id() -> int:
    return random.randint(1, 999_999)


def new_ps3_title_id() -> str:
    return f"BLUS{random.randint(0, 99999):05d}_00"


def new_ps_plus_sku_title_id() -> str:
    return f"NPIA{random.randint(0, 99999):05d}_01"


def new_install_size_gb() -> float:
    return round(random.uniform(1, 100), 1)


def new_capacity_gb() -> float:
    return round(random.uniform(1000, 2000), 1)


def new_size_gb_below(ceiling_gb: float) -> float:
    return round(random.uniform(ceiling_gb / 10, ceiling_gb / 2), 2)


def new_percent_above(floor: int) -> int:
    return random.randint(floor + 1, 100)


def new_percent_below(ceiling: int) -> int:
    return random.randint(0, ceiling - 1)


def new_review_score_below(ceiling: float) -> float:
    return round(ceiling * random.uniform(0.1, 0.9), 1)


def new_url_on_host(host: str) -> str:
    return f"https://{host}/{lowercase_token()}"


def new_direct_message_group_id() -> str:
    return f"~{uuid.uuid4().hex[:16].upper()}.{uuid.uuid4().hex[:16].upper()}"


def new_profile_handle() -> str:
    return lowercase_token(random.randint(3, 16))


def new_thousands_price_cents() -> int:
    return random.randint(100_000, 99_999_999)


def new_database_url() -> str:
    return f"postgresql://{lowercase_token()}/{lowercase_token()}"


def new_service_bus_namespace() -> str:
    return f"{lowercase_token()}.servicebus.windows.net"


def new_service_bus_connection_string() -> str:
    return (
        f"Endpoint=sb://{new_service_bus_namespace()}/;SharedAccessKeyName={lowercase_token()};"
        f"SharedAccessKey={new_opaque_token()}"
    )


def new_access_token_lifetime_seconds() -> int:
    return random.randint(600, 7200)


def new_refresh_token_lifetime_seconds() -> int:
    return random.randint(86_400, 5_184_000)


def new_unmapped_title_id() -> str:
    return f"{random.randint(1000, 9999)}{random.randint(0, 99999):05d}_00"


def new_title_id_fragment() -> str:
    return lowercase_token(random.randint(1, 3)).upper()


def new_endpoint_url() -> str:
    return f"https://{lowercase_token()}.{lowercase_token(4)}"


def new_env_var_name() -> str:
    return f"CURATOR_TEST_{lowercase_token().upper()}"


def new_port() -> int:
    return random.randint(1024, 65535)


def new_npsso() -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(64))
