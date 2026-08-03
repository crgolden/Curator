"""Tests for ``curator.app._library_refresh_continuation_handler``, using hand-written fakes for every
collaborator except the real :class:`~curator.enrichment.enrichment_service.EnrichmentService` and
:class:`~curator.psn.session.PsnSession` -- those are real so the rate-limit control flow
(``RawgApiError`` 429 -> ``EnrichmentRateLimitError`` -> ``RateLimitRetryScheduled``) is exercised end to
end, not re-mocked at the seam that actually raises it. Every game in these tests has ``title_id=None``,
so the real PSN-catalog signal is never attempted and no real PSN network call ever happens; RAWG calls
(when a key is configured) go through ``httpx.MockTransport``.

Unlike ``_library_refresh_handler`` (which always calls ``IngestionService.ingest()`` -- a real PSN
entitlements fetch with no test seam to intercept it), the continuation handler never re-ingests, which is
exactly what makes it fully testable this way.
"""

from __future__ import annotations

import json

import httpx
import pytest

from curator.app import _library_refresh_continuation_handler
from curator.jobs.queue_consumer import RateLimitRetryScheduled
from curator.library.repository import ContinuationGame


class FakeLink:
    def __init__(self, token_response_enc: bytes) -> None:
        self.token_response_enc = token_response_enc


class FakeRepository:
    """Satisfies the narrow slice of ``curator.persistence.repository.Repository`` that
    ``DbTokenStore.load()`` uses."""

    def __init__(self, link: FakeLink | None) -> None:
        self._link = link

    async def get_link(self, sub):
        return self._link


class FakeTokenCrypto:
    """Identity 'encryption' -- good enough since these tests never touch real ciphertext."""

    def decrypt(self, data: bytes) -> bytes:
        return data

    def encrypt(self, data: bytes) -> bytes:
        return data


class FakeEnrichmentKeysRepository:
    def __init__(self, rawg_key: bytes | None = None, opencritic_key: bytes | None = None) -> None:
        self._rawg_key = rawg_key
        self._opencritic_key = opencritic_key
        self.rejected_calls: list[tuple[str, str]] = []

    async def get_decrypted_key_material(self, sub):
        return self._rawg_key, self._opencritic_key

    async def mark_rawg_key_rejected(self, sub):
        self.rejected_calls.append((sub, "rawg"))

    async def mark_opencritic_key_rejected(self, sub):
        self.rejected_calls.append((sub, "opencritic"))


class FakeAuditRepository:
    def __init__(self):
        self.log_calls: list[tuple[str, str, str | None]] = []

    async def log(self, identity_sub, action, detail=None):
        self.log_calls.append((identity_sub, action, detail))


class FakeEnrichmentRepository:
    """Implements every method the real ``EnrichmentService``/``enrich_games`` need -- no BYOK keys means
    RAWG/OpenCritic are never actually called, so the cache reads/writes below just need to be no-ops."""

    def __init__(self, publisher_tier_rules=None):
        self._publisher_tier_rules = publisher_tier_rules or []
        self.save_calls: list[tuple] = []

    async def get_rawg_cache(self, title):
        return None

    async def save_rawg_cache(self, title, *, rawg_game_id, raw):
        return None

    async def get_all_opencritic_games(self):
        return []

    async def save_opencritic_games(self, games):
        return None

    async def get_psn_catalog_cache(self, title_id):
        return None

    async def save_psn_catalog_cache(self, entry):
        return None

    async def get_opencritic_cursor(self, platform):
        return 0

    async def set_opencritic_cursor(self, platform, next_skip):
        return None

    async def get_active_genres(self):
        return []

    async def save_game_enrichment(self, game_id, genre_id, subgenre_id, result):
        self.save_calls.append((game_id, genre_id, subgenre_id, result))

    async def list_publisher_tier_rules(self):
        return self._publisher_tier_rules


class FakeCatalogRepository:
    async def get_size_estimates(self):
        return []


