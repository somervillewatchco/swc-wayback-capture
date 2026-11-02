# Somerville Watch Co. daily archive

Scheduled at 08:07 and 09:43 UTC. GitHub may delay schedules. The retry uses independently verified UTC-day coverage, not a 20-hour availability response.

A page counts as verified only when CDX lists a current HTTP 200 HTML/revisit record for the expected URL, and its exact replay returns usable HTML at the same timestamp. Ordinary HTTP/HTTPS, www and trailing-slash variants are allowed; other redirect/canonical destinations remain unverified. This checks the HTML document, not every embedded image or interactive checkout behavior.

SPN success alone is insufficient. Empty HTTP 204, stale success, pending jobs and lookup failures never count as success. Every configured/discovered URL gets a result in `capture-report.json`, uploaded for 90 days. Any incomplete result makes the run red. The controlled-cart URL remains included conservatively and will remain unverified unless its populated-cart content can actually be demonstrated.

## Run and verify

Run offline tests: `python -m unittest -v test_capture`.

Read-only real verification: `python capture.py --verify-only --scope priority`.

Recover a saved job without submitting: `python capture.py --poll-only --scope pilot --state PATH_TO_SAVED_STATE`. This reads existing job status and CDX/replay evidence only; missing or uncertain job IDs never cause a POST. Use a separate recovery-state path when inspecting an older cache so current state is not overwritten.

Manual Actions runs default to read-only and the one-page warranty pilot. Set verify_only=false only after any other manual capture queue is finished. Choose priority or all for broader runs. Existing repository secrets are consumed only inside Actions; do not copy keys locally.

Production Archive requests now share the candidate120-second completion-based global gate described below; this also spaces submissions. Each dispatch has at most one logical submission per URL; a pilot never resubmits after an HTTP/host refusal. The narrowly proven TCP-not-sent case below permits one physical connection retry, not a second potentially accepted capture. Host-paused/429 responses cause a persisted host-wide cooldown of at least 11 minutes (or longer Retry-After). A POST with an uncertain outcome is preserved and not automatically duplicated. Pending job IDs are cached across runs and rechecked. Success responses are resolved with independent archive evidence: proven prior-day reuse becomes terminal `reused` under the next-day policy below; missing/malformed timestamps, mismatched URLs and otherwise unknown outcomes remain `review_required`. Job ID, submission time, safe request/response diagnostics and parsed Retry-After are retained; none triggers an automatic duplicate submission in the same run. A throttled status lookup retains the accepted job and its cooldown rather than treating it as a refused submission. Arbitrary response bodies, headers, and unknown returned URLs are not retained. Cache loss/eviction is possible; review unresolved records before manually restarting. Failed status/index checks remain visible rather than being described as successful.

An accepted job can take longer than this run's polling window. Pending indexing is rechecked on the next run without a new POST. If an uncertain submission has no job ID and never appears in CDX, it requires review of the saved state before retrying; do not automatically clear it. CDX errors mean unknown, not absence. Historic index gaps cannot be repaired by captures made today.

## Deployment / rollback

Original baseline: `5cf5239c44cbd3018a3cd165f5ed91d3eac48201`.

Deploy script and tests before replacing the workflow. Then run a read-only pilot and, once other queues are stopped, a one-page live pilot. Inspect both the artifact and independent CDX/replay evidence before claiming capture success. A green test suite alone is not end-to-end verification.

Rollback: revert the workflow-replacement commit. This restores the previous workflow and its known reporting defects, so use only to recover an operational regression; retain audit evidence and pending-state records.

For reviewed recovery of an older saved job, set manual `recovery_cache_key` to its full known cache key. This forces `--poll-only` regardless of verify_only and never submits a capture. Review the recovered diagnostics before using a normal live run.

## Completed reused captures and recovery

Daily UTC coverage and a newly made capture are distinct. A same-day capture verified by CDX and exact usable replay satisfies daily coverage even when SPN reuses it; the report does not label that as a new capture. A prior-day success is classified as terminal `reused` only after its exact timestamp also passes CDX/replay verification. Its job and evidence stay in state. A migration/classification run itself never POSTs—even if the next-day boundary has already passed, the following normal scheduled opportunity is required. No additional POST is made that run; the next regular submission opportunity is the UTC day after the original attempt, or a later server Retry-After. This next-day rule is our one-attempt-per-day policy for confirmed reuse, not a claimed Archive.org cooldown. The prior terminal record is retained in a bounded audit trail when a later normal run becomes eligible.

