"""Azure Service Bus queue publish/consume plumbing for Curator's two long-running background workflows:
a user's library refresh, and the global enrichment catalog re-scrape.

Publishing instead of processing inline means the routes that trigger these workflows return immediately
with a run id; the actual work -- which can involve many uncached RAWG/OpenCritic/PSN calls, bound by
those services' own rate limits -- happens on :mod:`curator.jobs.queue_consumer`'s own schedule instead of
blocking the request. Mirrors the Directory/Functions queue-cascade pattern already used elsewhere in the
workspace.
"""

from __future__ import annotations

LIBRARY_REFRESH_QUEUE = "curator-library-refresh"
ENRICHMENT_QUEUE = "curator-enrichment"
