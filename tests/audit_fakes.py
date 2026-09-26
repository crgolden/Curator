"""An in-memory ``account_action_log`` for route tests: records every row with its outcome, and can be told
to fail the write that starts a row or the one that marks its outcome."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from curator.audit.repository import OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_STARTED, AccountActionLogEntry


@dataclass
class RecordedRow:
    identity_sub: str
    action: str
    detail: str | None
    outcome: str


class RecordingAuditRepository:
    def __init__(self) -> None:
        self.rows: list[RecordedRow] = []
        self.begin_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.log_error: Exception | None = None

    async def log(self, identity_sub: str, action: str, detail: str | None = None) -> None:
        if self.log_error is not None:
            raise self.log_error
        self.rows.append(RecordedRow(identity_sub, action, detail, OUTCOME_COMPLETED))

    async def begin(self, identity_sub: str, action: str, detail: str | None = None) -> str:
        if self.begin_error is not None:
            raise self.begin_error
        self.rows.append(RecordedRow(identity_sub, action, detail, OUTCOME_STARTED))
        return str(len(self.rows) - 1)

    async def finish(self, log_id: str, outcome: str, detail: str | None = None) -> None:
        if self.finish_error is not None:
            raise self.finish_error
        row = self.rows[int(log_id)]
        if row.outcome != OUTCOME_STARTED:
            raise RuntimeError(f"row {log_id} was not a started row")
        row.outcome = outcome
        if detail is not None:
            row.detail = detail

    async def count_since(self, identity_sub: str, actions: tuple[str, ...], since: datetime) -> int:
        return len(
            [
                row
                for row in self.rows
                if row.identity_sub == identity_sub and row.action in actions and row.outcome != OUTCOME_FAILED
            ]
        )

    async def list_for_user(self, identity_sub: str) -> list[AccountActionLogEntry]:
        return [
            AccountActionLogEntry(
                log_id=str(index),
                identity_sub=row.identity_sub,
                action=row.action,
                detail=row.detail,
                occurred_at=datetime.now(timezone.utc),
                outcome=row.outcome,
            )
            for index, row in enumerate(self.rows)
            if row.identity_sub == identity_sub
        ]

    @property
    def entries(self) -> list[tuple[str, str, str | None]]:
        """The completed rows, as ``(identity_sub, action, detail)``."""
        return [(row.identity_sub, row.action, row.detail) for row in self.rows if row.outcome == OUTCOME_COMPLETED]

    @property
    def outcomes(self) -> list[tuple[str, str]]:
        """Every row's ``(action, outcome)``, in write order."""
        return [(row.action, row.outcome) for row in self.rows]
