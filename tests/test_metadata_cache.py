import copy
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import create_app
from services.thumbnail_quality import enhance_payload_thumbnails


class MetadataClients:
    def __init__(self):
        thumbnail = {
            "url": "https://lh3.googleusercontent.com/art=w120-h120-l90-rj",
            "width": 120,
            "height": 120,
        }
        track = {
            "videoId": "s1", "title": "Song", "duration": "2:00",
            "artists": [{"id": "a1", "name": "Artist"}],
            "thumbnails": [thumbnail],
        }
        self.payloads = {
            "get_song": {
                "videoDetails": {
                    "videoId": "s1", "title": "Song", "author": "Artist",
                    "channelId": "a1", "lengthSeconds": "120",
                    "thumbnail": {"thumbnails": [thumbnail]},
                },
                "playabilityStatus": {"status": "OK"},
            },
            "get_artist": {
                "name": "Artist", "thumbnails": [thumbnail],
                "songs": {"results": [track]},
                "sections": [{"title": "Albums", "items": [{"browseId": "al1"}]}],
            },
            "get_album": {"title": "Album", "thumbnails": [thumbnail], "tracks": [track]},
            "get_watch_playlist": {"tracks": [track], "lyrics": "lyrics-id"},
        }
        self.calls = Counter()
        self.failures = set()
        self.lock = threading.Lock()
        self.started = None
        self.release = None

    def call_ytmusic(self, method, identifier, **kwargs):
        with self.lock:
            self.calls[(method, identifier)] += 1
        if self.started is not None and method == "get_song":
            self.started.set()
            assert self.release.wait(5), "Concurrent test did not release upstream call"
        if method in self.failures:
            raise RuntimeError("upstream unavailable")
        return self.payloads[method]


CASES = [
    ("/song/s1", "get_song", "s1", "song", {"song_id": "s1"}),
    ("/artist/a1", "get_artist", "a1", "artist", {"artist_id": "a1"}),
    ("/album/al1", "get_album", "al1", "album", {"album_id": "al1"}),
    ("/related/s1", "get_watch_playlist", "s1", "watch_playlist", {"song_id": "s1"}),
]


@pytest.fixture
def metadata_app(settings_factory):
    clients = MetadataClients()
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    return app, clients


@pytest.mark.parametrize("path,method,identifier,namespace,key_data", CASES)
def test_repeated_metadata_requests_preserve_payload_and_use_cache(
    metadata_app, path, method, identifier, namespace, key_data
):
    app, clients = metadata_app
    original = copy.deepcopy(clients.payloads[method])
    expected = original["tracks"] if namespace == "watch_playlist" else original
    with app.test_client() as client:
        first = client.get(path)
        second = client.get(path)
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json() == enhance_payload_thumbnails(expected)
    assert first.headers["X-Cache"] == "miss"
    assert second.headers["X-Cache"] == "hit"
    assert "X-Data-Stale" not in second.headers
    assert clients.calls[(method, identifier)] == 1
    assert clients.payloads[method] == original


@pytest.mark.parametrize("path,method,identifier,namespace,key_data", CASES)
def test_metadata_stale_fallback_refresh_and_expiry(
    metadata_app, monkeypatch, path, method, identifier, namespace, key_data
):
    app, clients = metadata_app
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    with app.test_client() as client:
        fresh = client.get(path)
        cache = app.extensions["ytmusic_cache_layer"]
        service = app.extensions["ytmusic_hot_service"]
        key = service._subcache_key(namespace, key_data)
        envelope = cache.get_envelope(key).envelope
        now[0] = envelope["fresh_until"] + 1
        clients.failures.add(method)
        stale = client.get(path)
        assert stale.status_code == 200
        assert stale.get_json() == fresh.get_json()
        assert stale.headers["X-Cache"] == "stale"
        assert stale.headers["X-Data-Stale"] == "1"

        clients.failures.clear()
        refreshed = client.get(path)
        assert refreshed.status_code == 200
        assert refreshed.headers["X-Cache"] == "miss"
        assert "X-Data-Stale" not in refreshed.headers
        assert client.get(path).headers["X-Cache"] == "hit"

        now[0] = cache.get_envelope(key).envelope["stale_until"] + 1
        clients.failures.add(method)
        expired = client.get(path)
        assert expired.status_code == 500
        assert "error" in expired.get_json()
        assert clients.calls[(method, identifier)] == 4


