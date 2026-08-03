"""Tests for EnrichmentService orchestration, using hand-written fakes for every collaborator."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.enrichment_service import EnrichmentAuthError, EnrichmentRateLimitError, EnrichmentService
from curator.enrichment.opencritic_client import OpenCriticApiError, PaginationResult
from curator.enrichment.opencritic_matcher import OpenCriticGame
from curator.enrichment.publisher_tier import PublisherTierRule
from curator.enrichment.rawg_client import RawgApiError
from curator.enrichment.repository import PsnCatalogCacheEntry
from curator.psn.models import TitleConcept
from curator.scoring.size_estimation_service import SizeEstimate

_PUBLISHER_RULES = [
    PublisherTierRule(tier_id="1", pattern="sony", tier="AAA", match_kind="substring"),
]
_SIZE_ESTIMATES = [
    SizeEstimate(estimate_id="1", title_pattern=None, aaa_tier="AAA", genre_class=None, platform="PS5", size_gb=59),
    SizeEstimate(estimate_id="2", title_pattern=None, aaa_tier="Indie", genre_class=None, platform="PS5", size_gb=16),
]
_GENRE_PRIORITIES = {"action": 0, "adventure": 1, "rpg": 2}


class FakeRawgClient:
    def __init__(self, search_results=None, detail=None, search_raises=None, detail_raises=None):
        self._search_results = search_results or []
        self._detail = detail
        self._search_raises = search_raises
        self._detail_raises = detail_raises
        self.search_calls: list[str] = []
        self.detail_calls: list[int] = []

    async def search_games(self, title, *, page_size=5):
        self.search_calls.append(title)
        if self._search_raises:
            raise self._search_raises
        return self._search_results

    async def fetch_detail(self, rawg_game_id):
        self.detail_calls.append(rawg_game_id)
        if self._detail_raises:
            raise self._detail_raises
        return self._detail


class FakeOpenCriticClient:
    def __init__(self, games_by_platform=None, results_by_platform=None, raises=None):
        self._games_by_platform = games_by_platform or {}
        self._results_by_platform = results_by_platform or {}
        self._raises = raises
        self.fetch_calls: list[tuple[str, int, int | None]] = []

    async def fetch_platform_games(self, platform, *, start_skip=0, max_pages=None):
        self.fetch_calls.append((platform, start_skip, max_pages))
        if self._raises:
            raise self._raises
        if platform in self._results_by_platform:
            return self._results_by_platform[platform]
        games = self._games_by_platform.get(platform, [])
        return PaginationResult(games=games, next_skip=0, exhausted=True)


class FakeCatalogClient:
    def __init__(self, concept=None, raises=None):
        self._concept = concept
        self._raises = raises
        self.title_concept_calls: list[str] = []

    async def title_concept(self, title_id, platform="PS5"):
        self.title_concept_calls.append(title_id)
        if self._raises:
            raise self._raises
        return self._concept


class FakeEnrichmentRepository:
    def __init__(self, opencritic_games=None):
        self.rawg_cache: dict[str, tuple] = {}
        self.psn_cache: dict[str, PsnCatalogCacheEntry] = {}
        self.opencritic_games = opencritic_games or []
        self.saved_opencritic_batches: list[list[OpenCriticGame]] = []
        self.opencritic_cursors: dict[str, int] = {}

    async def get_rawg_cache(self, title):
        from curator.enrichment.rawg_matcher import normalize
        from curator.enrichment.repository import RawgCacheEntry

        key = normalize(title)
        if key not in self.rawg_cache:
            return None
        rawg_game_id, raw = self.rawg_cache[key]
        return RawgCacheEntry(normalized_title=key, rawg_game_id=rawg_game_id, raw=raw)

    async def save_rawg_cache(self, title, *, rawg_game_id, raw):
        from curator.enrichment.rawg_matcher import normalize

        self.rawg_cache[normalize(title)] = (rawg_game_id, raw)

    async def get_all_opencritic_games(self):
        return self.opencritic_games

    async def save_opencritic_games(self, games):
        self.saved_opencritic_batches.append(games)
        self.opencritic_games = [*self.opencritic_games, *games]

    async def get_psn_catalog_cache(self, title_id):
        return self.psn_cache.get(title_id)

    async def save_psn_catalog_cache(self, entry):
        self.psn_cache[entry.title_id] = entry

    async def get_opencritic_cursor(self, platform):
        return self.opencritic_cursors.get(platform, 0)

    async def set_opencritic_cursor(self, platform, next_skip):
        self.opencritic_cursors[platform] = next_skip


_UNSET = object()


def _service(rawg_client=_UNSET, opencritic_client=_UNSET, catalog_client=None, repository=None):
    return EnrichmentService(
        rawg_client=FakeRawgClient() if rawg_client is _UNSET else rawg_client,
        opencritic_client=FakeOpenCriticClient() if opencritic_client is _UNSET else opencritic_client,
        catalog_client=catalog_client or FakeCatalogClient(),
        repository=repository or FakeEnrichmentRepository(),
    )


def _rawg_detail(genres=("Action",), developers=("Dev Co",), publishers=("Sony",), metacritic=85, tags=()):
    return {
        "genres": [{"name": g} for g in genres],
        "developers": [{"name": d} for d in developers],
        "publishers": [{"name": p} for p in publishers],
        "metacritic": metacritic,
        "released": "2020-06-15",
        "esrb_rating": {"name": "Mature"},
        "tags": [{"name": t} for t in tags],
    }


async def test_enrich_game_resolves_rawg_genre_publisher_and_size():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="God of War", platform_ids=frozenset({187}))
    rawg_client = FakeRawgClient(search_results=[candidate], detail=_rawg_detail())
    repository = FakeEnrichmentRepository()
    service = _service(rawg_client=rawg_client, repository=repository)

    result, size = await service.enrich_game(
        "God of War",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == "Action"
    assert result.publisher == "Sony"
    assert result.developer == "Dev Co"
    assert result.aaa_tier == "AAA"
    assert result.critical_score == 85.0
    assert result.release_year == 2020
    assert result.esrb == "Mature"
    assert size == 59
    assert rawg_client.search_calls == ["God of War"]
    assert rawg_client.detail_calls == [1]


async def test_enrich_game_uses_cached_rawg_result_without_calling_client_again():
    from curator.enrichment.rawg_matcher import normalize

    repository = FakeEnrichmentRepository()
    repository.rawg_cache[normalize("God of War")] = (1, _rawg_detail())
    rawg_client = FakeRawgClient()
    service = _service(rawg_client=rawg_client, repository=repository)

    result, _ = await service.enrich_game(
        "God of War",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == "Action"
    assert rawg_client.search_calls == []


async def test_enrich_game_no_rawg_match_caches_none_and_defaults_indie():
    rawg_client = FakeRawgClient(search_results=[])
    repository = FakeEnrichmentRepository()
    service = _service(rawg_client=rawg_client, repository=repository)

    result, size = await service.enrich_game(
        "Unknown Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == ""
    assert result.aaa_tier == "Indie"
    assert result.score_source is None
    assert size == 16
    from curator.enrichment.rawg_matcher import normalize

    assert repository.rawg_cache[normalize("Unknown Game")] == (None, None)


async def test_enrich_game_psn_genres_override_rawg_genres():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Some Game", platform_ids=frozenset({187}))
    rawg_client = FakeRawgClient(search_results=[candidate], detail=_rawg_detail(genres=("RPG",)))
    concept = TitleConcept(concept_id="c1", genres=("Action", "Adventure"))
    catalog_client = FakeCatalogClient(concept=concept)
    service = _service(rawg_client=rawg_client, catalog_client=catalog_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == "Action"
    assert result.subgenre == "Adventure"
    assert catalog_client.title_concept_calls == ["p1"]


async def test_enrich_game_calls_catalog_client_with_title_id_not_product_id():
    """Regression test for the product_id/title_id mix-up (0023_library_entries_title_id.sql): PSN's
    catalog "concepts" endpoint requires an npTitleId (e.g. "CUSA13505_00"), not a PSN product id (e.g.
    "UP0700-CUSA13505_00-ELEVENELEVEN1111"). Passing the wrong identifier doesn't error -- it silently
    resolves nothing -- so a passing suite that never asserts on the exact value reaching the catalog
    client would not have caught this. This asserts the real npTitleId-shaped id is what the catalog
    client actually receives.
    """
    concept = TitleConcept(concept_id="c1", genres=("Action",), star_rating=4.5)
    catalog_client = FakeCatalogClient(concept=concept)
    service = _service(catalog_client=catalog_client)

    result, _ = await service.enrich_game(
        "Eleven Eleven",
        title_id="CUSA13505_00",
        is_ps5=False,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert catalog_client.title_concept_calls == ["CUSA13505_00"]
    assert result.psn_rating == 4.5


async def test_enrich_game_psn_catalog_cache_avoids_second_lookup():
    concept = TitleConcept(concept_id="c1", genres=("Action",))
    catalog_client = FakeCatalogClient(concept=concept)
    repository = FakeEnrichmentRepository()
    service = _service(catalog_client=catalog_client, repository=repository)

    await service.enrich_game(
        "Game A",
        title_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )
    await service.enrich_game(
        "Game A",
        title_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert catalog_client.title_concept_calls == ["p1"]


async def test_enrich_game_detects_multiplayer_from_tags():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Multiplayer Game", platform_ids=frozenset({187}))
    detail = _rawg_detail(tags=("Co-op", "Singleplayer"))
    rawg_client = FakeRawgClient(search_results=[candidate], detail=detail)
    service = _service(rawg_client=rawg_client)

    result, _ = await service.enrich_game(
        "Multiplayer Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.multiplayer is True


async def test_enrich_game_resolves_opencritic_score_and_source():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Combo Game", platform_ids=frozenset({187}))
    rawg_client = FakeRawgClient(search_results=[candidate], detail=_rawg_detail(metacritic=90))
    oc_game = OpenCriticGame(
        oc_game_id=1, name="Combo Game", top_critic_score=80, tier="Strong", percent_recommended=95
    )
    repository = FakeEnrichmentRepository(opencritic_games=[oc_game])
    service = _service(rawg_client=rawg_client, repository=repository)

    result, _ = await service.enrich_game(
        "Combo Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.oc_score == 80
    assert result.oc_tier == "Strong"
    assert result.score_source == "RAWG + OC"


async def test_enrich_game_without_catalog_client_skips_psn_genre_resolution():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Some Game", platform_ids=frozenset({187}))
    rawg_client = FakeRawgClient(search_results=[candidate], detail=_rawg_detail(genres=("RPG",)))
    service = EnrichmentService(
        rawg_client=rawg_client, opencritic_client=FakeOpenCriticClient(), repository=FakeEnrichmentRepository()
    )

    result, _ = await service.enrich_game(
        "Some Game",
        title_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == "RPG"  # falls back to RAWG's genre since no PSN catalog_client is configured


async def test_refresh_opencritic_cache_paginates_both_platforms_and_saves():
    ps4_games = [
        OpenCriticGame(oc_game_id=1, name="PS4 Game", top_critic_score=80, tier="Strong", percent_recommended=80)
    ]
    ps5_games = [
        OpenCriticGame(oc_game_id=2, name="PS5 Game", top_critic_score=85, tier="Strong", percent_recommended=85)
    ]
    opencritic_client = FakeOpenCriticClient(games_by_platform={"ps4": ps4_games, "ps5": ps5_games})
    repository = FakeEnrichmentRepository()
    service = _service(opencritic_client=opencritic_client, repository=repository)

    total = await service.refresh_opencritic_cache()

    assert total == 2
    assert [call[0] for call in opencritic_client.fetch_calls] == ["ps4", "ps5"]
    assert repository.saved_opencritic_batches == [ps4_games, ps5_games]


async def test_refresh_opencritic_cache_resumes_from_and_advances_the_shared_cursor():
    repository = FakeEnrichmentRepository()
    repository.opencritic_cursors["ps4"] = 40
    opencritic_client = FakeOpenCriticClient(
        results_by_platform={
            "ps4": PaginationResult(games=[], next_skip=60, exhausted=False),
            "ps5": PaginationResult(games=[], next_skip=0, exhausted=True),
        }
    )
    service = _service(opencritic_client=opencritic_client, repository=repository)

    await service.refresh_opencritic_cache()

    assert opencritic_client.fetch_calls[0] == ("ps4", 40, None)
    assert repository.opencritic_cursors["ps4"] == 60
    assert repository.opencritic_cursors["ps5"] == 0


async def test_refresh_opencritic_cache_requires_a_configured_client():
    service = _service(opencritic_client=None)

    with pytest.raises(RuntimeError):
        await service.refresh_opencritic_cache()


async def test_refresh_opencritic_cache_translates_401_to_auth_error():
    opencritic_client = FakeOpenCriticClient(raises=OpenCriticApiError("bad key", status_code=401))
    service = _service(opencritic_client=opencritic_client)

    with pytest.raises(EnrichmentAuthError) as exc_info:
        await service.refresh_opencritic_cache()

    assert exc_info.value.provider == "opencritic"


async def test_refresh_opencritic_cache_translates_403_to_auth_error():
    opencritic_client = FakeOpenCriticClient(raises=OpenCriticApiError("forbidden", status_code=403))
    service = _service(opencritic_client=opencritic_client)

    with pytest.raises(EnrichmentAuthError):
        await service.refresh_opencritic_cache()


async def test_refresh_opencritic_cache_translates_429_to_rate_limit_error():
    opencritic_client = FakeOpenCriticClient(
        raises=OpenCriticApiError("rate limited", status_code=429, retry_after_seconds=120.0)
    )
    service = _service(opencritic_client=opencritic_client)

    with pytest.raises(EnrichmentRateLimitError) as exc_info:
        await service.refresh_opencritic_cache()

    assert exc_info.value.provider == "opencritic"
    assert exc_info.value.retry_after_seconds == 120.0


async def test_refresh_opencritic_cache_skips_platform_on_5xx():
    opencritic_client = FakeOpenCriticClient(raises=OpenCriticApiError("server error", status_code=503))
    repository = FakeEnrichmentRepository()
    service = _service(opencritic_client=opencritic_client, repository=repository)

    total = await service.refresh_opencritic_cache()

    assert total == 0
    assert repository.saved_opencritic_batches == []


async def test_has_rawg_client_reflects_configured_client():
    assert _service(rawg_client=FakeRawgClient()).has_rawg_client is True
    assert _service(rawg_client=None).has_rawg_client is False


async def test_has_opencritic_client_reflects_configured_client():
    assert _service(opencritic_client=FakeOpenCriticClient()).has_opencritic_client is True
    assert _service(opencritic_client=None).has_opencritic_client is False


async def test_has_catalog_client_reflects_configured_client():
    assert _service(catalog_client=FakeCatalogClient()).has_catalog_client is True
    # _service() substitutes a falsy catalog_client with a FakeCatalogClient() default (matching how
    # every other caller treats "no client given" as "use the default"), so exercising the True-`None`
    # case needs a direct construction rather than going through that helper.
    service = EnrichmentService(
        rawg_client=FakeRawgClient(), opencritic_client=FakeOpenCriticClient(), repository=FakeEnrichmentRepository()
    )
    assert service.has_catalog_client is False


async def test_enrich_game_with_no_rawg_client_skips_rawg_signal_silently():
    service = _service(rawg_client=None)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.critical_score is None
    assert result.rawg_enriched is False


async def test_enrich_game_rawg_auth_failure_raises_enrichment_auth_error():
    rawg_client = FakeRawgClient(search_raises=RawgApiError("bad key", status_code=401))
    service = _service(rawg_client=rawg_client)

    with pytest.raises(EnrichmentAuthError) as exc_info:
        await service.enrich_game(
            "Some Game",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    assert exc_info.value.provider == "rawg"


async def test_enrich_game_rawg_transient_failure_skips_that_games_rawg_signal():
    rawg_client = FakeRawgClient(search_raises=RawgApiError("server error", status_code=500))
    service = _service(rawg_client=rawg_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.rawg_enriched is False


async def test_enrich_game_rawg_search_timeout_skips_that_games_rawg_signal():
    # httpx.ReadTimeout is not a RawgApiError -- RawgClient never wraps it, so this exercises the
    # separate except httpx.HTTPError branch, not the RawgApiError one above.
    rawg_client = FakeRawgClient(search_raises=httpx.ReadTimeout("timed out"))
    service = _service(rawg_client=rawg_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.rawg_enriched is False


async def test_enrich_game_rawg_detail_timeout_skips_that_games_rawg_signal():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Some Game", platform_ids=frozenset({187}))
    rawg_client = FakeRawgClient(search_results=[candidate], detail_raises=httpx.ConnectError("refused"))
    service = _service(rawg_client=rawg_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.rawg_enriched is False


async def test_enrich_game_rawg_rate_limit_raises_enrichment_rate_limit_error_with_header_hint():
    rawg_client = FakeRawgClient(search_raises=RawgApiError("rate limited", status_code=429, retry_after_seconds=120.0))
    service = _service(rawg_client=rawg_client)

    with pytest.raises(EnrichmentRateLimitError) as exc_info:
        await service.enrich_game(
            "Some Game",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    assert exc_info.value.provider == "rawg"
    assert exc_info.value.retry_after_seconds == 120.0


async def test_enrich_game_rawg_rate_limit_falls_back_to_default_and_doubles_on_repeat():
    rawg_client = FakeRawgClient(search_raises=RawgApiError("rate limited", status_code=429))
    service = _service(rawg_client=rawg_client)

    with pytest.raises(EnrichmentRateLimitError) as first:
        await service.enrich_game(
            "Game A",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )
    with pytest.raises(EnrichmentRateLimitError) as second:
        await service.enrich_game(
            "Game B",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    assert first.value.retry_after_seconds == 3600.0
    assert second.value.retry_after_seconds == 7200.0


async def test_enrich_game_opencritic_topup_timeout_sets_incomplete_flag_not_a_raised_error():
    # httpx.ReadTimeout is not an OpenCriticApiError -- OpenCriticClient never wraps it, so this exercises
    # the separate except httpx.HTTPError branch alongside the existing 5xx one.
    opencritic_client = FakeOpenCriticClient(raises=httpx.ReadTimeout("timed out"))
    service = _service(opencritic_client=opencritic_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.opencritic_enriched is False
    assert service.opencritic_topup_incomplete is True


async def test_enrich_game_psn_catalog_timeout_skips_that_games_psn_signal():
    # httpx.ReadTimeout is not something CatalogClient wraps into a Curator-specific error -- exercises
    # the same transient-network except httpx.HTTPError handling already applied to RAWG/OpenCritic above.
    catalog_client = FakeCatalogClient(raises=httpx.ReadTimeout("timed out"))
    repository = FakeEnrichmentRepository()
    service = _service(catalog_client=catalog_client, repository=repository)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.psn_rating is None
    assert catalog_client.title_concept_calls == ["p1"]
    assert repository.psn_cache == {}  # a transient failure is never cached as a false negative


async def test_disable_provider_makes_rawg_behave_as_not_configured():
    rawg_client = FakeRawgClient()
    service = _service(rawg_client=rawg_client)
    assert service.has_rawg_client is True

    service.disable_provider("rawg")

    assert service.has_rawg_client is False
    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )
    assert result.rawg_enriched is False
    assert rawg_client.search_calls == []  # never called at all once disabled


async def test_disable_provider_makes_opencritic_behave_as_not_configured():
    service = _service()
    assert service.has_opencritic_client is True

    service.disable_provider("opencritic")

    assert service.has_opencritic_client is False


async def test_enrich_game_opencritic_rate_limit_raises_enrichment_rate_limit_error():
    opencritic_client = FakeOpenCriticClient(raises=OpenCriticApiError("rate limited", status_code=429))
    service = _service(opencritic_client=opencritic_client)

    with pytest.raises(EnrichmentRateLimitError) as exc_info:
        await service.enrich_game(
            "Some Game",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    assert exc_info.value.provider == "opencritic"


async def test_enrich_game_rawg_match_sets_rawg_enriched_true_even_without_a_score():
    from curator.enrichment.rawg_matcher import RawgCandidate

    candidate = RawgCandidate(rawg_game_id=1, name="Some Game", platform_ids=frozenset({187}))
    detail = _rawg_detail(metacritic=None)
    rawg_client = FakeRawgClient(search_results=[candidate], detail=detail)
    service = _service(rawg_client=rawg_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.critical_score is None
    assert result.rawg_enriched is True  # a real match, even with no usable Metacritic score


async def test_enrich_game_with_no_opencritic_client_skips_opencritic_signal_silently():
    service = _service(opencritic_client=None)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.oc_score is None
    assert result.opencritic_enriched is False


async def test_enrich_game_opencritic_cache_miss_triggers_one_topup_and_then_matches():
    oc_game = OpenCriticGame(oc_game_id=1, name="Some Game", top_critic_score=80, tier="Strong", percent_recommended=95)
    opencritic_client = FakeOpenCriticClient(games_by_platform={"ps4": [], "ps5": [oc_game]})
    service = _service(opencritic_client=opencritic_client)

    result, _ = await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.opencritic_enriched is True
    assert result.oc_score == 80
    assert [call[0] for call in opencritic_client.fetch_calls] == ["ps4", "ps5"]


async def test_enrich_game_opencritic_topup_only_attempted_once_per_service_instance():
    opencritic_client = FakeOpenCriticClient(games_by_platform={"ps4": [], "ps5": []})
    service = _service(opencritic_client=opencritic_client)

    for _ in range(2):
        await service.enrich_game(
            "Some Game",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    # Two misses, but only one top-up attempt (both platforms) across the whole run.
    assert len(opencritic_client.fetch_calls) == 2


async def test_enrich_game_opencritic_topup_incomplete_flag_set_when_not_exhausted():
    opencritic_client = FakeOpenCriticClient(
        results_by_platform={
            "ps4": PaginationResult(games=[], next_skip=100, exhausted=False),
            "ps5": PaginationResult(games=[], next_skip=0, exhausted=True),
        }
    )
    service = _service(opencritic_client=opencritic_client)

    await service.enrich_game(
        "Some Game",
        title_id=None,
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert service.opencritic_topup_incomplete is True


async def test_enrich_game_opencritic_topup_auth_failure_raises_enrichment_auth_error():
    opencritic_client = FakeOpenCriticClient(raises=OpenCriticApiError("bad key", status_code=403))
    service = _service(opencritic_client=opencritic_client)

    with pytest.raises(EnrichmentAuthError) as exc_info:
        await service.enrich_game(
            "Some Game",
            title_id=None,
            is_ps5=True,
            genre_priorities=_GENRE_PRIORITIES,
            publisher_tier_rules=_PUBLISHER_RULES,
            size_estimates=_SIZE_ESTIMATES,
        )

    assert exc_info.value.provider == "opencritic"
