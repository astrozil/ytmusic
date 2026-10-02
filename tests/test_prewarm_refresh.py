import asyncio
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from flask import Flask

from cache_layer import CacheLayer, key_for_billboard, key_for_trending_country
from services.hot_endpoints import HotEndpointsService
from services.prewarm import PrewarmManager


class ChartClients:
    def __init__(self):
        self.calls = Counter()
        self.version = 1
        self.failure = None
        self.empty = False
        self.empty_charts = False
        self.started = None
        self.release = None

    def call_ytmusic(self, method, *args, **kwargs):
        self.calls[method] += 1
        if method == "get_charts":
            if self.started:
                self.started.set()
                assert self.release.wait(5)
            if self.empty_charts:
                return {"videos": {"items": []}}
            return {"videos": {"items": [{"playlistId": "chart", "title": "Trending"}]}}
        if method == "get_playlist":
            if self.failure:
                raise self.failure
            return {"tracks": [] if self.empty else [{
                "videoId": f"v{self.version}", "title": "Song",
                "artists": [{"name": "Artist"}], "duration": "3:00",
                "thumbnails": [{"url": "https://example.test/art.jpg"}],
            }]}
        if method == "search":
            if kwargs.get("filter") == "artists":
                return [{"browseId": "artist"}]
            return [{"videoId": "match", "title": args[0]}]
        raise AssertionError(method)