@pytest.mark.parametrize("path,method,identifier,namespace,key_data", CASES)
def test_metadata_cold_failure_is_not_cached(
    metadata_app, path, method, identifier, namespace, key_data
):
    app, clients = metadata_app
    clients.failures.add(method)
    with app.test_client() as client:
        assert client.get(path).status_code == 500
        clients.failures.clear()
        assert client.get(path).headers["X-Cache"] == "miss"
        assert client.get(path).headers["X-Cache"] == "hit"
    assert clients.calls[(method, identifier)] == 2


@pytest.mark.parametrize("single_first", [True, False])
def test_song_and_batch_song_share_raw_metadata(metadata_app, single_first):
    app, clients = metadata_app
    with app.test_client() as client:
        if single_first:
            single = client.get("/song/s1")
            batch = client.post("/songs", json={"song_ids": ["s1"]})
        else:
            batch = client.post("/songs", json={"song_ids": ["s1"]})
            single = client.get("/song/s1")
            assert single.headers["X-Cache"] == "hit"
    assert single.status_code == batch.status_code == 200
    assert single.get_json()["videoDetails"]["videoId"] == "s1"
    assert batch.get_json()[0]["videoId"] == "s1"
    assert clients.calls[("get_song", "s1")] == 1


@pytest.mark.parametrize("single_first", [True, False])
@pytest.mark.parametrize("consumer", ["mix", "artist_songs", "artists"])
def test_artist_metadata_shared_with_other_routes(metadata_app, single_first, consumer):
    app, clients = metadata_app
    with app.test_client() as client:
        def request_consumer():
            if consumer == "artists":
                return client.post("/artists", json={"artist_ids": ["a1"]})
            if consumer == "artist_songs":
                return client.get("/artist/a1/songs")
            return client.get("/mix?artists=a1&limit=1")

        if single_first:
            single = client.get("/artist/a1")
            other = request_consumer()
        else:
            other = request_consumer()
            single = client.get("/artist/a1")
            assert single.headers["X-Cache"] == "hit"
    assert single.status_code == other.status_code == 200
    assert clients.calls[("get_artist", "a1")] == 1


@pytest.mark.parametrize("single_first", [True, False])
def test_album_metadata_shared_with_artist_songs(metadata_app, single_first):
    app, clients = metadata_app
    with app.test_client() as client:
        if single_first:
            single = client.get("/album/al1")
            songs = client.get("/artist/a1/songs")
        else:
            songs = client.get("/artist/a1/songs")
            single = client.get("/album/al1")
            assert single.headers["X-Cache"] == "hit"
    assert single.status_code == songs.status_code == 200
    assert songs.get_json()[0]["album"] == "Album"
    assert clients.calls[("get_album", "al1")] == 1


@pytest.mark.parametrize("single_first", [True, False])
def test_related_and_recommendations_share_watch_playlist(metadata_app, single_first):
    app, clients = metadata_app
    with app.test_client() as client:
        if single_first:
            related = client.get("/related/s1")
            recommendations = client.post("/recommendations", json={"song_ids": ["s1"]})
        else:
            recommendations = client.post("/recommendations", json={"song_ids": ["s1"]})
            related = client.get("/related/s1")
            assert related.headers["X-Cache"] == "hit"
    assert related.status_code == recommendations.status_code == 200
    assert related.get_json() == recommendations.get_json()
    assert clients.calls[("get_watch_playlist", "s1")] == 1


