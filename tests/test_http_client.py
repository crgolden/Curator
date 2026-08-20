"""Tests for the process-wide shared TLS context."""

from __future__ import annotations

import ssl

import httpx

from curator.http_client import shared_ssl_context


def test_returns_the_same_context_on_every_call() -> None:
    assert shared_ssl_context() is shared_ssl_context()


def test_verifies_certificates_like_an_unconfigured_client() -> None:
    """The whole point is to change when the CA bundle is read, never what is trusted -- a context that
    skipped verification would make every outbound PSN/RAWG/OpenCritic call trust any certificate."""
    context = shared_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_matches_the_trust_store_httpx_would_have_built_on_its_own() -> None:
    unconfigured = httpx.create_ssl_context()

    assert shared_ssl_context().get_ca_certs() == unconfigured.get_ca_certs()