Existing saved matching success metadata is migrated using read-only archive evidence, so expired status retention does not prevent resolving a proven completed job. Missing/malformed responses, wrong URLs, unverified historical captures and expired unknown jobs remain `review_required`, are never automatically resubmitted, and continue receiving daily evidence checks. Read-only/poll-only runs cannot release a terminal record for a new POST.

Recovery restores the explicitly requested cache only, without prefix fallback, and saves under `wayback-recovery-v1-*`, never under the production `wayback-state-v1-*` prefix. Thus inspecting an older snapshot cannot promote it into production state. Cache eviction can still lose history; inspect evidence before manually repeating uncertain submissions.

## Supplemental published-site inventory

`--scope supplemental` reads the public same-origin sitemap index and its nested indexes, then selects published pages, named collections, blog indexes/posts and any other sitemap URLs **outside** the existing priority + catalog/product (`all`) inventory. It does not revisit the existing all-scope target set simply because some targets failed. Both inventories must finish before any capture is attempted. This is bounded discovery, not an arbitrary website crawl: at most 32 sitemap documents, nesting depth 4, and 5,000 target URLs; DTD/entities and unexpected XML are rejected. Only direct standard sitemap URL locations are targets; image/video namespace locations and external origins are excluded.

Use the Actions `supplemental` scope only after the current all-scope run finishes. Workflow concurrency and existing state/cooldowns still apply. This scope covers published sitemap records, not password-protected content, arbitrary query variants, or every image. A successful empty supplemental inventory reports `no-additional-targets` rather than pretending that a page was captured.

The November 2 supplemental inventory contains 22 records: 21 HTML-page candidates plus `/agents.md`, observed as HTTP 200 `text/markdown`. The exact `/agents.md` entry remains in the result inventory as `unsupported-document-type`, with **no capture submission**. The HTML verifier is not weakened to accept it, and no arbitrary filename-extension exclusion is applied. That explicit unsupported record keeps the overall supplemental run incomplete/red even if all 21 HTML candidates verify; it must not be described as complete whole-site archival coverage.

## Public GET-only transport diagnostics

Use the manual Actions `diagnostic=true` input (default false), or run `python diagnose_transport.py` in a comparable environment. Diagnostic mode skips capture execution, production/recovery cache restore and save, and the capture-evidence upload; it receives no Archive credentials. It uploads only `transport-report.json` as `transport-evidence-*`. Scheduled capture times are unchanged.

The script makes two paced passes, 30 seconds apart, over four fixed public GET endpoints: Archive system status, current-UTC-day warranty CDX, the known terms replay `20261102015923`, and the storefront refund page. HTTP timeout is 10 seconds; DNS observation timeout is 5 seconds; requests are 3 seconds apart, redirects are not followed, and at most 4 KiB is read without logging response content. A host's HTTP 429 suppresses remaining probes until its parsed Retry-After (at least 60 seconds). The workflow bounds the diagnostic step to five minutes.

Evidence contains HTTP status, allowlisted content type, elapsed time, open/read phase, exception and nested reason class/errno, and traceback **function names only**—not raw exception messages, headers, body, filesystem paths, environment values or secrets. It records Python/OpenSSL versions, proxy-variable **presence only**, and DNS address-family/count (no address values). Normal urllib proxy/TLS behavior stays intact; no IP pinning, direct TCP workaround, proxy bypass or TLS weakening is used. DNS observations describe the local resolver and do not prove how a configured proxy resolves the destination. Compare the same script's cloud and GitHub artifacts to distinguish local/proxy resolution, connection establishment, HTTP refusal and body-read failures. A completed diagnostic run is not evidence of capture success.

## Bounded transport resilience and circuit breaker

