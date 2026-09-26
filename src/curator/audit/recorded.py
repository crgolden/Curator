"""Write-ahead recording of every action that uses a person's PSN token.

:func:`recorded` writes the ``account_action_log`` row before its body runs and marks the outcome after,
so nothing inside the body can touch the token unless the history row landed first. It never catches a
failed history write: that failure is the request's failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request

from curator.audit.repository import OUTCOME_COMPLETED, OUTCOME_FAILED


class ActionRecorder(Protocol):
    """The slice of :class:`curator.audit.repository.AccountActionLogRepository` write-ahead recording needs."""

    async def begin(self, identity_sub: str, action: str, detail: str | None = None) -> str:
        """Write a ``started`` row and return its ``log_id``."""
        ...

    async def finish(self, log_id: str, outcome: str, detail: str | None = None) -> None:
        """Mark a ``started`` row's outcome."""
        ...


def request_recorder(request: Request) -> ActionRecorder:
    """Return the app's history repository, as the route layer reaches it.

    :param request: The incoming request, whose app state carries ``audit_repository``.
    """
    recorder: ActionRecorder = request.app.state.audit_repository
    return recorder


@dataclass(slots=True)
class RecordedAction:
    """The row :func:`recorded` wrote. Setting ``detail`` replaces the row's detail when the outcome is marked.

    :param log_id: The ``account_action_log`` row id.
    :param detail: The detail the outcome update writes, or ``None`` to keep the one written at the start.
    :param outcome: The outcome written when the body exits without raising; a body that handles a PSN
        failure itself sets :data:`~curator.audit.repository.OUTCOME_FAILED` here.
    """

    log_id: str
    detail: str | None = None
    outcome: str = OUTCOME_COMPLETED


@asynccontextmanager
async def recorded(
    recorder: ActionRecorder, identity_sub: str, action: str, detail: str | None = None
) -> AsyncIterator[RecordedAction]:
    """Record ``action`` before the body runs, then mark it ``failed`` if the body raised, else the
    outcome the body left on the entry (``completed`` unless it set otherwise).

    :param recorder: The history repository.
    :param identity_sub: The Identity ``sub`` claim whose token the body uses.
    :param action: One of the ``account_action_log.action`` CHECK values.
    :param detail: A short summary written with the ``started`` row.
    """
    entry = RecordedAction(await recorder.begin(identity_sub, action, detail))
    try:
        yield entry
    except Exception:
        await recorder.finish(entry.log_id, OUTCOME_FAILED, entry.detail)
        raise
    await recorder.finish(entry.log_id, entry.outcome, entry.detail)
