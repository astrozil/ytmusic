import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from app import create_app
from clients import UpstreamClients
from services.lyrics import (
    LyricsLookupUnavailable,
    _provider_lookups,
    key_for_lyrics,
    key_for_lyrics_negative,
    lyrics_negative_backoff_ttl,
    resolve_lyrics_payload,
)


class Response:
    def __init__(self, status=404, payload=None, text=""):
        self.status_code = status
        self.payload = payload
        self.text = text
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class ScriptedClients:
    def __init__(self, action):
        self.action = action
        self.calls = []
        self.responses = []
        self.lock = threading.Lock()

    def http_get(self, url, timeout=None, **kwargs):
        with self.lock:
            self.calls.append({"url": url, "timeout": timeout, **kwargs})
        response = self.action(url, timeout, kwargs)
        with self.lock:
            self.responses.append(response)
        return response


@pytest.mark.parametrize("outcome", ["success", "missing", "unavailable"])
@pytest.mark.parametrize("cache_writes_fail", [False, True])
def test_normalized_concurrent_requests_share_result_and_cleanup(
    settings_factory, monkeypatch, outcome, cache_writes_fail,
):
    entered = threading.Event()
    release = threading.Event()
    followers_ready = threading.Event()
    follower_count = [0]
    counter_lock = threading.Lock()
    start_barrier = threading.Barrier(8)
    local = threading.local()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                with counter_lock:
                    follower_count[0] += 1
                    if follower_count[0] == 7:
                        followers_ready.set()
            return super().result(timeout=timeout)

    def action(url, timeout, kwargs):
        if "lrclib" in url:
            entered.set()
            assert release.wait(5)
            if outcome == "success":
                return Response(200, {"plainLyrics": "shared result"})
            if outcome == "unavailable":
                raise requests.ConnectionError("offline")
        return Response()

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    service = app.extensions["ytmusic_lyrics_service"]
    cache = app.extensions["ytmusic_cache_layer"]
    if cache_writes_fail:
        monkeypatch.setattr(cache, "cache_set_safe", lambda *args, **kwargs: False)
    original_cached = service._cached

    def synchronized_cache_read(key):
        cached = original_cached(key)
        if not getattr(local, "started", False):
            local.started = True
            start_barrier.wait(timeout=5)
        return cached

    monkeypatch.setattr(service, "_cached", synchronized_cache_read)
    monkeypatch.setattr("services.lyrics.Future", ObservedFuture)

    def request(index):
        with app.test_client() as client:
            return client.get("/lyrics", query_string={
                "title": "Song A" if index % 2 else "  song   a  ",
                "artist": "Artist A" if index % 2 else "ARTIST   A",
            })

    with ThreadPoolExecutor(max_workers=8) as callers:
        futures = [callers.submit(request, index) for index in range(8)]
        try:
            assert entered.wait(5)
            assert followers_ready.wait(5)
            assert len(service._inflight) == 1
        finally:
            release.set()
        responses = [future.result(timeout=5) for future in futures]
    expected_status = {"success": 200, "missing": 404, "unavailable": 503}[outcome]
    assert Counter(response.status_code for response in responses) == {expected_status: 8}
    assert all(response.get_json() == responses[0].get_json() for response in responses)
    assert len(clients.calls) == (1 if outcome == "success" else 3)
    assert service._inflight == {}
    assert all(response.closed for response in clients.responses)
    negative = cache.cache_get_safe(key_for_lyrics_negative("Song A", "Artist A"))
    if not cache_writes_fail and outcome == "missing":
        assert negative["failures"] == 1
    elif outcome == "unavailable" or cache_writes_fail:
        assert negative is None


def test_different_song_lookups_run_independently(settings_factory):
    barrier = threading.Barrier(2)

    def action(url, timeout, kwargs):
        barrier.wait(timeout=5)
        return Response(200, {"plainLyrics": kwargs["params"]["track_name"]})

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    service = app.extensions["ytmusic_lyrics_service"]
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(service.lookup, "First", "Artist")
        second = callers.submit(service.lookup, "Second", "Artist")
        assert first.result(timeout=5)["lyrics"] == "First"
        assert second.result(timeout=5)["lyrics"] == "Second"
    assert len(clients.calls) == 2
    assert service._inflight == {}


def test_follower_timeout_does_not_cancel_owner_or_publish_negative_cache(settings_factory):
    entered = threading.Event()
    release = threading.Event()

    def action(url, timeout, kwargs):
        entered.set()
        assert release.wait(5)
        return Response(200, {"plainLyrics": "owner result"})

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    service = app.extensions["ytmusic_lyrics_service"]
    with ThreadPoolExecutor(max_workers=1) as callers:
        owner = callers.submit(service.lookup, "Song", "Artist")
        try:
            assert entered.wait(5)
            service.settings = replace(service.settings, lyrics_timeout_sec=0.04)
            with pytest.raises(LyricsLookupUnavailable, match="wait exceeded"):
                service.lookup("Song", "Artist")
            assert len(service._inflight) == 1
            assert app.extensions["ytmusic_cache_layer"].cache_get_safe(
                key_for_lyrics_negative("Song", "Artist"),
            ) is None
        finally:
            release.set()
        assert owner.result(timeout=5)["lyrics"] == "owner result"
    assert service.lookup("Song", "Artist")["lyrics"] == "owner result"
    assert len(clients.calls) == 1
    assert service._inflight == {}


