"""Tests for EnrichmentService orchestration, using hand-written fakes for every collaborator."""

from __future__ import annotations

from curator.enrichment.enrichment_service import EnrichmentService
from curator.enrichment.opencritic_matcher import OpenCriticGame
from curator.enrichment.publisher_tier import PublisherTierRule
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
    def __init__(self, search_results=None, detail=None):
        self._search_results = search_results or []
        self._detail = detail
        self.search_calls: list[str] = []
        self.detail_calls: list[int] = []

    async def search_games(self, title, *, page_size=5):
        self.search_calls.append(title)
        return self._search_results

    async def fetch_detail(self, rawg_game_id):
        self.detail_calls.append(rawg_game_id)
        return self._detail


class FakeOpenCriticClient:
    def __init__(self, games_by_platform=None):
        self._games_by_platform = games_by_platform or {}
        self.fetch_calls: list[str] = []

    async def fetch_platform_games(self, platform, *, start_skip=0, max_pages=None):
        self.fetch_calls.append(platform)
        return self._games_by_platform.get(platform, [])


class FakeCatalogClient:
    def __init__(self, concept=None):
        self._concept = concept
        self.title_concept_calls: list[str] = []

    async def title_concept(self, title_id, platform="PS5"):
        self.title_concept_calls.append(title_id)
        return self._concept


class FakeEnrichmentRepository:
    def __init__(self, opencritic_games=None):
        self.rawg_cache: dict[str, tuple] = {}
        self.psn_cache: dict[str, PsnCatalogCacheEntry] = {}
        self.opencritic_games = opencritic_games or []
        self.saved_opencritic_batches: list[list[OpenCriticGame]] = []

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

    async def get_psn_catalog_cache(self, product_id):
        return self.psn_cache.get(product_id)

    async def save_psn_catalog_cache(self, entry):
        self.psn_cache[entry.product_id] = entry


def _service(rawg_client=None, opencritic_client=None, catalog_client=None, repository=None):
    return EnrichmentService(
        rawg_client=rawg_client or FakeRawgClient(),
        opencritic_client=opencritic_client or FakeOpenCriticClient(),
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
        product_id=None,
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
        product_id=None,
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
        product_id=None,
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
        product_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )

    assert result.genre == "Action"
    assert result.subgenre == "Adventure"
    assert catalog_client.title_concept_calls == ["p1"]


async def test_enrich_game_psn_catalog_cache_avoids_second_lookup():
    concept = TitleConcept(concept_id="c1", genres=("Action",))
    catalog_client = FakeCatalogClient(concept=concept)
    repository = FakeEnrichmentRepository()
    service = _service(catalog_client=catalog_client, repository=repository)

    await service.enrich_game(
        "Game A",
        product_id="p1",
        is_ps5=True,
        genre_priorities=_GENRE_PRIORITIES,
        publisher_tier_rules=_PUBLISHER_RULES,
        size_estimates=_SIZE_ESTIMATES,
    )
    await service.enrich_game(
        "Game A",
        product_id="p1",
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
        product_id=None,
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
        product_id=None,
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
        product_id="p1",
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
    assert opencritic_client.fetch_calls == ["ps4", "ps5"]
    assert repository.saved_opencritic_batches == [ps4_games, ps5_games]
