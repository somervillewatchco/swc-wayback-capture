# Temporary scheduler heartbeat diagnostic

This independent workflow has one intended scheduled opportunity:
**November2,2026 at17:07UTC**, cron `7 17 2 11 *`. Publish before that time; do not
interpret a missed pre-deployment opportunity as a scheduler failure. GitHub cron
has no year field, so the shell explicitly limits diagnostic work to Nov2,2026
through Nov3,2026 at01:30UTC. Later annual invocations only print an expired/no-op
line and exit. The workflow must still be removed as described below.

The sole step runs local `date` and shell `printf`. No checkout, actions, credentials,
secrets, cache, artifacts, HTTP calls, Archive requests, capture state or production
workflow changes. `permissions: {}` grants no token permissions. It deliberately
has no Wayback capture concurrency group, so a running capture cannot explain an
otherwise delayed heartbeat. GitHub's own workflow/runner infrastructure remains
outside the diagnostic's control; “network-free” describes the authored step.

`workflow_dispatch` exists only as an optional, explicitly chosen registration /
syntax check. It is **not automatically dispatched**. A successful manual event
cannot demonstrate that scheduling works. Only an event marked `schedule` answers
whether this scheduled opportunity reached execution. The log prints UTC and event.

## Observation and removal

Observe the scheduled opportunity through **Nov3,2026 at01:30UTC**—8hours23minutes
after the nominal time—to accommodate previously seen6–7hour delays. Keep it until
that fixed deadline or until the first scheduled event is observed, whichever
occurs first; then remove the temporary workflow. Do not remove it early merely
because there is no event yet. A manual run does not satisfy the scheduled-event
stopping condition. Removal is a separate parent-controlled repository action.

Evidence may distinguish a scheduled invocation from missing/delayed scheduling
or a workflow-specific problem, but cannot prove why GitHub delayed an event.
Runner queuing also affects start time. No event by the deadline is a bounded
negative observation, not proof scheduling is permanently broken. This neither
repairs GitHub scheduling nor establishes capture coverage.