A POST may receive **one** retry only when instrumented `HTTPSConnection._create_connection` raises before it returns any connected TCP socket. That proves this new attempt could not have transmitted target HTTP bytes. The delay is at least 60 seconds, respects a longer stored submission cooldown, and requires enough remaining run budget. HTTP responses (including redirects/429), established-TCP proxy CONNECT/TLS failures, send/partial-send failures, read timeouts and all unclassified errors are **not** eligible for POST retry. The second attempt gets a fresh tracker; uncertainty on it overrides the first attempt's safe failure. Attempt numbers/timestamps are persisted before each physical attempt; diagnostics never include credentials or exception messages. Existing TLS/proxy behavior remains unchanged and authenticated requests are restricted to the HTTPS SPN endpoints without redirects.

Only newly created attempts with this direct proof may receive `known_not_sent` state and retry at a later scheduled opportunity after cooldown. Historical no-job records—including the 11 uncertain records from the interrupted full run—are never inferred safe or automatically cleared. Ordinary daily archive evidence can still resolve them.

After three consecutive **URL operations** containing an exhausted Archive transport failure, the run opens a circuit breaker and checkpoints the untouched queue for bounded pause/health-check/resume (below). Only after recovery/budget exhaustion are remaining URLs reported deferred without further requests. Internal GET retry attempts and the one safe POST retry do not multiply the count. An intermediate healthy CDX response does not erase a later POST/status failure for that URL. The streak resets only after a completed URL operation has a healthy Archive HTTP response and no exhausted transport failure; storefront responses cannot reset it. A 15-minute local-policy cooldown is persisted (not represented as an Archive.org-imposed window), preserving any longer submission cooldown. Subsequent capture runs respect it; the separate public diagnostic mode can establish transport health without touching capture state. This limits load and failure propagation; it does not claim to repair Archive.org connectivity.

### Resuming a transport outage

Three consecutive URL operations with exhausted Archive transport failures pause
submissions rather than abandon the queue. Progress (inventory, cursor and actual
per-URL outcomes) is atomically checkpointed by scope, initial UTC day and mode.
The run waits a local-policy 15 minutes, then requires two healthy public GET
passes (SPN system status and exact warranty CDX, 30 seconds apart) before
resuming **untouched URLs only**, with normal submission spacing. These probes
show connectivity, not successful captures. Failed probes extend the local pause
and honor longer Retry-After values; at most two recovery cycles run within the
remaining time budget. No proxy bypass, TLS changes or authenticated health calls.

When recovery/budget is exhausted, untouched suffix entries are reported deferred,
not consumed or marked verified. A later same-day run resumes that suffix; a
completed checkpoint is rebuilt on a later invocation, and next-day queues start
afresh for the daily goal. An interrupted in-flight operation is reported
unverified and skipped on resume, preserving its pending state. Existing unknown
submissions (including the 11 historical jobs) remain protected from blind POSTs.
Queue completion does **not** imply coverage: prior unverified/unsupported results
remain in the report and keep the exit status incomplete. Modes/scopes have separate
checkpoints, so a probe/recovery run cannot overwrite an all-scope cursor.

`--scope transport_probe` selects only the fixed, previously untouched product
`https://somervillewatchco.com/products/date-mod-ice-blue-arabic-watch` for a scoped
live transport test. It does not change the warranty pilot or scheduled scopes.

### Status errors can conflict with a real capture

An accepted job's error status (including recognized `error:bad-request`) is not
proof that no capture exists. The runner preserves the job and safe response
metadata, then checks current-day CDX and exact usable replay. Verified evidence
wins, with `status_conflict: true`; `new_capture` is true only when its timestamp
is at least the stored submission time minus five seconds. If evidence is absent
or unavailable, the job remains protected for review without automatic status
polling or resubmission. Later runs still check daily evidence to reconcile an
indexing delay, while continuing unrelated URLs.

All removal paths snapshot allowlisted diagnostics before clearing pending state:
verified captures, explicit no-job submission refusals, host-paused refusals,
proven-not-sent releases, and reviewed prior-day reuse releases. A bounded
50-entry completed audit retains these transitions. A paused response containing
an accepted job ID retains that job instead. Arbitrary raw messages, response URLs,
headers and secrets are never stored. This improves future evidence; it cannot
recover a response or job ID discarded by an older deployed version.

### Recovery observability and offline state inspection

