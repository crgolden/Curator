"""Typed result models returned by ``curator.psn``'s clients, ported verbatim from ``psnpy.models``.

These are Curator's own stable shapes, decoupled from PSN's raw JSON response shapes, so the interface
stays constant as the backend evolves. Pure data -- no I/O, no async -- so nothing here changed as part of
folding ``psnpy`` into Curator beyond the module's new home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TrophyCounts:
    """A bronze/silver/gold/platinum trophy tally."""

    bronze: int = 0
    silver: int = 0
    gold: int = 0
    platinum: int = 0

    @property
    def total(self) -> int:
        """The total number of trophies across all tiers."""
        return self.bronze + self.silver + self.gold + self.platinum


@dataclass(frozen=True, slots=True)
class TrophySummary:
    """A user's overall trophy standing."""

    level: int
    progress: int
    tier: int
    earned: TrophyCounts
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrophyTitle:
    """A single game's trophy status for a user."""

    name: str | None
    np_communication_id: str | None
    platforms: tuple[str, ...]
    progress: int | None
    earned: TrophyCounts
    defined: TrophyCounts
    last_updated: str | None = None


@dataclass(frozen=True, slots=True)
class Presence:
    """A user's current online presence."""

    online_status: str | None = None
    platform: str | None = None
    last_online_date: str | None = None
    game_title: str | None = None


@dataclass(frozen=True, slots=True)
class TrophyDetail:
    """A single trophy's definition merged with the user's earned progress for it.

    ``rarity`` is the percentage of all players who have earned this trophy (lower = rarer).
    """

    trophy_id: int | None
    name: str | None
    detail: str | None
    type: str | None = None
    hidden: bool | None = None
    icon_url: str | None = None
    earned: bool | None = None
    earned_date: str | None = None
    progress_rate: int | None = None
    rarity: float | None = None


@dataclass(frozen=True, slots=True)
class TrophyGroup:
    """One trophy group within a title -- the base game or a single DLC/expansion."""

    group_id: str | None
    name: str | None
    detail: str | None = None
    icon_url: str | None = None
    progress: int | None = None
    defined: TrophyCounts = field(default_factory=TrophyCounts)
    earned: TrophyCounts = field(default_factory=TrophyCounts)
    last_updated: str | None = None


@dataclass(frozen=True, slots=True)
class TrophyGroups:
    """A title's trophy-group breakdown: overall title counts plus one entry per group (base game + DLCs)."""

    title_name: str | None
    platforms: tuple[str, ...]
    progress: int | None
    defined: TrophyCounts
    earned: TrophyCounts
    groups: tuple[TrophyGroup, ...]
    last_updated: str | None = None


@dataclass(frozen=True, slots=True)
class TitleStat:
    """A user's play statistics for a single game title (PS4/PS5 only).

    ``play_duration_seconds`` is the total playtime in seconds (durable/DB-friendly; format for display).
    """

    title_id: str | None
    name: str | None
    category: str | None = None
    play_count: int | None = None
    first_played: str | None = None
    last_played: str | None = None
    play_duration_seconds: int | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class TitleConcept:
    """Store/catalog details for a game title (its "concept" -- the storefront product record).

    ``star_rating`` is the average PS Store user rating (0-5). ``title_ids`` are all the platform SKUs
    (npTitleIds) that share this concept. Ownership is not implied -- this is public catalog metadata.
    """

    concept_id: str | None
    name: str | None = None
    type: str | None = None
    publisher: str | None = None
    release_date: str | None = None
    minimum_age: int | None = None
    content_rating: str | None = None
    rating_authority: str | None = None
    star_rating: float | None = None
    genres: tuple[str, ...] = ()
    title_ids: tuple[str, ...] = ()
    cover_image_url: str | None = None


