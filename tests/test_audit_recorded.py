"""Tests for curator.audit.recorded.recorded: the outcome each way out of the body leaves on the started row."""

from __future__ import annotations

import asyncio

import pytest

from audit_fakes import RecordingAuditRepository
from curator.audit.recorded import recorded
from curator.audit.repository import ACTION_LINK_REVERIFIED, OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_STARTED
from test_values import lowercase_token, new_identity_sub


async def test_a_body_that_returns_marks_the_row_completed() -> None:
    recorder = RecordingAuditRepository()

    async with recorded(recorder, new_identity_sub(), ACTION_LINK_REVERIFIED):
        pass

    assert recorder.outcomes == [(ACTION_LINK_REVERIFIED, OUTCOME_COMPLETED)]


async def test_a_body_that_raises_marks_the_row_failed_and_reraises() -> None:
    recorder = RecordingAuditRepository()
    message = lowercase_token()
    error = ValueError(message)
    attempt = recorded(recorder, new_identity_sub(), ACTION_LINK_REVERIFIED)

    with pytest.raises(ValueError, match=message):
        async with attempt:
            raise error

    assert recorder.outcomes == [(ACTION_LINK_REVERIFIED, OUTCOME_FAILED)]


async def test_a_cancelled_body_leaves_the_row_started_so_the_attempt_still_counts() -> None:
    recorder = RecordingAuditRepository()
    attempt = recorded(recorder, new_identity_sub(), ACTION_LINK_REVERIFIED)

    with pytest.raises(asyncio.CancelledError):
        async with attempt:
            raise asyncio.CancelledError

    assert recorder.outcomes == [(ACTION_LINK_REVERIFIED, OUTCOME_STARTED)]