def test_slow_early_providers_leave_budget_for_the_final_fallback(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("services.lyrics.time.monotonic", lambda: now[0])

    def action(url, timeout, kwargs):
        now[0] += timeout * 0.9
        if "lyrics.ovh" in url and url.endswith("/Primary/Song"):
            return Response(200, {"lyrics": "fallback success"})
        raise requests.Timeout("provider stalled")

    clients = ScriptedClients(action)
    payload = resolve_lyrics_payload(clients, "Song", "Primary & Guest", deadline=112, provider_timeout=3)
    assert payload["source"] == "lyrics_ovh"
    assert payload["normalizedArtist"] == "Primary"
    assert len(clients.calls) == 6
    assert now[0] < 112
    assert clients.calls[0]["timeout"] == 2
    assert all(0 < call["timeout"] <= 3 for call in clients.calls)
    assert clients.responses[0].closed


def test_exhausted_budget_stops_next_calls_and_never_caches_a_miss(settings_factory, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("services.lyrics.time.monotonic", lambda: now[0])

    def action(url, timeout, kwargs):
        now[0] = 120
        return Response()

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        response = client.get("/lyrics?title=Song&artist=Primary%20%26%20Guest")
    assert response.status_code == 503
    assert len(clients.calls) == 1
    assert clients.responses[0].closed
    assert app.extensions["ytmusic_cache_layer"].cache_get_safe(
        key_for_lyrics_negative("Song", "Primary & Guest"),
    ) is None
    assert app.extensions["ytmusic_lyrics_service"]._inflight == {}


@pytest.mark.parametrize("failure", [408, 429, 500, 503, "connection", "json"])
def test_transient_provider_failure_can_retry_on_next_request(settings_factory, failure):
    def action(url, timeout, kwargs):
        if "lrclib" not in url:
            return Response()
        if failure == "connection":
            raise requests.ConnectionError("offline")
        if failure == "json":
            return Response(200, ValueError("bad JSON"))
        return Response(failure)

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        assert client.get("/lyrics?title=Song&artist=Artist").status_code == 503
        clients.action = lambda *args: Response(200, {"plainLyrics": "recovered"})
        response = client.get("/lyrics?title=Song&artist=Artist")
    assert response.status_code == 200
    assert response.get_json()["lyrics"] == "recovered"
    assert len(clients.calls) == 4
    assert all(response.closed for response in clients.responses)


def test_negative_backoff_starts_when_lookup_finishes(settings_factory, monkeypatch):
    now = [1000.25]
    monkeypatch.setattr("services.lyrics.time.time", lambda: now[0])

    def action(*args):
        now[0] += 2
        return Response()

    clients = ScriptedClients(action)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        assert client.get("/lyrics?title=Song&artist=Artist").status_code == 404
        state = app.extensions["ytmusic_cache_layer"].cache_get_safe(key_for_lyrics_negative("Song", "Artist"))
        assert state["next_retry_at"] == 1016.25
        assert client.get("/lyrics?title=Song&artist=Artist").status_code == 404
    assert len(clients.calls) == 3


def test_expired_negative_failure_resets_after_success(settings_factory):
    clients = ScriptedClients(lambda *args: Response(200, {"plainLyrics": "recovered"}))
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    cache = app.extensions["ytmusic_cache_layer"]
    key = key_for_lyrics_negative("Song", "Artist")
    cache.cache_set_safe(key, {"failures": 7, "next_retry_at": 0}, timeout=60)
    with app.test_client() as client:
        assert client.get("/lyrics?title=Song&artist=Artist").status_code == 200
    assert cache.cache_get_safe(key) == {"failures": 0, "next_retry_at": 0}


@pytest.mark.parametrize("state", ["fresh", "stale", "negative"])
def test_cache_hits_do_not_initialize_clients(settings_factory, monkeypatch, state):
    monkeypatch.setattr("app.UpstreamClients", lambda *args: pytest.fail("Initialized clients on cache hit"))
    app = create_app(settings_obj=settings_factory())
    cache = app.extensions["ytmusic_cache_layer"]
    key = key_for_lyrics("Song", "Artist")
    if state == "negative":
        cache.cache_set_safe(f"{key}:negative", {"next_retry_at": time.time() + 30}, timeout=60)
    else:
        envelope = cache.set_envelope(key, {"lyrics": "cached"}, 60, 120)
        if state == "stale":
            envelope["fresh_until"] = int(time.time()) - 1
            cache.cache_set_safe(key, envelope, timeout=120)
    with app.test_client() as client:
        response = client.get("/lyrics?title=Song&artist=Artist")
    assert response.status_code == (404 if state == "negative" else 200)
    assert app.extensions["ytmusic_clients"] is None
    assert app.extensions["ytmusic_lyrics_service"]._inflight == {}


def test_repeated_provider_urls_are_only_requested_once_and_title_spacing_is_cleaned(settings_factory):
    lookups = _provider_lookups("Song", "Artist /")
    assert len([lookup for lookup in lookups if lookup[0] == "genius"]) == 1
    clients = ScriptedClients(lambda *args: Response(200, {"plainLyrics": "found"}))
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        response = client.get("/lyrics", query_string={"title": "  Song   A ", "artist": " Artist  A "})
    assert response.status_code == 200
    assert clients.calls[0]["params"] == {"track_name": "Song A", "artist_name": "Artist A"}
    assert response.get_json()["song_title"] == "  Song   A "


def test_synced_only_and_genius_fallbacks_preserve_payload(settings_factory):
    clients = ScriptedClients(lambda *args: Response(200, {"syncedLyrics": "[00:01.00] hello\n[00:02.00] world"}))
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        synced = client.get("/lyrics?title=Synced&artist=Artist").get_json()
        assert synced["lyrics"] == "hello\nworld"
        assert synced["isSynced"] is True
        clients.action = lambda url, *args: Response(200, text='{"lyrics":"line-1\\nline-2"}') if "genius" in url else Response()
        genius = client.get("/lyrics?title=Genius&artist=Artist").get_json()
        assert genius["source"] == "genius"
        assert genius["lyrics"] == "line-1\nline-2"
        assert genius["isSynced"] is False
    assert all(response.closed for response in clients.responses)


def test_cache_write_exception_cleans_flight_and_allows_retry(settings_factory, monkeypatch):
    clients = ScriptedClients(lambda *args: Response(200, {"plainLyrics": "found"}))
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    service = app.extensions["ytmusic_lyrics_service"]
    original_set = service.cache_layer.set_envelope

    def fail(*args):
        raise RuntimeError("cache write failure")

    monkeypatch.setattr(service.cache_layer, "set_envelope", fail)
    with pytest.raises(RuntimeError, match="cache write failure"):
        service.lookup("Song", "Artist")
    assert service._inflight == {}
    monkeypatch.setattr(service.cache_layer, "set_envelope", original_set)
    assert service.lookup("Song", "Artist")["lyrics"] == "found"


def test_real_http_timeouts_bound_the_whole_provider_chain(settings_factory, monkeypatch):
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.3)
            payload = json.dumps({"plainLyrics": "late"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("clients.YTMusic", lambda *args, **kwargs: object())
    settings = replace(settings_factory(), lyrics_timeout_sec=0.18, lyrics_provider_timeout_sec=0.1,
                       upstream_max_workers=2, upstream_retry_attempts=2)
    clients = UpstreamClients(settings, logging.getLogger(__name__))
    original_get = clients.http_get
    monkeypatch.setattr(clients, "http_get", lambda url, **kwargs: original_get(
        f"http://127.0.0.1:{server.server_port}/", **kwargs,
    ))
    app = create_app(settings_obj=settings, clients_obj=clients)
    try:
        started = time.monotonic()
        with app.test_client() as client:
            response = client.get("/lyrics?title=Song&artist=Artist")
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert elapsed < 0.55
        assert app.extensions["ytmusic_cache_layer"].cache_get_safe(
            key_for_lyrics_negative("Song", "Artist"),
        ) is None
    finally:
        clients._ytmusic_executor.shutdown(wait=True, cancel_futures=True)
        clients.http.close()
        clients._ytmusic_http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("title,artist", [(" ", "Artist"), ("Song", " ")])
def test_blank_lyrics_parameters_are_rejected(settings_factory, title, artist):
    app = create_app(settings_obj=settings_factory())
    with app.test_client() as client:
        assert client.get("/lyrics", query_string={"title": title, "artist": artist}).status_code == 400
    assert app.extensions["ytmusic_clients"] is None


@pytest.mark.parametrize("value,expected", [("bad", 12), ("-2", 0.5), ("90", 60)])
def test_total_lyrics_budget_setting_is_bounded(settings_factory, value, expected):
    assert settings_factory(LYRICS_TIMEOUT_SEC=value).lyrics_timeout_sec == expected


@pytest.mark.parametrize("value,expected", [("bad", 3), ("0", 0.1), ("90", 10)])
def test_provider_budget_setting_is_bounded(settings_factory, value, expected):
    assert settings_factory(LYRICS_PROVIDER_TIMEOUT_SEC=value).lyrics_provider_timeout_sec == expected


def test_large_negative_failure_count_stays_at_ttl_cap(settings_factory):
    settings = settings_factory()
    assert lyrics_negative_backoff_ttl(settings, 100000) == settings.cache_ttl_lyrics_negative_max_sec