@dataclass(frozen=True, slots=True)
class Entitlement:
    """An owned PS4/PS5 game or add-on (a purchase/entitlement on the authenticated account).

    ``package_type`` distinguishes the platform generation (e.g. ``"PS5GD"`` / ``"PS4GD"``); ``active``
    reflects whether the entitlement is currently valid (``activeFlag``). ``name`` is the already-resolved
    display fallback (``gameMeta.name`` or ``titleMeta.name``) most callers want; ``game_meta_name``/
    ``concept_meta_name``/``title_meta_name`` are kept separately (undecided) because
    ``curator.catalog.canonicalization_service.canonicalize`` needs the distinction between them --
    ``game_meta_name`` carries "Bonus Content"/"Demo" suffixes for exclusion checks that
    ``title_meta_name`` (preferred for display) often strips.
    """

    entitlement_id: str | None
    name: str | None = None
    title_id: str | None = None
    concept_id: str | None = None
    product_id: str | None = None
    package_type: str | None = None
    game_type: str | None = None
    active: bool | None = None
    active_date: str | None = None
    image_url: str | None = None
    game_meta_name: str | None = None
    concept_meta_name: str | None = None
    title_meta_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileShareLink:
    """A shareable link to the authenticated user's PSN profile, plus a QR-code image of it.

    ``share_image_url`` is a signed (time-limited) URL to a QR image that resolves to
    ``share_image_url_destination`` when scanned.
    """

    share_url: str | None = None
    share_image_url: str | None = None
    share_image_url_destination: str | None = None


@dataclass(frozen=True, slots=True)
class AccountDevice:
    """A console/device registered (activated) to the authenticated account.

    ``activation_type`` distinguishes e.g. ``"PRIMARY"`` (the primary console) from other activation kinds.
    A ``deactivation_date`` (when present) means the device was later deactivated.
    """

    device_id: str | None
    device_type: str | None = None
    device_name: str | None = None
    activation_type: str | None = None
    activation_date: str | None = None
    deactivation_date: str | None = None


# ----------------------------------------------------------------------------------------------------------
# Account PII shape (documentation-only, intentionally never hydrated).
#
# The ``accounts.api.playstation.com/api/v1/accounts/me`` endpoint returns the signed-in account's full
# private record. Curator deliberately does NOT read, map, or persist that PII. The models below exist
# purely as a *guide marker* -- they document what PSN exposes so a future maintainer knows the shape -- but
# no code path populates them, and none should be added without a deliberate privacy decision. The one
# sanctioned field is the email, surfaced narrowly by ``curator.psn.account_client``'s ``account_email``
# (a single string, not persisted). If you ever need more of this data, hydrate it explicitly and
# consciously; do not wire a blanket mapper that pulls the whole record into memory.
# ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountEmail:
    """Shape of an account email address (guide marker -- never hydrated; see the note above)."""

    address: str | None = None
    is_main: bool = False
    is_verified: bool = False
    qualifier: str | None = None


@dataclass(frozen=True, slots=True)
class AccountPhone:
    """Shape of an account phone number (guide marker -- never hydrated; see the note above)."""

    number: str | None = None
    country_code: str | None = None
    is_main: bool = False
    is_verified: bool = False
    qualifier: str | None = None


@dataclass(frozen=True, slots=True)
class AccountAddress:
    """Shape of an account postal address (guide marker -- never hydrated; see the note above).

    ``subdivision`` is PSN's ``countrySubdivision`` (e.g. a US state code).
    """

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    subdivision: str | None = None
    postal_code: str | None = None
    country: str | None = None
    is_main: bool = False
    qualifier: str | None = None


@dataclass(frozen=True, slots=True)
class AccountDetails:
    """Shape of the authenticated user's private account record -- a documentation stub, never hydrated.

    Mirrors what ``accounts.api.playstation.com/.../accounts/me`` returns for the signed-in account (name,
    contact details, date of birth, locale/region, account status). Curator intentionally does not fetch or
    persist this PII: no code constructs a populated instance, so every field stays at its stub default.
    The class is kept only to record what is *available*. For the email, use the narrow, sanctioned
    ``account_email`` client method instead.
    """

    account_id: str | None = None
    online_id: str | None = None
    account_uuid: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    primary_email: str | None = None
    emails: tuple[AccountEmail, ...] = ()
    phones: tuple[AccountPhone, ...] = ()
    addresses: tuple[AccountAddress, ...] = ()
    date_of_birth: str | None = None
    language: str | None = None
    region: str | None = None
    legal_country: str | None = None
    is_banned: bool = False
    is_suspended: bool = False
    np_status: str | None = None
    customer_since: str | None = None
    has_pin: bool = False
    verification_status: str | None = None
    adult_verification_status: str | None = None
    last_update_date: str | None = None


