# Performance benchmarks

Run from the repository root using the existing Python environment:

```powershell
.\.venv\Scripts\python.exe -B -m benchmarks.run --output "$env:TEMP\ytmusic-baseline.json"
```

The default run measures 12 routes with synthetic SDK/HTTP responses. It exercises
the actual Flask handlers, serialization, bounded memory cache, single-flight
coordination, batch workers, and upstream executor/admission limits. No server,
network, credentials, deployment configuration changes, or new dependencies are
needed. SDK construction is replaced with fixtures, and unexpected Requests
network calls are blocked.

Each route has three phases:

| Phase | Workload | Cache state |
| --- | --- | --- |
| `cold` | 10 sequential requests | Clear all caches before every request |
| `warm` | Prime once, then 10 requests with 4 request workers | Retain primed caches; exclude priming from metrics |
| `cold_burst` | 3 groups of 4 simultaneous requests to the same route/key | Clear all caches before each group; start requests at a barrier |

Routes are measured independently; clearing includes shared metadata subcaches.
App/client construction is outside measurement. The first cold sample can include
lazy service initialization. Prewarming, rate limiting, Redis, and TTL jitter are
disabled; positive cache TTLs are an hour to avoid expiration during ordinary runs.
Other settings use project defaults independently of shell environment variables.

The JSON report includes p50/p95/p99 latency, request throughput, HTTP status and
payload failures, response sizes, cache headers and internal metric deltas, counted
upstream attempts by method/provider, maximum active upstream work, sampled cache
bytes, and runtime/dependency/settings/Git metadata. Routes lacking `X-Cache` are
reported as `unreported`; zero upstream calls still demonstrates reuse. Cache bytes measure
serialized retained content, not RSS or temporary Python allocation peaks. Internal
cache metric deltas use existing application counters and may be approximate under
concurrency; upstream attempt counts use a dedicated lock. Partial batch/chart
failures count as failed responses even on 200.

Latency spans dispatch through response-body construction in a Flask test client.
JSON validation is outside latency timing. Throughput includes request-task
dispatch, joining, and validation, but excludes cache clears, priming, snapshots,
and pool construction. These are **local WSGI measurements**, not deployed HTTP
latency or Waitress/network throughput. Upstream delay is simulated (5 ms per
attempt); it does not model real provider variability, errors, retries, or quotas.
Concurrent random selection can vary response order; fixtures and response sizes
remain controlled. Low sample counts are smoke checks, not reliable tail estimates.

For a longer run or a focused comparison:

```powershell
.\.venv\Scripts\python.exe -B -m benchmarks.run --samples 100 --bursts 10 --concurrency 8 --upstream-workers 4 --delay-ms 5 --routes trending mix songs --output "$env:TEMP\ytmusic-before.json"
# After changing code, repeat the identical arguments and compare:
.\.venv\Scripts\python.exe -B -m benchmarks.run --samples 100 --bursts 10 --concurrency 8 --upstream-workers 4 --delay-ms 5 --routes trending mix songs --baseline "$env:TEMP\ytmusic-before.json" --output "$env:TEMP\ytmusic-after.json"
```

Comparison requires matching fixture/schema versions, route coverage, workload,
settings, and runtime (including dependency versions). Latency ratios below 1
indicate improvement; upstream call deltas show added/removed work. Compare
repeated runs on the same idle machine
before attributing small timing differences to a code change. Reports containing
failed responses cannot be compared. Exit codes are 0 for successful measurement,
1 for measured response failures, and 2 for invalid inputs, priming/setup failure,
or incompatible baselines. Output paths overwrite the specified report file.

Use `--delay-ms 0` to emphasize local CPU work, or adjust `--upstream-workers` to
measure capacity tradeoffs. Choose distinct report files for different configurations;
the comparison command intentionally rejects unlike workloads. Generated reports
can contain machine and commit metadata but no credentials or upstream bodies.