class FakeLibraryRepository:
    def __init__(self, games: list[ContinuationGame]):
        self._games = games

    async def get_games_for_continuation(self, identity_sub, game_ids):
        return [game for game in self._games if game.game_id in game_ids]


class FakeJobRunsRepository:
    def __init__(self, existing_result_summary: dict | None = None):
        self._existing_result_summary = existing_result_summary
        self.rate_limited_calls: list[tuple[str, dict]] = []
        self._next_seq = 0

    async def get(self, run_id):
        if self._existing_result_summary is None:
            return None

        class _Run:
            result_summary = self._existing_result_summary

        return _Run()

    async def mark_rate_limited(self, run_id, result_summary):
        self.rate_limited_calls.append((run_id, result_summary))
        self._next_seq += 1
        return self._next_seq


class FakeQueuePublisher:
    def __init__(self):
        self.continuation_calls: list[tuple] = []

    async def publish_library_refresh_continuation(
        self, run_id, identity_sub, remaining_game_ids, provider, retry_after_seconds, *, seq
    ):
        self.continuation_calls.append((run_id, identity_sub, remaining_game_ids, provider, retry_after_seconds, seq))


def _saved_token_bytes() -> bytes:
    return json.dumps({"refresh_token": "RT1", "refresh_token_expires_at": 9999999999}).encode()


class _Collaborators:
    def __init__(self, *, games, existing_result_summary, rawg_key, http_responder):
        self.repository = FakeRepository(FakeLink(_saved_token_bytes()))
        self.token_crypto = FakeTokenCrypto()
        self.catalog_repository = FakeCatalogRepository()
        self.enrichment_repository = FakeEnrichmentRepository()
        self.library_repository = FakeLibraryRepository(games)
        self.enrichment_keys_repository = FakeEnrichmentKeysRepository(rawg_key=rawg_key)
        self.audit_repository = FakeAuditRepository()
        self.job_runs_repository = FakeJobRunsRepository(existing_result_summary)
        self.queue_publisher = FakeQueuePublisher()
        self.http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(http_responder or (lambda request: httpx.Response(500)))
        )

    def build_handler(self):
        return _library_refresh_continuation_handler(
            repository=self.repository,
            token_crypto=self.token_crypto,
            catalog_repository=self.catalog_repository,
            enrichment_repository=self.enrichment_repository,
            library_repository=self.library_repository,
            enrichment_keys_repository=self.enrichment_keys_repository,
            job_runs_repository=self.job_runs_repository,
            queue_publisher=self.queue_publisher,
            http_client=self.http_client,
            rate_limiter=None,
            redis_adapter=None,
            audit_repository=self.audit_repository,
        )


def _setup(games=(), existing_result_summary=None, rawg_key=None, http_responder=None) -> _Collaborators:
    return _Collaborators(
        games=list(games),
        existing_result_summary=existing_result_summary,
        rawg_key=rawg_key,
        http_responder=http_responder,
    )


async def test_no_psn_link_raises_runtime_error():
    collaborators = _setup(games=[])
    collaborators.repository = FakeRepository(None)  # no PSN link on file
    handle = collaborators.build_handler()

    with pytest.raises(RuntimeError, match="sub-1"):
        await handle(
            {
                "run_id": "r1",
                "identity_sub": "sub-1",
                "remaining_game_ids": ["g1"],
                "provider": "rawg",
                "retry_after_seconds": 3600.0,
            }
        )


async def test_success_merges_new_titles_into_existing_result_summary():
    games = [ContinuationGame(game_id="g1", title="Game A", product_id=None, title_id=None, native_ps5=True)]
    existing_summary = {
        "rawg_enriched_titles": ["Old Game"],
        "opencritic_enriched_titles": [],
        "opencritic_topup_incomplete": False,
    }
    collaborators = _setup(games=games, existing_result_summary=existing_summary)
    handle = collaborators.build_handler()

    result = await handle(
        {
            "run_id": "r1",
            "identity_sub": "sub-1",
            "remaining_game_ids": ["g1"],
            "provider": "rawg",
            "retry_after_seconds": 3600.0,
        }
    )

    assert result is not None
    # No RAWG/OpenCritic key configured -- this game enriches with neither signal, but the prior run's
    # already-enriched titles must still be carried forward untouched.
    assert result["rawg_enriched_titles"] == ["Old Game"]
    assert result["opencritic_enriched_titles"] == []
    assert result["opencritic_topup_incomplete"] is False