@dataclass(frozen=True, slots=True)
class PersonalDetail:
    """Shape of a friend's shared real-name/birthday detail (guide marker -- never hydrated; see above)."""

    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    profile_picture_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Profile:
    """A user's legacy public profile: about-me text, avatars, languages, verification status.

    From the legacy community-profile endpoint (``us-prof.np.community.playstation.net/.../profile2``),
    distinct from the private ``AccountDetails`` record. ``personal_detail`` is intentionally never hydrated
    (see :class:`PersonalDetail`) even though the endpoint can return it for friends who share it.
    """

    about_me: str | None = None
    avatars: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    is_officially_verified: bool = False
    personal_detail: PersonalDetail | None = None


@dataclass(frozen=True, slots=True)
class SocialUser:
    """A PSN user referenced from a social list (a friend, a blocked account, or a friend request)."""

    account_id: str
    online_id: str | None = None


@dataclass(frozen=True, slots=True)
class Friendship:
    """The authenticated user's friendship standing with another user."""

    relation: str | None = None
    personal_detail_sharing: str | None = None
    friends_count: int | None = None
    mutual_friends_count: int | None = None


@dataclass(frozen=True, slots=True)
class ChatGroup:
    """A gaming-lounge chat group (a group DM thread) the authenticated user participates in.

    ``name`` is the custom group name if one is set; when blank, PSN derives a display name from the
    members, so ``name`` is ``None`` here rather than an empty string.
    """

    group_id: str | None
    name: str | None = None
    favorite: bool | None = None
    member_count: int = 0
    members: tuple[SocialUser, ...] = ()
    modified_at: str | None = None


@dataclass(frozen=True, slots=True)
class SentMessage:
    """The result of sending a message to a chat group."""

    message_uid: str | None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single message in a chat group's conversation history."""

    message_uid: str | None
    body: str | None = None
    message_type: int | None = None
    created_at: str | None = None
    sender: SocialUser | None = None


@dataclass(frozen=True, slots=True)
class LibraryGame:
    """A game from the account's library -- recently played or purchased (PS3/PS4/PS5).

    ``last_played`` is populated for the recently-played list and ``None`` for the purchased list.
    """

    title_id: str | None
    name: str | None = None
    platform: str | None = None
    concept_id: str | None = None
    product_id: str | None = None
    image_url: str | None = None
    last_played: str | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class GameSearchResult:
    """A game or add-on returned by universal search (store/catalog surface, no ownership implied)."""

    id: str | None
    name: str | None = None
    type: str | None = None
    platforms: tuple[str, ...] = ()
    image_url: str | None = None
    price: str | None = None
    discounted_price: str | None = None
    is_free: bool | None = None


@dataclass(frozen=True, slots=True)
class PlayerSearchResult:
    """A player returned by universal search."""

    account_id: str | None
    online_id: str | None = None
    avatar_url: str | None = None
    is_ps_plus: bool | None = None
    relationship: str | None = None


def trophy_counts(source: dict[str, Any] | None) -> TrophyCounts:
    """Build :class:`TrophyCounts` from a raw ``{"bronze": ..., "silver": ..., ...}`` dict.

    :param source: The raw trophy-count dict, or ``None``.
    :returns: The normalized counts.
    """
    if source is None:
        return TrophyCounts()
    return TrophyCounts(
        int(source.get("bronze", 0) or 0),
        int(source.get("silver", 0) or 0),
        int(source.get("gold", 0) or 0),
        int(source.get("platinum", 0) or 0),
    )
