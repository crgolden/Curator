-- Curator schema — migration 0022 (job_runs checkpoint sequence)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds job_runs.seq: a monotonic checkpoint counter, bumped by JobRunsRepository.mark_rate_limited on
-- every real checkpoint a library-refresh run reaches, and stamped into the resulting continuation
-- message's payload at publish time (QueuePublisher.publish_library_refresh_continuation's seq
-- parameter). QueueConsumer._handle() compares a delivered message's seq against the row's current seq
-- via a new compare-and-swap, JobRunsRepository.try_begin_delivery, before doing any work.
--
-- This closes a redelivery race identified from production Elasticsearch logs: a library-refresh (or
-- continuation) message that takes long enough to process can have its Service Bus lock expire before
-- QueueConsumer._handle()'s except RateLimitRetryScheduled branch reaches its
-- receiver.complete_message(message) call, which then raises MessageLockLostError. Because the message
-- was never actually settled at the broker, Service Bus redelivers it -- to the same, only,
-- F1-single-instance consumer -- which used to unconditionally stomp the run back to running (the old
-- JobRunsRepository.mark_running, called with no prior-status guard) and reprocess the entire original
-- batch from scratch, hit the same rate limit again, and republish another duplicate continuation
-- message before failing to settle again -- repeating until maxDeliveryCount was exhausted and the
-- message dead-lettered, permanently freezing the run.
--
-- seq is what lets a stale redelivery be told apart from a legitimate one: a message whose payload seq
-- no longer matches job_runs.seq is a redelivered copy of a message whose work has already been
-- superseded by a later checkpoint (or a run that has already reached a terminal status) -- it is
-- settled without reprocessing rather than restarting the batch. A message whose original send predates
-- this migration (or the un-rescheduled first send of any run) simply carries no seq field at all;
-- QueueConsumer._handle() treats that as 0, which matches this column's DEFAULT 0 for every existing and
-- newly created row.

ALTER TABLE job_runs
    ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
