# Local read-only client comparison proposal

Not published or run live. Uses existing `diagnose_transport.py` helpers; no change
to capture.py, caches, credentials, pending jobs or existing workflows. New manual
workflow shares `wayback-snapshot` concurrency,25-minute timeout. Pinned to `ubuntu-24.04` for the same-environment comparison following the
unchanged paced diagnostic; production capture workflow is unchanged.

At most8 public GETs on2 fixed endpoints: Archive system status and current-day
exact warranty CDX. Each endpoint is paired Python→curl in round1 and curl→Python
in round2. At least120seconds after the previous response completes; no retries.
Any403/429 or any explicit Retry-After stops all calls immediately. A transport
failure permits only the already-planned other client for that same endpoint,
after120seconds, then ends the sequence. If the failing request was second in its
pair, no further calls are made. This is controlled diagnosis, not bypass or a
production fallback.

Identical User-Agent, Accept */*, Accept-Encoding identity. Neither client follows
redirects or receives credentials. Curl -q disables implicit user config; native
proxy/TLS and HTTP negotiation preserved. Curl stderr and temporary response
headers never enter the report; only status, exitcode, HTTPversion, TLS verification
result, peer IP **family**, timings and parsed Retry-After. Python additionally
records safe exception classes/errno and negotiated TLSversion when exposed.
Curl negotiated TLSversion is explicitly unavailable without unsafe verbose output;
backend/version are recorded instead. A proxy peer is not necessarily the origin.

Confounds remain explicit: shared UA differs from historical curl default;
HTTPnegotiation may differ; Python has socket timeouts versus curl's total timeout;
Python reads first4KiB versus curl discarding the complete body. Thus outcomes can
identify a transport-specific symptom, not prove historical UA or protocol caused
it, nor establish archive capture coverage.

History supplied by parent: Oct23 succeeded on Ubuntu24.04 image20261012.334.1;
Oct29 also succeeded on Ubuntu26.04 image20261025.179.1, the SAME image as Oct30's
first failure. Therefore image migration is NOT an established root cause. Parent
is first testing only the existing unchanged paced diagnostic on24.04; this client
comparison remains a later proposal, not approval for publication or live calls.

Timing/order is an additional confound: success then failure120seconds later may
reflect time-varying connectivity, not client behavior. The conservative stop rule
may prevent the reversed-order round; an incomplete comparison cannot resolve
that confound or establish that curl repairs connectivity.
