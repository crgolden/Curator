"""Maps a caught background-job exception to short, safe, user-facing text.

The mapped string is what actually reaches ``job_runs.error`` (persisted), the browser
(``GET /library/refresh/{run_id}``), and Elasticsearch's dead-letter/failure log line via
:mod:`curator.jobs.queue_consumer` -- never the raw exception's own message. This is the second half of
closing the RAWG-key-in-URL leak class of bug (see ``curator.enrichment.rawg_client.RawgApiError``'s
``from None`` chain-suppression, which sanitizes the exception's own message at the source): a friendly,
category-based message here means even a *future* exception type nobody thought to sanitize can't leak
anything sensitive through this path, since its raw text is never used at all.
"""

from __future__ import annotations

from curator.enrichment.enrichment_service import EnrichmentAuthError
from curator.enrichment.opencritic_client import OpenCriticApiError
from curator.enrichment.rawg_client import RawgApiError
from curator.psn.errors import PsnAuthError

_RATE_LIMIT_STATUS_CODE = 429
_GENERIC_MESSAGE = "The job failed unexpectedly. If this keeps happening, contact support."


def friendly_job_error(exc: Exception) -> str:
    """Map ``exc`` to short, safe, user-facing text -- never the raw exception string.

    :param exc: The exception a job's processing raised.
    :returns: Friendly text, safe to persist/log/display.
    """
    if isinstance(exc, EnrichmentAuthError):
        return f"Your {exc.provider.upper()} API key was rejected. Check that it's correct and try again."
    if isinstance(exc, RawgApiError | OpenCriticApiError) and exc.status_code == _RATE_LIMIT_STATUS_CODE:
        return "Enrichment provider rate limit reached. Try again later."
    if isinstance(exc, PsnAuthError):
        return "Your PlayStation Network link has expired or was rejected. Re-link your account and try again."
    return _GENERIC_MESSAGE