@pytest.fixture
def setup_refresh(settings_factory, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    settings = settings_factory(
        CACHE_TTL_TRENDING_SEC=1200, CACHE_STALE_TRENDING_SEC=2400,
        CACHE_TTL_SUBCACHE_ARTIST_SEC=21600, CACHE_STALE_SUBCACHE_ARTIST_SEC=43200,
        CACHE_TTL_BILLBOARD_SEC=604800, CACHE_STALE_BILLBOARD_SEC=1209600,
        ENABLE_PREWARM="true",
    )
    app = Flask(__name__)
    clients = ChartClients()
    cache = CacheLayer(app, settings, app.logger)
    service = HotEndpointsService(clients, cache, settings, app.logger)
    manager = PrewarmManager(lambda: service, settings, app.logger)
    return now, service, clients, manager


def test_trending_refreshes_chart_and_long_lived_playlist_before_expiry(setup_refresh):
    now, service, clients, manager = setup_refresh
    assert manager._run_trending()
    old = service.cache_layer.get_envelope(key_for_trending_country("US")).envelope
    clients.version = 2
    now[0] += manager.trending_interval_sec - 1
    assert not manager._run_trending()
    assert clients.calls == {"get_charts": 1, "get_playlist": 1}
    now[0] += 1
    assert manager._run_trending()
    current = service.cache_layer.get_envelope(key_for_trending_country("US"))
    assert current.payload[0]["videoId"] == "v2"
    assert current.envelope["fetched_at"] > old["fetched_at"]
    assert now[0] < old["fresh_until"]
    assert clients.calls == {"get_charts": 2, "get_playlist": 2}
    assert service.trending("US", "5")[1:] == ("hit", False)


def test_expired_foreground_chart_refreshes_playlist_even_with_prewarm_disabled(setup_refresh):
    now, service, clients, _ = setup_refresh
    service.trending("US", "5")
    now[0] += 1201
    clients.version = 2
    payload, state, stale = service.trending("US", "5")
    assert payload[0]["videoId"] == "v2"
    assert state == "miss" and not stale
    assert clients.calls == {"get_charts": 2, "get_playlist": 2}


def test_empty_chart_refresh_preserves_cache_and_recovers_on_next_attempt(setup_refresh):
    now, service, clients, manager = setup_refresh
    manager._run_trending()
    key = key_for_trending_country("US")
    original = service.cache_layer.get_envelope(key).envelope
    now[0] += manager.trending_interval_sec
    clients.empty_charts = True
    with pytest.raises(RuntimeError, match="stale or empty"):
        manager._run_trending()
    assert service.cache_layer.get_envelope(key).envelope == original
    clients.empty_charts = False
    assert manager._run_trending()


@pytest.mark.parametrize("empty", [False, True])
def test_failed_playlist_refresh_keeps_original_cache_and_is_reported_as_failure(setup_refresh, empty):
    now, service, clients, manager = setup_refresh
    manager._run_trending()
    country_key = key_for_trending_country("US")
    playlist_key = service._subcache_key("playlist", {"playlist_id": "chart"})
    old_country = service.cache_layer.get_envelope(country_key).envelope
    old_playlist = service.cache_layer.get_envelope(playlist_key).envelope
    now[0] += manager.trending_interval_sec
    clients.empty = empty
    clients.failure = None if empty else RuntimeError("offline")
    with pytest.raises(RuntimeError, match="stale or empty"):
        manager._run_trending()
    assert service.cache_layer.get_envelope(country_key).envelope == old_country
    assert service.cache_layer.get_envelope(playlist_key).envelope == old_playlist
    assert service.trending("US", "5")[1:] == ("hit", False)
    clients.empty = False
    clients.failure = None
    clients.version = 2
    assert manager._run_trending()
    assert service.trending("US", "5")[0][0]["videoId"] == "v2"


def test_foreground_reads_old_fresh_entry_during_refresh_and_warmers_fetch_once(setup_refresh):
    now, service, clients, manager = setup_refresh
    manager._run_trending()
    now[0] += manager.trending_interval_sec
    clients.version = 2
    clients.started = threading.Event()
    clients.release = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(manager._run_trending)
        try:
            assert clients.started.wait(2)
            follower = executor.submit(manager._run_trending)
            payload, state, stale = service.trending("US", "5")
            assert payload[0]["videoId"] == "v1"
            assert state == "hit" and not stale
        finally:
            clients.release.set()
        assert leader.result(timeout=3)
        assert not follower.result(timeout=3)
    assert clients.calls == {"get_charts": 2, "get_playlist": 2}
    assert service.trending("US", "5")[0][0]["videoId"] == "v2"


def test_multiple_limits_warm_once_per_country_and_continue_after_country_failure(settings_factory):
    settings = settings_factory(PREWARM_TRENDING_COUNTRIES="US,SG", PREWARM_TRENDING_LIMITS="5,10,50")
    calls = []

    class Hot:
        def trending(self, country, limit, **kwargs):
            calls.append((country, limit, kwargs))
            if country == "US":
                raise RuntimeError("offline")
            return [{"videoId": "v"}], "miss", False

    manager = PrewarmManager(lambda: Hot(), settings, Flask(__name__).logger)
    with pytest.raises(RuntimeError, match="US: offline"):
        manager._run_trending()
    assert [(country, limit) for country, limit, _ in calls] == [("US", "50"), ("SG", "50")]
    assert all(kwargs == {"refresh_after_sec": manager.trending_interval_sec} for _, _, kwargs in calls)


def test_billboard_proactively_refreshes_stats_without_repeating_song_matches(setup_refresh, monkeypatch):
    now, service, clients, manager = setup_refresh
    calls = []
    rank = [1]
    week = ["2026-09-29"]
    monkeypatch.setattr(service, "_billboard_week_key", lambda: week[0])

    def chart(*args, **kwargs):
        calls.append(week[0])
        return [SimpleNamespace(title="Song", artist="Artist", rank=rank[0], lastPos=2, peakPos=1, weeks=8)]

    monkeypatch.setattr("services.hot_endpoints.BillboardChart", chart)
    assert manager._run_billboard()
    now[0] += 30
    assert not manager._run_billboard()
    assert len(calls) == 1
    now[0] = 1000 + manager.billboard_interval_sec
    rank[0] = 3
    assert manager._run_billboard()
    payload, state, stale = asyncio.run(service.billboard())
    assert payload["data"][0]["rank"] == 3
    assert state == "hit" and not stale
    assert len(calls) == 2
    # Artist metadata may expire daily; successful song matches retain their 30-day TTL.
    assert clients.calls["search"] == 3
    week[0] = "2026-10-06"
    now[0] += 30
    assert manager._run_billboard()
    assert len(calls) == 3


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_proactive_refresh_failure_does_not_extend_existing_fresh_or_stale_ttl(setup_refresh, mode):
    now, service, _, _ = setup_refresh
    service.cache_layer.set_envelope("refresh", {"value": "old"}, 1200, 2400)
    old = service.cache_layer.get_envelope("refresh").envelope
    now[0] += 600

    def fail():
        raise RuntimeError("offline")

    async def fail_async():
        fail()

    if mode == "sync":
        result = service._with_cache_sync("refresh", 1200, 2400, fail, refresh_after_sec=600)
    else:
        result = asyncio.run(service._with_cache_async("refresh", 1200, 2400, fail_async, refresh_after_sec=600))
    assert result == ({"value": "old"}, "stale", True)
    assert service.cache_layer.get_envelope("refresh").envelope == old


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_distributed_refresh_waits_for_new_entry_instead_of_accepting_old_hit(setup_refresh, monkeypatch, mode):
    now, service, _, _ = setup_refresh
    cache = service.cache_layer
    cache.set_envelope("refresh", "old", 1200, 2400)
    now[0] += 600
    service._distributed_singleflight_enabled = True
    monkeypatch.setattr(service, "_try_acquire_distributed_lock", lambda *args: False)
    reads = [0]
    original = cache.get_envelope

    def read(key):
        reads[0] += 1
        if reads[0] == 4:
            cache.set_envelope(key, "new", 1200, 2400)
        return original(key)

    monkeypatch.setattr(cache, "get_envelope", read)

    def never_fetch():
        raise AssertionError("Follower fetched instead of waiting for leader")

    async def never_fetch_async():
        never_fetch()

    if mode == "sync":
        result = service._with_cache_sync("refresh", 1200, 2400, never_fetch, refresh_after_sec=600)
    else:
        result = asyncio.run(service._with_cache_async("refresh", 1200, 2400, never_fetch_async, refresh_after_sec=600))
    assert result == ("new", "hit", False)
    assert reads[0] == 4


def test_scheduler_polls_cache_hits_without_refetching_and_recovers_eviction(setup_refresh, monkeypatch):
    now, service, clients, manager = setup_refresh
    monkeypatch.setattr(manager, "_run_billboard", lambda: False)
    assert manager.run_cycle_once(now[0])
    now[0] += 31
    assert manager.run_cycle_once(now[0])
    assert clients.calls == {"get_charts": 1, "get_playlist": 1}
    assert manager.snapshot()["endpoints"]["trending"]["refresh_count"] == 1
    service.cache_layer.cache.delete(key_for_trending_country("US"))
    now[0] += 31
    assert manager.run_cycle_once(now[0])
    assert clients.calls == {"get_charts": 2, "get_playlist": 1}
    assert manager.snapshot()["endpoints"]["trending"]["refresh_count"] == 2


@pytest.mark.parametrize("jitter", [0, 0.5])
def test_short_ttl_refresh_interval_is_before_earliest_jittered_expiry(settings_factory, jitter):
    settings = settings_factory(CACHE_TTL_TRENDING_SEC=60, CACHE_TTL_BILLBOARD_SEC=60, CACHE_JITTER_PCT=jitter)
    manager = PrewarmManager(lambda: None, settings, Flask(__name__).logger)
    assert manager.trending_interval_sec == manager.billboard_interval_sec == int(30 * (1 - jitter))


def test_failure_retry_backoff_is_shorter_than_daily_refresh_and_resets_on_success(setup_refresh, monkeypatch):
    now, _, _, manager = setup_refresh
    monkeypatch.setattr(manager, "_run_trending", lambda: False)
    failure = [True]

    def billboard():
        if failure[0]:
            raise RuntimeError("offline")
        return True

    monkeypatch.setattr(manager, "_run_billboard", billboard)
    manager.run_cycle_once(now[0])
    first = manager.snapshot()["endpoints"]["billboard"]
    assert first["next_due_at"] == pytest.approx(now[0] + 60, abs=0.1)
    assert first["consecutive_failures"] == 1
    now[0] = first["next_due_at"] + 1
    manager.run_cycle_once(now[0])
    second = manager.snapshot()["endpoints"]["billboard"]
    assert second["next_due_at"] == pytest.approx(now[0] + 120, abs=0.1)
    failure[0] = False
    now[0] = second["next_due_at"] + 1
    manager.run_cycle_once(now[0])
    assert manager.snapshot()["endpoints"]["billboard"]["consecutive_failures"] == 0


def test_partial_or_stale_billboard_warm_is_a_failure(setup_refresh, monkeypatch):
    _, service, _, manager = setup_refresh

    async def incomplete(**kwargs):
        return {"data": [{"ytmusic_result": {}}]}, "miss", False

    monkeypatch.setattr(service, "billboard", incomplete)
    with pytest.raises(RuntimeError, match="incomplete"):
        manager._run_billboard()


def test_concurrent_cycles_are_skipped_and_stop_does_not_start_billboard(setup_refresh, monkeypatch):
    _, _, _, manager = setup_refresh
    started = threading.Event()
    release = threading.Event()
    billboard = []

    def trending():
        started.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(manager, "_run_trending", trending)
    monkeypatch.setattr(manager, "_run_billboard", lambda: billboard.append(True))
    with ThreadPoolExecutor(max_workers=1) as executor:
        cycle = executor.submit(manager.run_cycle_once, 1000)
        try:
            assert started.wait(2)
            assert not manager.run_cycle_once(1000)
            manager.stop(timeout=0)
        finally:
            release.set()
        assert cycle.result(timeout=3)
    assert billboard == []
