# Daily archival repair

Prepared November 4, 2026, against main
`37c4361c2bf123201095b4fc84e481a4842864c0`.
Scope: all 12 essential pages and all discovered products on every UTC day.

## Problem and resulting behavior

Scheduled run #48 restored #47's state, then stopped on three more paused
HTTP-200 error payloads for the same accepted contact job. Coverage stayed at
3/12 essential pages and 0/154 products. The current per-URL fresh-capture path
also needs at least five Archive requests per target: 830 for 166 targets,
which cannot fit a day at the protected 120-second request spacing.

This repair batches independent verification around capture/status operations,
retains repeated paused job errors for review, and provides enough scheduled
runtime for the measured healthy-provider workload. It does not establish that
the external crawling restriction is resolved, or claim successful production
coverage from offline tests.

## Capture and verification

Daily all/priority workflows use `--daily-batches` and process batches of at
most eight URLs. The persisted queue records the batch bounds, cursor and
phase: initial shared index/replay verification, capture/status processing,
then final shared verification. Each stage checkpoints before advancing.

The shared index uses a bounded, same-day host query. It validates the entire
response before emitting any evidence. Truncation, continuation markers, row
limits, malformed data and failed reads stop the batch. The query uses the
[Internet Archive CDX API](https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md),
requests a continuation marker and conservatively refuses its 5,000-row cap.
Unknown URLs are never replayed or returned. Missing index evidence is not
proof of non-acceptance or permission to repeat an uncertain submission.

Each covered URL still needs an exact-original same-day CDX record and its
individual exact-timestamp usable HTML replay. Product identity, title, heading
and positive price checks remain. A provider success alone is not coverage.

On restart, negative index evidence is refreshed before further capture work.
Positive same-day evidence survives restarts and completed passes; new targets
join unfinished work. Old queues and request journals remain history. A crash
after uncertain acceptance does not cause another POST. The next UTC day uses
a separate queue and cannot count the previous day's captures as today's.

Essential pages are handled before products on each invocation. Any outlink
hold stops further daily processing and reports the exact URL/reason. Daily
modes do not dispatch outlink batches. The legacy per-URL path remains available
by omitting `--daily-batches`; the workflow enables batching only for all/priority.

## Repeated paused job errors

Three paused status observations for an accepted job, each with actual HTTP 200
and `status=error`, put that job into the existing review-required state. The
third response still invokes the same global host pause and three-response
breaker. The current run stops. On later runs the job ID/history remain, no new
submission or status polling is allowed for it, and independent archive evidence
continues to be checked. Other URLs may proceed after the global cooldown.

This is conservative quarantine, not a claim that the job is terminal or was
never accepted. Actual endpoint HTTP 429 responses do not qualify for this
transition. A nonqualifying status observation resets the job-error counter.
Legacy records without the counter are not guessed or retroactively cleared;
they must accumulate the three qualifying observations under the new code.
An unresolved reviewed URL remains missing in the coverage report.

Provider diagnostics retain allowlisted meanings and reported status codes
separately from the actual API status. Raw provider messages and arbitrary
unknown URLs/values are not retained.

## Runtime and reports

The existing serialized workflow gets four scheduled opportunities:
00:07, 06:07, 12:07 and 18:07 UTC. Each daily invocation has a 330-minute capture
budget within the unchanged 340-minute job timeout: 22 hours of potential
capture runtime daily, replacing the prior two 290-minute windows. No parallel
host requests, extra monitor, manual dispatch or credential change is required.

Every Archive request retains the completion-based 120-second floor, at least
20 minutes after host pause/429/overload, and any longer Retry-After. The
three-response pause breaker, transport safeguards, one product attempt per
UTC day, before-IO attempt journal, accepted/uncertain-job protections, state
continuity checks and UTC rollover guard remain. Cache namespaces, isolation
of recovery runs, and evidence publication are unchanged.

All-scope batched runs fail if any essential page or product is missing, or
catalog discovery is incomplete. The two exact missing-coverage summary lines
remain. Priority-only mode reports products as unassessed. Queue completion is
an execution checkpoint, not a coverage claim.

## Validation and limits

The full offline suite passed **358 tests in 58.900 seconds on Python 3.9.6**.
The test harness prohibits real socket/DNS access. The workflow YAML parsed
successfully and all three shell blocks passed `bash -n`; `git diff --check`
passed. CI uses Python 3.12, unavailable locally; scheduled jobs retain their
mandatory offline test step before capture.

The real orchestration, parsers and replay validators were exercised with a
synthetic provider, two seconds per request, the actual 120-second global gate,
and four independent 330-minute invocations starting at the proposed times:

| Measurement | Result |
| --- | ---: |
| Fresh targets independently verified | 166/166 |
| Physical POST attempts | 166, one per URL |
| All Archive requests, including restart index refreshes | 545 |
| First window elapsed | 19,278 seconds |
| Second window elapsed | 19,766 seconds |
| Third window elapsed | 19,766 seconds |
| Fourth window elapsed | 7,200 seconds |
| Completion in simulated UTC day | 20:07 UTC |

The separate one-snapshot verification component uses 167 requests for 166
already-indexed targets. The integrated eight-URL pipeline deliberately uses
more index reads to bound work and resume safely.

Additional tests cover incomplete index responses, uncertain acceptance after
interruption, partially verified batches, index lag, inventory additions, old
queue evidence, outlink holds, actual HTTP 429, repeated payload errors and
review retention, verify-only/poll-only modes, midnight, corrupt checkpoints,
and failure reporting when only products are missing.

These measurements demonstrate healthy-case capacity, not a guarantee during
provider refusal, long network latency, index lag or delayed GitHub starts.
Provider-caused misses must remain visible; quarantine is not proof that the
contact page has been archived. Production validation remains a separate
scheduled-run result owned by the existing observer, with this chat reconciling
that evidence. No test result is a production capture.

## Rollout and rollback

Publish the code, tests, workflow and this record together. Existing active
runs keep their selected commit; do not cancel, duplicate or restart them.
No manual validation dispatch is part of this release. Keep the existing
observer rather than creating another monitor.

Rollback means restoring the previous code/workflow in a normal reviewed
commit. Do not delete or overwrite runtime state: old queues and new daily-v3
queues coexist, and the legacy runner tolerates the additional retained
diagnostic fields. Review-required jobs must remain protected even after a
rollback. A live recovery action needs its own authorization and evidence.
