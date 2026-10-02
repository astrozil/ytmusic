"""Run with: python -B -m benchmarks.run --output baseline.json"""

import argparse
import atexit
import json
import logging
import math
import os
import platform
from importlib.metadata import version
import statistics
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from benchmarks.fixtures import SyntheticUpstream


@dataclass(frozen=True)
class Route:
    name: str
    path: str
    method: str = "GET"
    body: dict | None = None
    expected_items: int | None = None


ROUTES = (
    Route("trending", "/trending?country=US&limit=50", expected_items=50),
    Route("mix", "/mix?artists=artist-a,artist-b&limit=50", expected_items=50),
    Route("billboard", "/billboard", expected_items=100),
    Route("song", "/song/song-a"),
    Route("artist", "/artist/artist-a"),
    Route("album", "/album/album-a"),
    Route("artist_songs", "/artist/artist-a/songs", expected_items=45),
    Route("songs", "/songs", "POST", {"song_ids": [f"song-{i}" for i in range(5)]}, 5),
    Route("recommendations", "/recommendations", "POST", {"song_ids": ["seed-a", "seed-b"]}, 50),
    Route("lyrics", "/lyrics?title=Benchmark&artist=artist-a"),
    Route("artists", "/artists", "POST", {"artist_ids": ["artist-a", "artist-b"]}, 2),
    Route("related", "/related/seed-a", expected_items=60),
)


@dataclass(frozen=True)
class Workload:
    samples: int = 10
    concurrency: int = 4
    bursts: int = 3
    upstream_workers: int = 4
    delay_ms: float = 5.0

    def __post_init__(self):
        for name, maximum in [("samples", 10000), ("concurrency", 64),
                              ("bursts", 1000), ("upstream_workers", 256)]:
            value = getattr(self, name)
            if not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if not math.isfinite(self.delay_ms) or not 0 <= self.delay_ms <= 1000:
            raise ValueError("delay_ms must be between 0 and 1000")