def test_batch_artists_preserves_duplicates_order_and_partial_failures(metadata_app):
    app, clients = metadata_app
    original_call = clients.call_ytmusic

    def failing_artist(method, identifier, **kwargs):
        if identifier == "bad":
            with clients.lock:
                clients.calls[(method, identifier)] += 1
            raise RuntimeError("missing artist")
        return original_call(method, identifier, **kwargs)

    clients.call_ytmusic = failing_artist
    with app.test_client() as client:
        response = client.post("/artists", json={"artist_ids": ["a2", "bad", "a1", "a2", "bad"]})
        body = response.get_json()
        assert response.status_code == 200
        assert [artist["browseId"] for artist in body["artists"]] == ["a2", "a1", "a2"]
        assert body["total_requested"] == 5
        assert body["total_successful"] == 3
        assert body["total_failed"] == 2
        assert body["failed_requests"] == [{"browseId": "bad", "error": "missing artist"}] * 2
        assert body["artists"][0]["thumbnail"]["width"] == 544
        assert "success" not in body["artists"][0]
        assert client.get("/artist/a2").headers["X-Cache"] == "hit"
    assert clients.calls == Counter({("get_artist", "a1"): 1, ("get_artist", "a2"): 1, ("get_artist", "bad"): 1})


def test_concurrent_song_routes_share_one_upstream_request(metadata_app):
    app, clients = metadata_app
    # Initialize the shared service before the concurrent cold song requests.
    with app.test_client() as client:
        client.get("/artist/a1")
    clients.started = threading.Event()
    clients.release = threading.Event()
    barrier = threading.Barrier(8)

    def request_song():
        with app.test_client() as client:
            barrier.wait(timeout=5)
            return client.get("/song/s1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(request_song) for _ in range(8)]
        try:
            assert clients.started.wait(5)
        finally:
            clients.release.set()
        responses = [future.result(timeout=5) for future in futures]
    assert all(response.status_code == 200 for response in responses)
    assert Counter(response.headers["X-Cache"] for response in responses) == {"miss": 1, "hit": 7}
    assert clients.calls[("get_song", "s1")] == 1


def test_song_cache_cannot_outlive_playback_urls(settings_factory, monkeypatch):
    clients = MetadataClients()
    clients.payloads["get_song"]["streamingData"] = {
        "expiresInSeconds": "180", "adaptiveFormats": [{"url": "https://example.test/audio"}],
    }
    app = create_app(settings_obj=settings_factory(CACHE_JITTER_PCT="0.2"), clients_obj=clients)
    now = [1000]
    monkeypatch.setattr("cache_layer.time.time", lambda: now[0])
    monkeypatch.setattr("cache_layer.random.uniform", lambda low, high: high)
    with app.test_client() as client:
        first = client.get("/song/s1")
        assert first.status_code == 200
        assert first.get_json()["streamingData"] == clients.payloads["get_song"]["streamingData"]
        service = app.extensions["ytmusic_hot_service"]
        cache = app.extensions["ytmusic_cache_layer"]
        envelope = cache.get_envelope(service._subcache_key("song", {"song_id": "s1"})).envelope
        assert envelope["fresh_until"] < envelope["stale_until"] <= 1120
        assert client.get("/song/s1").headers["X-Cache"] == "hit"
        now[0] = 1121
        clients.failures.add("get_song")
        assert client.get("/song/s1").status_code == 500


@pytest.mark.parametrize("streaming_data", [{}, {"expiresInSeconds": "invalid"}, {"expiresInSeconds": "30"}])
def test_song_playback_without_usable_lifetime_is_not_cached(metadata_app, streaming_data):
    app, clients = metadata_app
    clients.payloads["get_song"]["streamingData"] = streaming_data
    with app.test_client() as client:
        assert client.get("/song/s1").headers["X-Cache"] == "miss"
        assert client.get("/song/s1").headers["X-Cache"] == "miss"
    assert clients.calls[("get_song", "s1")] == 2
