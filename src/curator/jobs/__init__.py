"""Azure Service Bus queue publishing for Curator's three long-running background workflows: a user's
library refresh, its rate-limit continuation, and the global enrichment catalog re-scrape.

Publishing instead of processing inline means the routes that trigger these workflows return immediately
with a run id; the actual work -- which can involve many uncached RAWG/OpenCritic/PSN calls, bound by
those services' own rate limits -- happens out of process, asynchronously, instead of blocking the
request. This module owns publishing only; nothing in this codebase consumes these queues.

``LIBRARY_REFRESH_CONTINUATION_QUEUE`` is a separate queue (not just another message shape on
``LIBRARY_REFRESH_QUEUE``) because its messages are always sent via Service Bus's native scheduled-message
feature -- a rate limit hit part-way through a refresh republishes the remaining games with a future
``schedule_time_utc`` so it isn't picked up again until the limit should have lifted, mirroring the same
run id. **Curator names that queue but never publishes to it**: the continuation is republished by the
worker that hit the rate limit, in the Functions repo
(``Functions/Curator/Library/LibraryRefreshQueuePublisher.cs``), which is also where the ``seq``
staleness checkpoint is stamped.
"""

from __future__ import annotations

LIBRARY_REFRESH_QUEUE = "curator-library-refresh"
LIBRARY_REFRESH_CONTINUATION_QUEUE = "curator-library-refresh-continuation"
ENRICHMENT_QUEUE = "curator-enrichment"
SCHEDULED_REFRESH_QUEUE = "curator-scheduled-refresh"
