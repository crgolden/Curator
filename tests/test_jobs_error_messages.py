"""Tests for friendly_job_error: every mapped exception category, plus the generic fallback."""

from __future__ import annotations

from curator.enrichment.enrichment_service import EnrichmentAuthError
from curator.enrichment.opencritic_client import OpenCriticApiError
from curator.enrichment.rawg_client import RawgApiError
from curator.jobs.error_messages import friendly_job_error
from curator.psn.errors import PsnAuthError


def test_enrichment_auth_error_names_the_provider():
    message = friendly_job_error(EnrichmentAuthError("rawg", "RAWG request failed with status 401"))
    assert message == "Your RAWG API key was rejected. Check that it's correct and try again."


def test_enrichment_auth_error_opencritic():
    message = friendly_job_error(EnrichmentAuthError("opencritic", "OpenCritic request failed with status 403"))
    assert message == "Your OPENCRITIC API key was rejected. Check that it's correct and try again."


def test_rawg_rate_limit_maps_to_rate_limit_message():
    message = friendly_job_error(RawgApiError("RAWG request failed with status 429", status_code=429))
    assert message == "Enrichment provider rate limit reached. Try again later."


def test_opencritic_rate_limit_maps_to_rate_limit_message():
    message = friendly_job_error(OpenCriticApiError("OpenCritic request failed with status 429", status_code=429))
    assert message == "Enrichment provider rate limit reached. Try again later."


def test_rawg_non_rate_limit_status_falls_back_to_generic():
    message = friendly_job_error(RawgApiError("RAWG request failed with status 500", status_code=500))
    assert message == "The job failed unexpectedly. If this keeps happening, contact support."


def test_psn_auth_error_maps_to_relink_message():
    message = friendly_job_error(PsnAuthError("npsso expired"))
    assert message == "Your PlayStation Network link has expired or was rejected. Re-link your account and try again."


def test_unrecognized_exception_falls_back_to_generic():
    message = friendly_job_error(RuntimeError("some internal detail that must never leak"))
    assert message == "The job failed unexpectedly. If this keeps happening, contact support."
    assert "internal detail" not in message
