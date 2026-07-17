"""Tests for ``curator.deps.require_preference``. Every other function in ``curator.deps`` (``require_bearer``,
``require_verified_caller``, ``require_admin``) is already exercised indirectly through the real routes in
``test_routes.py``/``test_authz.py`` -- ``require_preference`` has no route wired to it yet (a separate
work package builds those), so it is called directly here, against a hand-written fake repository and a
bare ``SimpleNamespace`` standing in for the one thing this dependency actually touches on ``Request``:
``request.app.state.repository``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from curator.deps import require_preference
from curator.persistence.repository import LinkRecord

SUB = "sub-1"


class FakeRepository:
    """Stands in for Repository: in-memory dict of sub -> LinkRecord."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}

    async def get_link(self, sub):
        return self.links.get(sub)


def _link(
    harvest_trophies: bool = False,
    harvest_identity: bool = False,
    harvest_presence: bool = False,
    harvest_devices: bool = False,
) -> LinkRecord:
    return LinkRecord(
        psn_account_id="psn-account-1",
        token_response_enc=b"encrypted",
        access_token_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
        harvest_trophies=harvest_trophies,
        harvest_identity=harvest_identity,
        harvest_presence=harvest_presence,
        harvest_devices=harvest_devices,
    )


def _request(repository: FakeRepository) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repository=repository)))


async def test_require_preference_raises_404_when_no_link():
    repository = FakeRepository()

    with pytest.raises(HTTPException) as exc_info:
        await require_preference(_request(repository), SUB, "harvest_trophies")

    assert exc_info.value.status_code == 404


async def test_require_preference_raises_403_when_category_flag_off():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=False)

    with pytest.raises(HTTPException) as exc_info:
        await require_preference(_request(repository), SUB, "harvest_trophies")

    assert exc_info.value.status_code == 403
    assert "harvest_trophies" in exc_info.value.detail


async def test_require_preference_returns_link_when_category_flag_on():
    repository = FakeRepository()
    seeded = _link(harvest_trophies=True)
    repository.links[SUB] = seeded

    result = await require_preference(_request(repository), SUB, "harvest_trophies")

    assert result == seeded


@pytest.mark.parametrize("category", ["harvest_identity", "harvest_presence", "harvest_devices"])
async def test_require_preference_checks_the_named_category_independently(category):
    repository = FakeRepository()
    # Only harvest_trophies is on -- every other category must still be gated off.
    repository.links[SUB] = _link(harvest_trophies=True)

    with pytest.raises(HTTPException) as exc_info:
        await require_preference(_request(repository), SUB, category)

    assert exc_info.value.status_code == 403
