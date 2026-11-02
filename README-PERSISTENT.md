# Public single-connection persistence feasibility diagnostic

Standalone manual experiment; production capture, pending jobs, queues and caches
are untouched. Uses the normal Python HTTPSConnection verified TLS/HTTP1.1 defaults
on ubuntu-latest. The manual workflow shares `wayback-snapshot` concurrency and has
a10-minute timeout. Do not dispatch before the already-known host cooldown expires
(15:50:56 UTC on Nov2 for the currently proposed run), or alongside other probes.

Only two fixed public GETs: Archive system status, then exact current-day warranty
CDX. There is no authentication, POST, redirects, retries, fallback connection,
keepalive header, ping or storefront control. Any proxy-variable presence (even
empty) aborts before network; no proxy routing is bypassed. The second request waits
at least120seconds after the first response is fully consumed and validated.

Standard DNS resolution must yield exactly one distinct socket address; multiple
addresses abort before TCP rather than selecting a family/address or falling back.
The original hostname/SNI and default TLS remain intact. The reported connect_count
counts actual socket.connect attempts, at most one.
A guarded HTTPSConnection prevents a second connect and disables automatic opening
after the first attempt. Any non200, Retry-After, server close/will_close, response
beyond64KiB, incomplete body, invalid schema or transport failure stops the test.
An idle-closed socket is not replaced and the interval is never shortened. The
report contains only typed errors, timing, HTTPversion, close flags, connection
count and same-socket boolean, not addresses, raw headers/bodies or credentials.
A result is feasibility evidence only—not root-cause proof, a production fix,
block evasion or capture coverage. Failure means inconclusive/unsupported.

HTTP1.1 permits connection persistence only when message framing and closure permit;
the full response must be consumed before another request. See
[RFC9112 §9.3](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3) and
[§9.5](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.5).
Python's [http.client documentation](https://docs.python.org/3/library/http.client.html)
describes full-response consumption and default verified HTTPS behavior;
[urllib.request](https://docs.python.org/3/library/urllib.request.html) normally
adds Connection:close. This experiment changes only the standalone diagnostic's
connection lifecycle, not production transport or security settings.
