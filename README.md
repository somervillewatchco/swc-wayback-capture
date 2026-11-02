# Somerville Watch Co. daily archive

Scheduled at 08:07 and 09:43 UTC. GitHub may delay schedules. The retry uses independently verified UTC-day coverage, not a 20-hour availability response.

A page counts as verified only when CDX lists a current HTTP 200 HTML/revisit record for the expected URL, and its exact replay returns usable HTML at the same timestamp. Ordinary HTTP/HTTPS, www and trailing-slash variants are allowed; other redirect/canonical destinations remain unverified. This checks the HTML document, not every embedded image or interactive checkout behavior.

SPN success alone is insufficient. Empty HTTP 204, stale success, pending jobs and lookup failures never count as success. Every configured/discovered URL gets a result in `capture-report.json`, uploaded for 90 days. Any incomplete result makes the run red. The controlled-cart URL remains included conservatively and will remain unverified unless its populated-cart content can actually be demonstrated.

## Run and verify

Run offline tests: `python -m unittest -v test_capture`.

Read-only real verification: `python capture.py --verify-only --scope priority`.

Recover a saved job without submitting: `python capture.py --poll-only --scope pilot --state PATH_TO_SAVED_STATE`. This reads existing job status and CDX/replay evidence only; missing or uncertain job IDs never cause a POST. Use a separate recovery-state path when inspecting an older cache so current state is not overwritten.

Manual Actions runs default to read-only and the one-page warranty pilot. Set verify_only=false only after any other manual capture queue is finished. Choose priority or all for broader runs. Existing repository secrets are consumed only inside Actions; do not copy keys locally.

Submissions are at least 60 seconds apart. Host-paused/429 responses cause a persisted host-wide cooldown of at least 11 minutes (or longer Retry-After). A POST with an uncertain outcome is preserved and not automatically duplicated. Pending job IDs are cached across runs and rechecked. A success response with stale/missing timestamp or mismatched URL remains pending for review: its job ID, submission time, and allowlisted status diagnostics are retained in the state and report; it never triggers an automatic duplicate submission. Arbitrary response bodies, headers, and unknown returned URLs are not retained. Cache loss/eviction is possible; review unresolved records before manually restarting. Failed status/index checks remain visible rather than being described as successful.

An accepted job can take longer than this run's polling window. Pending indexing is rechecked on the next run without a new POST. If an uncertain submission has no job ID and never appears in CDX, it requires review of the saved state before retrying; do not automatically clear it. CDX errors mean unknown, not absence. Historic index gaps cannot be repaired by captures made today.

## Deployment / rollback

Original baseline: `5cf5239c44cbd3018a3cd165f5ed91d3eac48201`.

Deploy script and tests before replacing the workflow. Then run a read-only pilot and, once other queues are stopped, a one-page live pilot. Inspect both the artifact and independent CDX/replay evidence before claiming capture success. A green test suite alone is not end-to-end verification.

Rollback: revert the workflow-replacement commit. This restores the previous workflow and its known reporting defects, so use only to recover an operational regression; retain audit evidence and pending-state records.

For reviewed recovery of an older saved job, set manual `recovery_cache_key` to its full known cache key. This forces `--poll-only` regardless of verify_only and never submits a capture. Review the recovered diagnostics before using a normal live run.