`operational-report.json` accompanies (does not replace) the per-URL capture
report in the existing evidence artifact. It records bounded, sanitized recovery
history: cycle/pass, fixed endpoint label, HTTP status or exception class and
transport phase, health decision, checked time and cooldown. It contains no raw
body/message/header, credentials, job IDs or target URLs. Queue summaries expose
scope/day/mode, cursor, total, remaining, in-flight flag and processed outcome
counts; these are not coverage assertions and do not inflate capture counts.
At most 100 recovery events are cached and exported; earlier runs without this
instrumentation cannot retrospectively supply missing probe reasons.

`python capture.py --inspect-state --state path/to/pending.json` emits that
summary without instantiating a network client, submitting/polling, modifying
saved state or reading credentials. It writes only `operational-report.json`.
Offline workflow tests run in a temporary working directory so test-generated
reports cannot be mistaken for real-run evidence.

### Candidate global Archive pacing (not a demonstrated connectivity fix)

Every production Archive request passes through one persisted gate: CDX, exact
replay, status, submission, health checks and existing bounded retries. The next
request starts at least120seconds after the previous attempt **finishes**, even
when it fails. Longer server Retry-After and restored host/transport cooldowns
win. Retry-After is captured before reading a body, so a body timeout cannot erase
an already-received server cooldown. Archive redirects are not followed: they
would otherwise create hidden requests outside this gate. Storefront discovery
is not subject to the Archive gate. Standalone offline Client unit fixtures are
ungated; the production Runner always binds the persisted120-second gate.

`_archive_next_request_at` is checkpointed before IO and after completion/error,
and exported in operational-report.json. A restarted scope restores it rather
than bursting immediately. The pre-IO reservation protects interrupted attempts;
existing pending/unknown jobs remain protected. No additional retries were added.
The narrow proven TCP-not-sent retry still runs at most once and honors this gate.
If its retry cannot fit the budget, the original proven-not-sent state survives;
it does not turn into a permanently unknown submission.

Budget decisions happen before sleeping/IO. A POST requires at least500seconds
of remaining budget after its gate opens (POST plus spaced status/CDX/replay
reserve); this reserves a useful verification opportunity, not a guarantee of
completion. Four health requests reserve620seconds including useful follow-up
room. If the budget is inadequate, untouched queue work remains checkpointed;
a submission that already happened stays pending. HTTP/socket timeouts remain
per-operation estimates rather than hard whole-operation deadlines.

`--scope pacing_probe` selects only the previously untouched
`https://somervillewatchco.com/products/date-mod-olive-green-watch`, with a maximum
30-minute budget. It does not redefine the earlier Ice Blue transport probe, and
an unexpected saved pending job is still protected from another POST. Existing
cooldowns may consume the canary budget; that is an incomplete/deferred result,
not permission to skip the cooldown.

This candidate drastically limits throughput. Even already-archived194-page
coverage requiring CDX+replay would need roughly13hours at this floor; new capture
verification typically needs more requests. The current290-minute budget cannot
establish complete daily194-page coverage at that rate. Queue resumption prevents
abandonment, but does not make the daily goal feasible or justify reporting success.
Earlier120-second diagnostics also failed, so pacing is not yet proven to fix the
intermittent network problem.

### One saved-job diagnostic without submission or verification

Manual scope `pacing_job_diagnostic` selects only the saved Olive Green pacing
probe job. Regardless of the verify checkbox, it performs at most one authenticated
status GET through the global gate (no automatic retry), then stops. A missing or
invalid saved job ID causes zero requests. It never enumerates inventory, performs
CDX/replay checks, submits, clears pending/review state, or processes queue cursors.
It respects saved cooldowns and has a maximum30-minute budget. A follow-up archive
verification must be a separate, properly paced operation.

The observation preserves safe status/error categories, an explicitly reported
`HTTP status=NNN`/`HTTP status: NNN` number, exception-presence/category, job/URL
match booleans, valid timestamp/freshness and resource count—not raw server text,
returned URLs, resource details, headers or credentials. A message-reported HTTP
number is **not** an independently observed origin status. Transport failures retain
only sanitized phase/class/errno. An observed status is not capture coverage.

Freshness/new-capture classification now uses the last valid physical attempt's
`request_evidence.attempts[-1].started_at`; legacy records fall back to submitted_at.
A capture that predates the paced actual submission may establish existing daily
coverage, but is not labeled a new capture attributable to that attempt. Timing
alone still does not establish that this particular job caused the capture.
