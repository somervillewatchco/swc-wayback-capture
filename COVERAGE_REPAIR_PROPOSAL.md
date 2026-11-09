# Ten-minute policy/cart coverage repair

Prepared November 8 EST / November 9 UTC. The user requested ten-minute pacing as part of the reviewed cart/completed-job edit.
Base: main `3c6dd489160e5b60bdb21cfff82aa83c38053419`.

## What this changes

Run #56 reached eight policy targets, then a shared archive-index connection failure prevented it from reaching cart. The policy/cart scope has nine targets but used the generic eight-target batch limit. This repair puts all nine in one batch. An initial complete index check remains mandatory; each page retains its own saved-job checks, and verified coverage still requires an exact index entry plus a usable archived page. Other scopes keep eight-target batches. Time budgets and cooldowns can still defer cart.

The terms page also consumed a run reconciling a previously saved successful job to an older capture. When exact index and replay evidence establishes that an old job is complete and its existing retry-eligibility date has already passed, this repair continues through the existing retirement/audit path in the same invocation. It does not release pending or uncertain jobs merely because they are old. Read-only modes, server Retry-After and all submission gates still apply.

At the user's request, reduce the policy/cart minimum interval between physical save attempts from 3,600 to **600 seconds (10 minutes)**. The global 120-second Archive request gate, longer provider Retry-After values, saved future cooldowns, pending-job protection and stop circuits still take precedence. Existing persisted hourly cooldowns are honored rather than shortened.

Runtime changes are limited to `capture.py`; regressions are in `test_capture.py`. `capture_schedule.py` and the workflow are unchanged. No product operations, target changes, new schedules, manual dispatches or monitors are included.

## Remaining obstacles

This patch cannot promise nine verified daily captures. On November 8 the GitHub runner timed out while connecting to Archive's index; successful later reads from the user's local environment do not establish why that earlier connection failed. Saved pending/uncertain jobs still need authoritative completion or reviewed recovery evidence.

The scheduled primary began at 10:53 a.m. EST instead of 4 a.m.; the 4 p.m. retry arrived at 7:41 p.m., after the UTC-day cutoff. The previous hourly setting required at least eight hours between nine fresh saves and therefore depended on a timely continuation. Ten-minute spacing reduces that minimum to 80 minutes. The healthy-provider simulation completes all nine captures and exact verifications in **6,266 seconds (1h44m26s)**, within one 5.5-hour budget. Real indexing lag, pending jobs, connection failures and provider pauses can still extend or prevent completion. This change cannot guarantee GitHub cron delivery or reverse the already missed day.

## Next operational repair

Keep the existing protections and daily requirement. Prepare a replacement execution arrangement that can start the 4 a.m. primary and 4 p.m. failure-only continuation promptly and can reach the Archive index and replay endpoints. A reliable external trigger alone would address cron creation delays but would leave runner queue delays and the observed connection problem unresolved. A continuously available runner would need to be chosen and validated before it is a defensible replacement.

The rollout must replace the existing schedule at cutover, use a single execution lock and the same durable job journal, retain the 7 p.m. EST UTC-day cutoff, and refuse duplicate or uncertain submissions. Verify clock/timezone handling, recovery after restart, ten-minute save pacing with longer cooldowns honored, complete nine-target coverage and transport access before enabling it. Do not activate new infrastructure, credentials, expenses or production runs without the user's authorization. Dot remains the sole scheduled observer.

## Validation

The two behavior regressions fail against the exact deployed capture.py: the old implementation does not submit cart after the first eight pages when the next index read fails, and returns reused instead of continuing an already-eligible completed job. Both pass on the proposal. Additional regressions preserve read-only/cooldown behavior, unknown and pending jobs, and the eight-page limit outside policy scope. The updated capacity test verifies all nine pages in one primary, exactly nine distinct physical submissions, at least 600 seconds between saves and no product reads. Restart and persisted-cooldown tests also pass. The final full suite passed **382 tests in 55.861 seconds** on Python 3.9.6. The exact-base patch check and `git diff --check` passed. The one-primary capacity regression fails against the previous hourly release and passes on this edit. Independent capture evidence is recorded in the handoff.

## Deployment boundary

The requested combined edit covers only capture.py, test_capture.py and this explanation, published using a checked current main commit. It does not dispatch a run, create a monitor, change the 4 a.m./4 p.m. schedule or UTC-day cutoff, reset job state or activate infrastructure. Production success remains a separate acceptance condition.
