# Separate paced transport diagnostic proposal

Not deployed or executed live. New files only; existing capture/observability files
are untouched. Manual `wayback-transport-paced.yml` shares the capture workflow's
concurrency group and has a 15-minute job timeout. No schedules, state-cache
restores/saves, credentials, POSTs or automatic retries.

Six Archive GETs: system status, exact current-day warranty CDX, known exact terms
replay; repeat that order once. Each subsequent Archive call waits at least120
seconds after the prior call finishes (successful sequence takes at least10
minutes). Uses the existing User-Agent, default proxy/TLS, fixed endpoint allowlist,
no redirects,10-second socket timeout and bounded4KiB body read.

Any transport/open/read/close failure or HTTP403/429 or any explicit Retry-After stops ALL remaining Archive
requests. Retry-After is recorded and honored by not contacting Archive again in
this run, even if longer than the job timeout. One storefront-refund GET control
runs after the Archive sequence or early stop, then finishes. No DNS probes occur
between Archive calls; initial host DNS metadata remains bounded/read-only.

Report preserves fixed endpoint labels, start offsets, elapsed time, HTTP status,
safe exception classes/errno/function names and proxy-presence booleans. No raw
body, message, header, credential or environment values. Partial report is saved
after each Archive observation. Healthy diagnostics do not establish capture
coverage or prove that pacing repairs connectivity.
