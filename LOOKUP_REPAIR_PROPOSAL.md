# Verification lookup repair

Base: main 446fd3ff50b38373e9bd6ec67be362e9aacd921f. This proposal changes capture.py, capture_schedule.py and test_capture.py. The workflow YAML is byte-for-byte unchanged.

Runs #51 (Nov 6) and #53 (Nov 7) stopped on an initial shared index/replay LookupFailure before new submissions; neither verified any of the nine selected pages. The current handler discarded the underlying reason. The exact external cause remains unknown.

Retry #52 arrived Nov 6 at 7:48:19 p.m. EST (Nov 7 00:48:19 UTC), after the previous coverage day ended. Its gate searched the new UTC date, found no primary, skipped all capture/cache/report steps, and returned success. This was not archival success.

## Proposed behavior

- Record allowlisted lookup reasons, index versus replay phase, HTTP status when observed, and transport phase/class/errno. Never write raw exception messages, bodies, credentials, or arbitrary response headers.
- Retain the existing two-attempt GET handling. For a recognized temporary lookup failure, allow up to two additional verification rounds per invocation, each at least 20 minutes later. Longer Retry-After and persisted gates prevail. No retry crosses the UTC-day budget or bypasses a pause/transport breaker. Invalid schemas and unknown failures remain blocked.
- Treat replay HTTP 429/502/503/504 as unavailable verification rather than evidence that permits another save. A regression reproduced a duplicate save attempt in the old implementation when an already-indexed capture replay returned 503.
- Report a missed continuation after a previous-day failed primary as a failed workflow, with run_capture=false. Do not dispatch a backdated or new-day capture. A later successful primary supersedes an older failure.

## Scope and limits

Keep the nine policy/cart targets, product pause, no-brand exclusion, one-hour save spacing, independent CDX/replay requirements, retained jobs, outlink holds, rate limits, daily cutoff, existing schedules and Dot observation ownership.

These changes improve diagnostics, bounded recovery and reporting. They do not establish the root HTTP/transport cause of #51/#53, remove Shopify/Wayback restrictions, or repair GitHub dispatch delays. At one-hour spacing, nine fresh saves need at least eight hours; delayed starts can make daily coverage impossible before the 7 p.m. EST cutoff. Changing that requirement, schedule service, or window needs a separate decision.

## Verification

Five targeted regression cases fail against the exact live baseline for the expected missing behavior: recovery after transient index failure, bounded transport recovery, missing diagnostics, replay-503 duplicate attempt, and false green missed retry. All pass on the proposal. The final full suite passed 376 tests in 55.417 seconds on Python 3.9.6. Patch applicability against the live baseline and git diff --check passed. Final revision evidence is recorded in wayback-archiving-handoff.md.

The user approved publication of this reviewed repair. Workflow dispatch, provider contact, extra schedules and new monitors are outside this deployment. Production coverage remains to be established.
