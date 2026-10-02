import asyncio
import copy
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from flask import Flask

from app import create_app
from cache_layer import CacheLayer, key_for_billboard
from services.hot_endpoints import HotEndpointsService


class MatchingClients:
    def __init__(self):
        self.calls = Counter()
        self.results = {}
        self.lock = threading.Lock()
        self.started = None
        self.release = None

    def call_ytmusic(self, method, query, filter, **kwargs):
        assert method == "search"
        with self.lock:
            self.calls[(filter, query)] += 1
        if filter == "artists":
            return [{"browseId": "artist-id"}]
        if self.started is not None:
            self.started.set()
            assert self.release.wait(5)
        result = self.results.get(query, [{
            "videoId": f"video-{query}", "title": query,
            "artists": [{"id": "artist-id", "name": "Artist"}],
            "duration": "3:00", "thumbnails": [],
        }])
        if isinstance(result, Exception):
            raise result
        return result


def entry(title="Song", artist="Artist", rank=1, last=2, peak=1, weeks=8):
    return SimpleNamespace(title=title, artist=artist, rank=rank, lastPos=last, peakPos=peak, weeks=weeks)


def make_service(settings_factory, **settings_overrides):
    settings = settings_factory(**settings_overrides)
    app = Flask(__name__)
    clients = MatchingClients()
    cache = CacheLayer(app, settings, app.logger)
    return HotEndpointsService(clients, cache, settings, app.logger), clients


class Charts:
    def __init__(self, monkeypatch, entries=None):
        self.entries = entries or [entry()]
        self.date = "2026-10-03"
        self.calls = 0
        monkeypatch.setattr("services.hot_endpoints.BillboardChart", self.fetch)

    def fetch(self, *args, **kwargs):
        self.calls += 1
        result = list(self.entries)

        class Chart(list):
            pass

        chart = Chart(result)
        chart.date = self.date
        return chart


def test_match_ttls_default_to_30_and_60_days(settings_factory, monkeypatch):
    monkeypatch.delenv("CACHE_TTL_BILLBOARD_MATCH_SEC", raising=False)
    monkeypatch.delenv("CACHE_STALE_BILLBOARD_MATCH_SEC", raising=False)
    settings = settings_factory()
    assert settings.cache_ttl_billboard_match_sec == 30 * 86400
    assert settings.cache_stale_billboard_match_sec == 60 * 86400


def test_match_ttls_are_configurable_and_bounded(settings_factory):
    settings = settings_factory(CACHE_TTL_BILLBOARD_MATCH_SEC="300", CACHE_STALE_BILLBOARD_MATCH_SEC="10")
    assert settings.cache_ttl_billboard_match_sec == settings.cache_stale_billboard_match_sec == 300
    settings = settings_factory(CACHE_TTL_BILLBOARD_MATCH_SEC="invalid", CACHE_STALE_BILLBOARD_MATCH_SEC="invalid")
    assert settings.cache_ttl_billboard_match_sec == 30 * 86400
    assert settings.cache_stale_billboard_match_sec == 60 * 86400


def test_match_cache_normalizes_case_and_whitespace_without_mutating_result(settings_factory):
    service, clients = make_service(settings_factory)
    source = [{"videoId": "v1", "title": "Original", "thumbnails": []}]
    original = copy.deepcopy(source)
    clients.results["Song Artist"] = source
    first, first_state, first_stale = service._get_cached_billboard_match("Song", "Artist")
    second, second_state, second_stale = service._get_cached_billboard_match("  SONG  ", "  artist  ")
    assert first == second == source[0]
    assert (first_state, second_state) == ("miss", "hit")
    assert not first_stale and not second_stale
    assert clients.calls == {("songs", "Song Artist"): 1}
    assert source == original


def test_title_artist_pairs_do_not_collide_when_queries_are_identical(settings_factory):
    service, clients = make_service(settings_factory)
    service._get_cached_billboard_match("Song Artist", "Other")
    service._get_cached_billboard_match("Song", "Artist Other")
    assert clients.calls[("songs", "Song Artist Other")] == 2


def test_concurrent_duplicate_matches_search_once(settings_factory):
    service, clients = make_service(settings_factory)
    clients.started = threading.Event()
    clients.release = threading.Event()
    barrier = threading.Barrier(8)

    def fetch():
        barrier.wait(timeout=5)
        return service._get_cached_billboard_match("Song", "Artist")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch) for _ in range(8)]
        try:
            assert clients.started.wait(5)
        finally:
            clients.release.set()
        results = [future.result(timeout=5) for future in futures]
    assert Counter(state for _, state, _ in results) == {"miss": 1, "hit": 7}
    assert clients.calls[("songs", "Song Artist")] == 1


def test_new_week_reuses_matches_but_updates_chart_statistics(settings_factory, monkeypatch):
    service, clients = make_service(settings_factory)
    charts = Charts(monkeypatch, [entry("First"), entry("Second", rank=2)])
    week = ["2026-09-29"]
    now = [1000]
    monkeypatch.setattr(service, "_billboard_week_key", lambda: week[0])
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    first, state, stale = asyncio.run(service.billboard())
    assert state == "miss" and not stale
    assert len(first["data"]) == 2
    assert asyncio.run(service.billboard())[1] == "hit"
    assert charts.calls == 1

    now[0] += 8 * 86400
    week[0] = "2026-10-06"
    charts.date = "2026-10-10"
    charts.entries = [entry("Second", rank=1, last=2, weeks=9), entry("First", rank=2, last=1, weeks=9), entry("New", rank=3, last=0, peak=3, weeks=1)]
    second, state, stale = asyncio.run(service.billboard())
    assert state == "miss" and not stale
    assert charts.calls == 2
    assert [song["title"] for song in second["data"]] == ["Second", "First", "New"]
    assert [(song["rank"], song["lastPos"], song["weeks"]) for song in second["data"]] == [(1, 2, 9), (2, 1, 9), (3, 0, 1)]
    assert second["metadata"]["chart_date"] == "2026-10-10"
    assert second["metadata"]["total_items"] == 3
    assert clients.calls[("songs", "First Artist")] == clients.calls[("songs", "Second Artist")] == 1
    assert clients.calls[("songs", "New Artist")] == 1
    assert second["data"][0]["ytmusic_result"] == first["data"][1]["ytmusic_result"]
    # Artist IDs keep using their existing, shorter metadata cache.
    assert clients.calls[("artists", "Artist")] == 2


