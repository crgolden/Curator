"""Whether an already-in-flight :class:`~curator.jobs.repository.JobRun` is still alive, or has been
abandoned and should be superseded by a fresh one."""

from __future__ import annotations

from datetime import datetime, timedelta

from curator.jobs.repository import JobRun

STALE_RUN_THRESHOLD = timedelta(hours=24)


def abandoned_run_reason(run: JobRun, now: datetime, *, noun: str) -> str | None:
    """Why ``run`` should be superseded by a fresh one, or ``None`` if it is still alive.

    :param run: A non-terminal run found by a duplicate-run guard.
    :param now: The current time, timezone-aware.
    :param noun: What to call the run in the user-visible reason (``"refresh"``, ``"enrichment run"``).
    :returns: A user-visible reason to record on the superseded run, or ``None`` to reuse it.
    """
    if run.status == "running":
        if run.lease_expires_at is not None and run.lease_expires_at > now:
            return None
        return f"This {noun} stopped before it finished, so a new one has been started in its place."
    if now - run.updated_at < STALE_RUN_THRESHOLD:
        return None
    return f"This {noun} made no progress for over 24 hours, so a new one has been started in its place."
