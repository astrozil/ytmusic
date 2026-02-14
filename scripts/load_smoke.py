import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests


WARM_TARGET_MS = 300.0
COLD_TARGET_MS = 2500.0


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[idx]


def timed_request(session, method, url, payload=None):
    started = time.perf_counter()
    if method == "GET":
        response = session.get(url, timeout=15)
    else:
        response = session.post(url, json=payload, timeout=15)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return response.status_code, elapsed_ms


def build_endpoints(base_url, artist_ids, song_ids):
    return [
        {
            "name": "trending",
            "method": "GET",
            "path": "/trending",
            "query": {"country": "US", "limit": 50},
            "payload": None,
        },
        {
            "name": "mix",
            "method": "GET",
            "path": "/mix",
            "query": {"artists": ",".join(artist_ids), "limit": 50},
            "payload": None,
        },
        {
            "name": "recommendations",
            "method": "POST",
            "path": "/recommendations",
            "query": {},
            "payload": {"song_ids": song_ids[:50]},
        },
        {
            "name": "billboard",
            "method": "GET",
            "path": "/billboard",
            "query": {},
            "payload": None,
        },
    ]


def endpoint_url(base_url, endpoint, iteration, cold):
    query = dict(endpoint["query"])
    if cold:
        if endpoint["name"] == "trending":
            countries = ["US", "CA", "GB", "AU"]
            query["country"] = countries[iteration % len(countries)]
        elif endpoint["name"] == "mix":
            query["limit"] = 45 + (iteration % 6)
        elif endpoint["name"] == "billboard":
            # Billboard key is weekly; this query does not bypass cache.
            query["cold_probe"] = iteration

    encoded = urlencode(query)
    if encoded:
        return f"{base_url.rstrip('/')}{endpoint['path']}?{encoded}"
    return f"{base_url.rstrip('/')}{endpoint['path']}"


def endpoint_payload(endpoint, iteration, cold):
    payload = endpoint["payload"]
    if not cold or payload is None:
        return payload
    if endpoint["name"] == "recommendations":
        song_ids = list(payload["song_ids"])
        if song_ids:
            song_ids.append(song_ids[iteration % len(song_ids)])
        return {"song_ids": song_ids[:50]}
    return payload


def run_series(session, base_url, endpoint, iterations, cold):
    latencies = []
    failures = 0

    if not cold:
        warmup_url = endpoint_url(base_url, endpoint, 0, cold=False)
        warmup_payload = endpoint_payload(endpoint, 0, cold=False)
        timed_request(session, endpoint["method"], warmup_url, payload=warmup_payload)

    for i in range(iterations):
        url = endpoint_url(base_url, endpoint, i, cold=cold)
        payload = endpoint_payload(endpoint, i, cold=cold)
        status, elapsed_ms = timed_request(session, endpoint["method"], url, payload=payload)
        if status != 200:
            failures += 1
            continue
        latencies.append(elapsed_ms)

    p95 = percentile(latencies, 95)
    avg = statistics.mean(latencies) if latencies else None
    return {
        "count": len(latencies),
        "failures": failures,
        "avg_ms": avg,
        "p95_ms": p95,
    }


def run_concurrency_probe(session, base_url, endpoint, total_requests, concurrency):
    latencies = []
    failures = 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for i in range(total_requests):
            url = endpoint_url(base_url, endpoint, i, cold=False)
            payload = endpoint_payload(endpoint, i, cold=False)
            futures.append(
                executor.submit(
                    timed_request,
                    session,
                    endpoint["method"],
                    url,
                    payload,
                )
            )

        for future in as_completed(futures):
            status, elapsed_ms = future.result()
            if status != 200:
                failures += 1
                continue
            latencies.append(elapsed_ms)

    return {
        "count": len(latencies),
        "failures": failures,
        "p95_ms": percentile(latencies, 95),
    }


def print_result(label, result):
    print(
        f"{label}: count={result['count']} failures={result['failures']} "
        f"avg_ms={result['avg_ms']:.1f} p95_ms={result['p95_ms']:.1f}"
        if result["avg_ms"] is not None and result["p95_ms"] is not None
        else f"{label}: count={result['count']} failures={result['failures']} avg_ms=n/a p95_ms=n/a"
    )


def main():
    parser = argparse.ArgumentParser(description="Lightweight load smoke checks for YTMusic API")
    parser.add_argument("--base-url", default="http://localhost:5000")
    parser.add_argument("--artist-ids", default="UCJrOtniJ0-NWz37R30urifQ")
    parser.add_argument("--song-ids", default="dQw4w9WgXcQ")
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--concurrency-requests", type=int, default=40)
    args = parser.parse_args()

    artist_ids = [value.strip() for value in args.artist_ids.split(",") if value.strip()]
    song_ids = [value.strip() for value in args.song_ids.split(",") if value.strip()]
    if not artist_ids or not song_ids:
        raise SystemExit("artist-ids and song-ids must each contain at least one ID")

    endpoints = build_endpoints(args.base_url, artist_ids, song_ids)
    session = requests.Session()

    warm_ok = True
    cold_ok = True

    print("=== Warm Cache Smoke ===")
    for endpoint in endpoints:
        result = run_series(
            session=session,
            base_url=args.base_url,
            endpoint=endpoint,
            iterations=args.iterations,
            cold=False,
        )
        print_result(f"{endpoint['name']} warm", result)
        if result["p95_ms"] is None or result["p95_ms"] > WARM_TARGET_MS or result["failures"] > 0:
            warm_ok = False

    print("\n=== Cold Path Smoke ===")
    for endpoint in endpoints:
        result = run_series(
            session=session,
            base_url=args.base_url,
            endpoint=endpoint,
            iterations=args.iterations,
            cold=True,
        )
        print_result(f"{endpoint['name']} cold", result)
        if result["p95_ms"] is None or result["p95_ms"] > COLD_TARGET_MS or result["failures"] > 0:
            cold_ok = False

    print("\n=== Concurrency Probe ===")
    for endpoint in endpoints:
        result = run_concurrency_probe(
            session=session,
            base_url=args.base_url,
            endpoint=endpoint,
            total_requests=args.concurrency_requests,
            concurrency=args.concurrency,
        )
        print(
            f"{endpoint['name']} concurrency: count={result['count']} failures={result['failures']} "
            f"p95_ms={result['p95_ms']:.1f}" if result["p95_ms"] is not None else
            f"{endpoint['name']} concurrency: count={result['count']} failures={result['failures']} p95_ms=n/a"
        )

    if warm_ok and cold_ok:
        print("\nRESULT: PASS")
        raise SystemExit(0)

    print(
        "\nRESULT: FAIL (targets: warm p95 <= 300ms, cold p95 <= 2500ms, and zero request failures)"
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