@pytest.mark.parametrize("failure", [[], [{}], [None], [{"videoId": " "}], [{"videoId": 123}], RuntimeError("upstream unavailable")])
def test_failed_matches_do_not_persist_for_a_chart_week(settings_factory, monkeypatch, failure):
    service, clients = make_service(settings_factory, CACHE_TTL_BILLBOARD_SEC="604800")
    charts = Charts(monkeypatch, [entry("Good"), entry("Retry", rank=2)])
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    clients.results["Retry Artist"] = failure
    first, state, stale = asyncio.run(service.billboard())
    assert state == "miss" and not stale
    assert first["data"][1]["ytmusic_result"] == {}
    assert asyncio.run(service.billboard())[1] == "hit"
    assert charts.calls == 1
    now[0] += 61
    clients.results.pop("Retry Artist")
    recovered, state, stale = asyncio.run(service.billboard())
    assert state == "miss" and not stale
    assert recovered["data"][1]["ytmusic_result"]["videoId"] == "video-Retry Artist"
    assert clients.calls[("songs", "Good Artist")] == 1
    assert clients.calls[("songs", "Retry Artist")] == 2
    assert charts.calls == 2


@pytest.mark.parametrize("failure", [[], RuntimeError("upstream unavailable")])
def test_stale_match_keeps_current_chart_stats_and_reports_staleness(settings_factory, monkeypatch, failure):
    service, clients = make_service(settings_factory, CACHE_TTL_BILLBOARD_MATCH_SEC="60", CACHE_STALE_BILLBOARD_MATCH_SEC="300")
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    cached, _, _ = service._get_cached_billboard_match("Song", "Artist")
    now[0] += 61
    clients.results["Song Artist"] = failure
    charts = Charts(monkeypatch, [entry(rank=5, last=3, peak=1, weeks=10)])
    payload, state, stale = asyncio.run(service.billboard())
    assert state == "stale" and stale
    assert payload["data"][0]["ytmusic_result"] == cached
    assert (payload["data"][0]["rank"], payload["data"][0]["weeks"]) == (5, 10)
    assert service.cache_layer.get_envelope(key_for_billboard(service._billboard_week_key())).state == "miss"

    clients.results.pop("Song Artist")
    recovered, state, stale = asyncio.run(service.billboard())
    assert state == "miss" and not stale
    assert charts.calls == 2
    assert asyncio.run(service.billboard())[1] == "hit"


def test_expired_match_is_not_used_on_failure(settings_factory, monkeypatch):
    service, clients = make_service(settings_factory, CACHE_TTL_BILLBOARD_MATCH_SEC="60", CACHE_STALE_BILLBOARD_MATCH_SEC="120")
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    service._get_cached_billboard_match("Song", "Artist")
    now[0] += 121
    clients.results["Song Artist"] = RuntimeError("upstream unavailable")
    Charts(monkeypatch)
    payload, state, stale = asyncio.run(service.billboard())
    assert payload["data"][0]["ytmusic_result"] == {}
    assert state == "miss" and not stale


def test_route_preserves_schema_artwork_and_stale_headers(settings_factory, monkeypatch):
    clients = MatchingClients()
    settings = settings_factory(CACHE_TTL_BILLBOARD_MATCH_SEC="60", CACHE_STALE_BILLBOARD_MATCH_SEC="300")
    app = create_app(settings_obj=settings, clients_obj=clients)
    charts = Charts(monkeypatch)
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    with app.test_client() as client:
        first = client.get("/billboard")
        assert first.status_code == 200
        row = first.get_json()["data"][0]
        assert set(row) == {"rank", "title", "artists", "lastPos", "peakPos", "weeks", "ytmusic_result"}
        assert row["ytmusic_result"]["thumbnails"][-1]["url"].endswith("hqdefault.jpg")
        now[0] += 61
        clients.results["Song Artist"] = RuntimeError("upstream unavailable")
        charts.entries = [entry(rank=3, weeks=9)]
        stale = client.get("/billboard")
        assert stale.status_code == 200
        assert stale.headers["X-Cache"] == "stale"
        assert stale.headers["X-Data-Stale"] == "1"
        assert stale.get_json()["data"][0]["rank"] == 3
        clients.results.pop("Song Artist")
        recovered = client.get("/billboard")
        assert recovered.headers["X-Cache"] == "miss"
        assert "X-Data-Stale" not in recovered.headers


def test_stale_match_fallback_can_be_disabled(settings_factory, monkeypatch):
    service, clients = make_service(settings_factory, ENABLE_STALE_FALLBACK="false", CACHE_TTL_BILLBOARD_MATCH_SEC="60", CACHE_STALE_BILLBOARD_MATCH_SEC="300")
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    service._get_cached_billboard_match("Song", "Artist")
    now[0] += 61
    clients.results["Song Artist"] = RuntimeError("upstream unavailable")
    Charts(monkeypatch)
    payload, state, stale = asyncio.run(service.billboard())
    assert payload["data"][0]["ytmusic_result"] == {}
    assert state == "miss" and not stale