async def test_success_enriches_every_remaining_game():
    games = [
        ContinuationGame(game_id="g1", title="Game A", product_id=None, title_id=None, native_ps5=True),
        ContinuationGame(game_id="g2", title="Game B", product_id=None, title_id=None, native_ps5=False),
    ]
    collaborators = _setup(games=games)
    handle = collaborators.build_handler()

    await handle(
        {
            "run_id": "r1",
            "identity_sub": "sub-1",
            "remaining_game_ids": ["g1", "g2"],
            "provider": "rawg",
            "retry_after_seconds": 3600.0,
        }
    )

    saved_game_ids = [call[0] for call in collaborators.enrichment_repository.save_calls]
    assert saved_game_ids == ["g1", "g2"]


async def test_rate_limited_again_marks_rate_limited_republishes_and_raises():
    """A second consecutive rate limit on the same provider must escalate the wait (3600s -> 7200s), not
    reset to the default -- each continuation message builds a fresh EnrichmentService, so this only
    happens because the handler seeds it from the inbound payload's own ``retry_after_seconds``."""
    games = [
        ContinuationGame(game_id="g1", title="Game A", product_id=None, title_id=None, native_ps5=True),
        ContinuationGame(game_id="g2", title="Game B", product_id=None, title_id=None, native_ps5=False),
    ]
    collaborators = _setup(games=games, rawg_key=b"rawg-key", http_responder=lambda request: httpx.Response(429))
    handle = collaborators.build_handler()

    with pytest.raises(RateLimitRetryScheduled):
        await handle(
            {
                "run_id": "r1",
                "identity_sub": "sub-1",
                "remaining_game_ids": ["g1", "g2"],
                "provider": "rawg",
                "retry_after_seconds": 3600.0,
            }
        )

    assert len(collaborators.job_runs_repository.rate_limited_calls) == 1
    run_id, summary = collaborators.job_runs_repository.rate_limited_calls[0]
    assert run_id == "r1"
    assert summary["rate_limited_provider"] == "rawg"
    assert summary["retry_after_seconds"] == 7200.0
    assert summary["remaining_count"] == 2  # g1 (the one that hit the limit) + g2, neither yet attempted

    assert len(collaborators.queue_publisher.continuation_calls) == 1
    republish_run_id, republish_sub, remaining_game_ids, provider, retry_after_seconds, seq = (
        collaborators.queue_publisher.continuation_calls[0]
    )
    assert republish_run_id == "r1"
    assert republish_sub == "sub-1"
    assert remaining_game_ids == ["g1", "g2"]
    assert provider == "rawg"
    assert retry_after_seconds == 7200.0
    assert seq == 1  # mark_rate_limited's first call for this run in this fake's lifetime


async def test_rejected_key_degrades_the_run_and_records_the_rejection():
    games = [ContinuationGame(game_id="g1", title="Game A", product_id=None, title_id=None, native_ps5=True)]
    collaborators = _setup(games=games, rawg_key=b"rawg-key", http_responder=lambda request: httpx.Response(401))
    handle = collaborators.build_handler()

    result = await handle(
        {
            "run_id": "r1",
            "identity_sub": "sub-1",
            "remaining_game_ids": ["g1"],
            "provider": "opencritic",  # this call's own resume reason -- unrelated to the RAWG rejection
            "retry_after_seconds": 3600.0,
        }
    )

    # The run reaches completion (returns a result summary, doesn't raise) despite RAWG rejecting the key.
    assert result is not None
    assert result["rejected_providers"] == ["rawg"]
    assert collaborators.enrichment_keys_repository.rejected_calls == [("sub-1", "rawg")]
    assert collaborators.audit_repository.log_calls == [("sub-1", "enrichment_key_rejected", "rawg")]