def percentile(values, percent):
    """Linearly interpolate ordered samples, including singleton runs."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def valid_payload(route, payload):
    if route.name == "billboard":
        return (isinstance(payload, dict) and len(payload.get("data", [])) == route.expected_items
                and all(item.get("ytmusic_result", {}).get("videoId")
                        and all(artist.get("id") for artist in item.get("artists", []))
                        for item in payload["data"]))
    if route.name == "artists":
        return (isinstance(payload, dict) and payload.get("total_failed") == 0
                and len(payload.get("artists", [])) == route.expected_items)
    if route.expected_items is not None:
        return (isinstance(payload, list) and len(payload) == route.expected_items
                and all(isinstance(item, dict) and "error" not in item for item in payload))
    if route.name == "song":
        return isinstance(payload, dict) and bool(payload.get("videoDetails", {}).get("videoId"))
    if route.name == "lyrics":
        return isinstance(payload, dict) and bool(payload.get("lyrics"))
    return isinstance(payload, dict) and bool(payload.get("name") or payload.get("title"))


def request_sample(app, route, barrier=None):
    # Each thread owns its test client; requests never share request contexts.
    with app.test_client() as client:
        if barrier is not None:
            barrier.wait(timeout=60)
        started = time.perf_counter()
        response = client.open(route.path, method=route.method, json=route.body)
        body = response.get_data()
        duration_ms = (time.perf_counter() - started) * 1000
        try:
            payload_ok = valid_payload(route, response.get_json())
        except (ValueError, TypeError, AttributeError, KeyError):
            payload_ok = False
        return {"latency_ms": duration_ms, "status": response.status_code,
                "cache": response.headers.get("X-Cache", "unreported"),
                "bytes": len(body), "ok": response.status_code == 200 and payload_ok}


@contextmanager
def benchmark_app(workload):
    from settings import Settings
    # Build settings independently of deployment variables, and guard app.py's
    # module-level app construction when it is imported for the first time.
    with patch.dict(os.environ, {"ENABLE_PREWARM": "false", "ENABLE_RATE_LIMITS": "false",
                                 "CACHE_BACKEND": "simple"}, clear=True):
        from app import create_app
        settings = Settings.from_env()
    settings = replace(settings, ytmusic_auth_file=None, upstream_max_workers=workload.upstream_workers,
                       upstream_timeout_sec=30, upstream_retry_attempts=0, cache_jitter_pct=0,
                       enable_distributed_singleflight=False,
                       **{name: 3600 for name in asdict(settings)
                          if name.startswith("cache_ttl_") and "negative" not in name},
                       **{name: 7200 for name in asdict(settings) if name.startswith("cache_stale_")})
    from clients import UpstreamClients
    fixtures = SyntheticUpstream(workload.delay_ms)
    clients = None
    app = None
    # Fail closed if a fixture misses a transport, rather than silently using the network.
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("Offline benchmark blocked network")):
        try:
            with patch("clients.YTMusic", return_value=fixtures):
                clients = UpstreamClients(settings, logging.getLogger("benchmarks"))
            with patch.object(clients.http, "get", side_effect=fixtures.http_get):
                app = create_app(settings_obj=settings, clients_obj=clients)
                yield app, fixtures, settings
        finally:
            if app is not None:
                manager = app.extensions["ytmusic_prewarm_manager"]
                manager.stop()
                atexit.unregister(manager.stop)
            if clients is not None:
                clients._ytmusic_executor.shutdown(wait=True, cancel_futures=True)
                clients.http.close()
                clients._ytmusic_http.close()


def measure_phase(app, fixtures, route, workload, phase):
    cache = app.extensions["ytmusic_cache_layer"]
    cache.cache.clear()
    if phase == "warm":
        priming = request_sample(app, route)
        if not priming["ok"]:
            raise RuntimeError(f"Cannot prime {route.name}: unsuccessful response")
    fixtures.reset_metrics()
    before = cache.health_snapshot()
    samples, elapsed = [], 0.0
    retained = before["memory"]["serialized_bytes"]
    concurrency = 1 if phase == "cold" else workload.concurrency
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="benchmark-request") as executor:
        if phase == "warm":
            started = time.perf_counter()
            samples = list(executor.map(lambda _: request_sample(app, route), range(workload.samples)))
            elapsed = time.perf_counter() - started
            retained = max(retained, cache.health_snapshot()["memory"]["serialized_bytes"])
        else:
            repeats = workload.samples if phase == "cold" else workload.bursts
            for _ in range(repeats):
                # Clear only after the preceding batch has joined. Includes all subcaches.
                cache.cache.clear()
                barrier = threading.Barrier(concurrency) if phase == "cold_burst" else None
                started = time.perf_counter()
                futures = [executor.submit(request_sample, app, route, barrier) for _ in range(concurrency)]
                samples.extend(future.result() for future in futures)
                elapsed += time.perf_counter() - started
                retained = max(retained, cache.health_snapshot()["memory"]["serialized_bytes"])
    after = cache.health_snapshot()
    latencies = [sample["latency_ms"] for sample in samples]
    return {
        "route": route.name, "phase": phase, "requests": len(samples), "concurrency": concurrency,
        "latency_ms": {"min": min(latencies), "p50": percentile(latencies, 50),
                       "p95": percentile(latencies, 95), "p99": percentile(latencies, 99),
                       "max": max(latencies), "mean": statistics.fmean(latencies)},
        "elapsed_sec": elapsed, "requests_per_sec": len(samples) / elapsed,
        "failures": sum(not sample["ok"] for sample in samples),
        "status_counts": dict(Counter(str(sample["status"]) for sample in samples)),
        "cache_header_counts": dict(Counter(sample["cache"] for sample in samples)),
        "response_bytes": {"min": min(sample["bytes"] for sample in samples),
                           "max": max(sample["bytes"] for sample in samples)},
        "upstream": fixtures.snapshot(),
        "cache_metrics_delta": {name: value - before["metrics"][name]
                                for name, value in after["metrics"].items()},
        "cache_memory_after": after["memory"], "max_sampled_cache_bytes": retained,
    }


def revision_metadata():
    try:
        root = Path(__file__).resolve().parents[1]
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                           stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root,
                                             stderr=subprocess.DEVNULL, text=True).strip())
        return {"commit": revision, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def run_benchmarks(workload, route_names):
    selected = [route for route in ROUTES if route.name in route_names]
    if not selected or set(route_names) - {route.name for route in ROUTES}:
        raise ValueError("Select at least one known benchmark route")
    with benchmark_app(workload) as (app, fixtures, settings):
        results = [measure_phase(app, fixtures, route, workload, phase)
                   for route in selected for phase in ("cold", "warm", "cold_burst")]
    safe_settings = {name: value for name, value in asdict(settings).items()
                     if name not in {"redis_url", "ytmusic_auth_file", "rate_limit_storage_uri"}}
    # Tuples become arrays in saved JSON; compare the same representation in memory.
    safe_settings = json.loads(json.dumps(safe_settings))
    return {"schema_version": 1, "fixture_version": 1, "mode": "offline_wsgi",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "dependencies": {
                            name: version(name) for name in ("flask", "flask-caching", "cachelib",
                                                             "werkzeug", "asgiref", "requests",
                                                             "ytmusicapi", "billboard.py")}},
            "git": revision_metadata(),
            "workload": asdict(workload), "settings": safe_settings, "results": results}


def compare_reports(current, baseline):
    if not isinstance(baseline, dict):
        raise ValueError("Baseline must be a JSON report object")
    for field in ("schema_version", "fixture_version", "mode", "workload", "settings", "runtime"):
        if baseline.get(field) != current[field]:
            raise ValueError(f"Baseline {field} differs; use identical workloads, settings, and runtime")
    old = {(row["route"], row["phase"]): row for row in baseline["results"]}
    if set(old) != {(row["route"], row["phase"]) for row in current["results"]}:
        raise ValueError("Baseline route/phase coverage differs")
    comparisons = []
    for row in current["results"]:
        previous = old[(row["route"], row["phase"])]
        if row["failures"] or previous["failures"]:
            raise ValueError("Cannot compare a report containing failed responses")
        comparisons.append({"route": row["route"], "phase": row["phase"],
                            "p50_ratio": row["latency_ms"]["p50"] / previous["latency_ms"]["p50"],
                            "p95_ratio": row["latency_ms"]["p95"] / previous["latency_ms"]["p95"],
                            "upstream_call_delta": row["upstream"]["total_calls"] - previous["upstream"]["total_calls"]})
    return comparisons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10, help="Requests per cold/warm phase")
    parser.add_argument("--concurrency", type=int, default=4, help="Warm workers and requests per cold burst")
    parser.add_argument("--bursts", type=int, default=3, help="Number of separately cleared cold bursts")
    parser.add_argument("--upstream-workers", type=int, default=4)
    parser.add_argument("--delay-ms", type=float, default=5, help="Simulated delay per upstream attempt")
    parser.add_argument("--routes", nargs="+", choices=[route.name for route in ROUTES],
                        default=[route.name for route in ROUTES])
    parser.add_argument("--output", type=Path, help="Optional JSON report path (overwrites that file)")
    parser.add_argument("--baseline", type=Path, help="Compare against a compatible JSON report")
    args = parser.parse_args(argv)
    try:
        workload = Workload(args.samples, args.concurrency, args.bursts, args.upstream_workers, args.delay_ms)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        report = run_benchmarks(workload, args.routes)
        if baseline is not None:
            report["comparison"] = compare_reports(report, baseline)
            report["baseline_git"] = baseline.get("git")
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ZeroDivisionError) as exc:
        parser.exit(2, f"Benchmark error: {exc}\n")
    print(f"{'Route':<17} {'Phase':<12} {'p50 ms':>9} {'p95 ms':>9} {'req/s':>9} {'Calls':>7} {'Failed':>7}")
    for row in report["results"]:
        print(f"{row['route']:<17} {row['phase']:<12} {row['latency_ms']['p50']:>9.2f} "
              f"{row['latency_ms']['p95']:>9.2f} {row['requests_per_sec']:>9.1f} "
              f"{row['upstream']['total_calls']:>7} {row['failures']:>7}")
    if baseline is not None:
        print("Baseline latency ratios (below 1 is faster):")
        for row in report["comparison"]:
            print(f"{row['route']:<17} {row['phase']:<12} p50={row['p50_ratio']:.3f} "
                  f"p95={row['p95_ratio']:.3f} calls_delta={row['upstream_call_delta']:+d}")
    if args.output:
        print(f"JSON report: {args.output.resolve()}")
    return int(any(row["failures"] for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
