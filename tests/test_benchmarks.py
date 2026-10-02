import copy
import json
import os
import threading
from unittest.mock import patch

import pytest
import requests

from benchmarks.run import (
    ROUTES, Workload, benchmark_app, compare_reports, main, percentile,
    run_benchmarks, valid_payload,
)


def test_all_routes_measure_cold_warm_and_shared_bursts_without_network():
    report = run_benchmarks(Workload(samples=1, concurrency=3, bursts=1,
                                     upstream_workers=2, delay_ms=1),
                            [route.name for route in ROUTES])
    assert len(report["results"]) == len(ROUTES) * 3
    for route in ROUTES:
        cold, warm, burst = [row for row in report["results"] if row["route"] == route.name]
        assert [row["phase"] for row in (cold, warm, burst)] == ["cold", "warm", "cold_burst"]
        for row in (cold, warm, burst):
            assert row["failures"] == 0
            assert row["status_counts"] == {"200": row["requests"]}
            assert row["latency_ms"]["min"] <= row["latency_ms"]["p50"] <= row["latency_ms"]["p95"] <= row["latency_ms"]["max"]
            assert row["requests_per_sec"] > 0
            assert row["upstream"]["active"] == 0
            assert row["upstream"]["peak_active"] <= 2
            assert row["cache_memory_after"]["serialized_bytes"] <= row["max_sampled_cache_bytes"]
        assert cold["upstream"]["total_calls"] > 0
        assert warm["upstream"]["total_calls"] == 0
        assert burst["upstream"]["calls"] == cold["upstream"]["calls"]
        assert burst["requests"] == 3
    # Several existing routes do not expose cache state headers. Do not invent hits.
    lyrics_warm = next(row for row in report["results"] if row["route"] == "lyrics" and row["phase"] == "warm")
    assert lyrics_warm["cache_header_counts"] == {"unreported": 1}
    assert "redis_url" not in report["settings"]
    assert "ytmusic_auth_file" not in report["settings"]
    assert not any(thread.name.startswith(("ytmusic-upstream", "benchmark-request"))
                   for thread in threading.enumerate())


def test_repeated_cold_samples_and_bursts_reset_all_caches():
    report = run_benchmarks(Workload(samples=3, concurrency=4, bursts=2, delay_ms=2), ["song"])
    cold, warm, burst = report["results"]
    assert cold["upstream"]["total_calls"] == 3
    assert warm["upstream"]["total_calls"] == 0
    assert burst["upstream"]["total_calls"] == 2
    assert [row["requests"] for row in report["results"]] == [3, 3, 8]
    assert cold["cache_header_counts"] == {"miss": 3}
    assert warm["cache_header_counts"] == {"hit": 3}
    assert burst["cache_header_counts"] == {"miss": 2, "hit": 6}


def test_offline_context_ignores_deployment_environment_and_cleans_up_on_failure(monkeypatch):
    monkeypatch.setenv("ENABLE_PREWARM", "true")
    monkeypatch.setenv("CACHE_BACKEND", "redis")
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", "must-not-open.json")
    monkeypatch.setenv("CACHE_MEMORY_MAX_BYTES", "1048576")
    original = os.environ.copy()
    with pytest.raises(RuntimeError, match="intentional"):
        with benchmark_app(Workload(upstream_workers=1)) as (app, _, settings):
            clients = app.extensions["ytmusic_clients"]
            assert settings.ytmusic_auth_file is None
            assert settings.cache_backend == "simple"
            assert settings.cache_memory_max_bytes == 64 * 1024 * 1024
            assert not settings.enable_prewarm and not settings.enable_rate_limits
            with pytest.raises(RuntimeError, match="blocked network"):
                requests.get("https://example.invalid")
            raise RuntimeError("intentional")
    assert os.environ == original
    assert clients._ytmusic_executor._shutdown
    assert not any(thread.name.startswith("ytmusic-upstream") for thread in threading.enumerate())


@pytest.mark.parametrize("options", [
    {"samples": 0}, {"concurrency": 65}, {"bursts": -1}, {"upstream_workers": 257},
    {"delay_ms": -1}, {"delay_ms": float("nan")}, {"delay_ms": float("inf")},
])
def test_invalid_workloads_are_rejected(options):
    with pytest.raises(ValueError):
        Workload(**options)


def test_percentiles_interpolate_without_index_bias():
    assert percentile([40, 10, 30, 20], 50) == 25
    assert percentile([10, 20, 30, 40], 95) == pytest.approx(38.5)
    assert percentile([12], 99) == 12


def test_partial_success_payloads_are_failures():
    by_name = {route.name: route for route in ROUTES}
    assert not valid_payload(by_name["songs"], [{"error": "failed"}] * 5)
    assert not valid_payload(by_name["artists"], {"artists": [{}, {}], "total_failed": 1})
    assert not valid_payload(by_name["billboard"], {"data": [{"ytmusic_result": {}}] * 100})
    assert not valid_payload(by_name["lyrics"], {"lyrics": ""})
    assert not valid_payload(by_name["trending"], [])


def test_report_comparisons_require_matching_measurements():
    current = run_benchmarks(Workload(samples=1, concurrency=1, bursts=1, delay_ms=0), ["song"])
    baseline = copy.deepcopy(current)
    for row in baseline["results"]:
        row["latency_ms"]["p50"] *= 2
        row["latency_ms"]["p95"] *= 2
    comparisons = compare_reports(current, baseline)
    assert all(row["p50_ratio"] == row["p95_ratio"] == 0.5 for row in comparisons)
    assert all(row["upstream_call_delta"] == 0 for row in comparisons)
    assert "flask" in current["runtime"]["dependencies"]
    with pytest.raises(ValueError, match="report object"):
        compare_reports(current, [])
    for field in ("schema_version", "fixture_version", "mode", "workload", "settings", "runtime"):
        incompatible = copy.deepcopy(baseline)
        incompatible[field] = None
        with pytest.raises(ValueError, match=field):
            compare_reports(current, incompatible)
    incomplete = copy.deepcopy(baseline)
    incomplete["results"].pop()
    with pytest.raises(ValueError, match="coverage"):
        compare_reports(current, incomplete)
    failed = copy.deepcopy(baseline)
    failed["results"][0]["failures"] = 1
    with pytest.raises(ValueError, match="failed responses"):
        compare_reports(current, failed)


def test_cli_saves_report_and_displays_comparison(tmp_path, capsys):
    path = tmp_path / "report.json"
    args = ["--samples", "1", "--concurrency", "1", "--bursts", "1", "--delay-ms", "0",
            "--routes", "song", "--output", str(path)]
    assert main(args) == 0
    assert len(json.loads(path.read_text())["results"]) == 3
    assert main(args + ["--baseline", str(path)]) == 0
    assert "Baseline latency ratios" in capsys.readouterr().out
    assert len(json.loads(path.read_text())["comparison"]) == 3


def test_cli_returns_failure_status_when_responses_fail(capsys):
    report = {"results": [{"route": "song", "phase": "cold", "latency_ms": {"p50": 1, "p95": 2},
                           "requests_per_sec": 1, "upstream": {"total_calls": 1}, "failures": 1}]}
    with patch("benchmarks.run.run_benchmarks", return_value=report):
        assert main(["--routes", "song"]) == 1
    assert "song" in capsys.readouterr().out
