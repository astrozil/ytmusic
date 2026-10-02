import asyncio
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from cache_layer import CacheLayer, key_for_billboard, key_for_trending_country
from services.hot_endpoints import HotEndpointsService


class ServiceFakeClients:
    def __init__(self, sleep_get_charts=0.0):
        self.sleep_get_charts = sleep_get_charts
        self.method_counts = defaultdict(int)
        self.watch_playlist_counts = defaultdict(int)
        self.artist_counts = defaultdict(int)
        self.lock = threading.Lock()

    def call_ytmusic(self, method_name, *args, **kwargs):
        with self.lock:
            self.method_counts[method_name] += 1

        if method_name == "get_charts":
            if self.sleep_get_charts > 0:
                time.sleep(self.sleep_get_charts)
            items = [
                {"title": f"title-{i}", "artists": [{"name": "artist-a"}]}
                for i in range(100)
            ]
            return {"videos": {"items": items}}

        if method_name == "search":
            query = args[0]
            filter_value = kwargs.get("filter")
            if filter_value == "artists":
                return [{"browseId": f"artist-{query}"}]
            return [{"videoId": f"video-{query}", "title": query}]

        if method_name == "get_watch_playlist":
            seed = args[0]
            with self.lock:
                self.watch_playlist_counts[seed] += 1
            if seed == "bad-seed":
                raise RuntimeError("seed failed")
            return {
                "tracks": [
                    {"videoId": "shared-track", "title": "shared"},
                    {"videoId": f"{seed}-track", "title": f"track-{seed}"},
                ]
            }

        if method_name == "get_song":
            song_id = args[0]
            return {
                "videoDetails": {
                    "author": f"artist-{song_id}",
                    "channelId": f"channel-{song_id}",
                    "lengthSeconds": "120",
                    "thumbnail": {"thumbnails": []},
                    "title": f"title-{song_id}",
                    "videoId": song_id,
                    "isLive": False,
                },
                "microformat": {
                    "microformatDataRenderer": {
                        "category": "Music",
                    }
                },
                "musicAnalytics": {"feedbackTokens": {"add": "add-token", "remove": "remove-token"}},
            }

        if method_name == "get_artist":
            artist_id = args[0]
            with self.lock:
                self.artist_counts[artist_id] += 1
            return {
                "songs": {
                    "results": [
                        {
                            "title": f"{artist_id}-song",
                            "videoId": f"{artist_id}-song-id",
                            "artists": [{"id": artist_id, "name": artist_id}],
                            "album": None,
                            "duration": "3:00",
                            "thumbnails": [],
                        }
                    ]
                },
                "albums": {"params": f"params-{artist_id}"},
                "sections": [
                    {
                        "title": "Albums",
                        "items": [{"browseId": f"album-{artist_id}-1"}],
                    },
                    {
                        "title": "Singles",
                        "items": [{"browseId": f"album-{artist_id}-1"}],
                    },
                ],
            }

        if method_name == "get_playlist":
            playlist_id = args[0]
            return {
                "tracks": [
                    {
                        "title": f"{playlist_id}-track",
                        "videoId": f"{playlist_id}-track-id",
                        "artists": [{"id": "artist-a", "name": "artist-a"}],
                        "duration": "3:00",
                        "thumbnails": [],
                    }
                ]
            }

        if method_name == "get_artist_albums":
            artist_id = args[0]
            return [{"browseId": f"album-{artist_id}-1"}]

        if method_name == "get_album":
            album_id = args[0]
            return {
                "title": f"title-{album_id}",
                "thumbnails": [],
                "artists": [{"id": "artist-a", "name": "artist-a"}],
                "tracks": [
                    {
                        "title": f"{album_id}-track",
                        "videoId": f"{album_id}-track-id",
                        "artists": [{"id": "artist-a", "name": "artist-a"}],
                        "duration": "3:00",
                        "thumbnails": [],
                    }
                ],
            }

        raise RuntimeError(f"Unexpected ytmusic call: {method_name}")


class RouteFakeClients:
    def call_ytmusic(self, method_name, *args, **kwargs):
        if method_name == "search":
            return []
        if method_name == "get_search_suggestions":
            return []
        return {}

    def http_get(self, *args, **kwargs):
        raise RuntimeError("http_get not expected in this test")


class RouteFakeHotService:
    def trending(self, country, limit_value):
        return [{"title": "t"}], "miss", False

    def recommendations(self, song_ids):
        return [{"videoId": "v1"}], "miss", False

    def mix(self, artist_ids, limit_value):
        return (
            [
                {
                    "title": "Song",
                    "videoId": "vid-1",
                    "artists": [{"id": "a1", "name": "Artist"}],
                    "album": None,
                    "duration": "3:00",
                    "thumbnails": [],
                }
            ],
            "hit",
            False,
        )

    async def billboard(self):
        return {"data": [], "metadata": {}}, "miss", False


def test_trending_limit_capped_to_50_and_cache_normalized(settings_factory):
    settings = settings_factory()
    from flask import Flask

    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)

    payload, state, stale_flag = service.trending("us", "999")
    assert len(payload) == 50
    assert state == "miss"
    assert stale_flag is False

    payload_2, state_2, stale_flag_2 = service.trending("US", "50")
    assert len(payload_2) == 50
    assert state_2 == "hit"
    assert stale_flag_2 is False


@pytest.mark.parametrize("section", ["videos", "daily", "weekly"])
def test_trending_expands_chart_playlists_and_prefers_trending(settings_factory, section):
    from flask import Flask

    class PlaylistChartClients(ServiceFakeClients):
        def __init__(self):
            super().__init__()
            self.playlists_requested = []

        def call_ytmusic(self, method_name, *args, **kwargs):
            if method_name == "get_charts":
                return {section: [
                    {"title": "Top 100 Live Performances", "playlistId": "live"},
                    {"title": "Daily Top Music Videos", "playlistId": "daily"},
                    {"title": "Trending 20 United States", "playlistId": "trending"},
                ]}
            if method_name == "get_playlist":
                self.playlists_requested.append(args[0])
                return {"tracks": [None, {"title": "Unavailable"},
                    {"videoId": "first", "title": "First"},
                    {"videoId": "second", "title": "Second"},
                ]}
            if method_name == "search":
                return []
            return super().call_ytmusic(method_name, *args, **kwargs)

    flask_app = Flask(__name__)
    settings = settings_factory()
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    clients = PlaylistChartClients()
    service = HotEndpointsService(clients, cache_layer, settings, logger=flask_app.logger)

    payload, state, _ = service.trending("US", "1")
    assert [track["videoId"] for track in payload] == ["first"]
    assert clients.playlists_requested == ["trending"]
    assert state == "miss"
    payload, _, _ = service.trending("US", "2")
    assert [track["videoId"] for track in payload] == ["first", "second"]
    assert clients.playlists_requested == ["trending"]


def test_trending_playlist_failure_preserves_stale_tracks(settings_factory):
    from flask import Flask

    class FailingPlaylistClients(ServiceFakeClients):
        def call_ytmusic(self, method_name, *args, **kwargs):
            if method_name == "get_charts":
                return {"videos": [{"title": "Trending", "playlistId": "broken"}]}
            if method_name == "get_playlist":
                raise RuntimeError("playlist unavailable")
            return super().call_ytmusic(method_name, *args, **kwargs)

    flask_app = Flask(__name__)
    settings = settings_factory()
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(FailingPlaylistClients(), cache_layer, settings, logger=flask_app.logger)
    stale_tracks = [{
        "videoId": "cached", "title": "Cached song",
        "artists": [{"id": "a1", "name": "Artist"}], "duration": "3:00",
        "thumbnails": [{"url": "https://example.com/cover.jpg"}],
    }]
    now = time.time()
    cache_layer.cache_set_safe(key_for_trending_country("US"), {
        "payload": stale_tracks, "fetched_at": now - 120,
        "fresh_until": now - 60, "stale_until": now + 120,
    }, timeout=120)

    payload, state, stale_flag = service.trending("US", "1")
    assert payload == stale_tracks
    assert state == "stale"
    assert stale_flag is True


class TrendingMetadataClients(ServiceFakeClients):
    def __init__(self, complete=True, fail_search=False):
        super().__init__(sleep_get_charts=0.05)
        self.complete = complete
        self.fail_search = fail_search

    @staticmethod
    def track(index):
        return {
            "videoId": f"chart-{index}", "title": f"Song {index}",
            "artists": [{"id": "artist-a", "name": "Artist A"}],
            "album": {"id": "album-a", "name": "Album A"}, "duration": "3:00",
            "thumbnails": [{"url": "https://example.com/cover.jpg", "width": 544, "height": 544}],
            "isExplicit": False, "rank": index + 1,
        }

    def call_ytmusic(self, method_name, *args, **kwargs):
        if method_name == "get_charts":
            with self.lock:
                self.method_counts[method_name] += 1
            time.sleep(self.sleep_get_charts)
            tracks = [self.track(index) for index in range(60)]
            if not self.complete:
                for track in tracks:
                    track.pop("duration")
            return {"videos": {"items": tracks}}
        if method_name == "search":
            with self.lock:
                self.method_counts[method_name] += 1
            if self.fail_search:
                raise RuntimeError("temporary search failure")
            return [{
                "videoId": "different-version", "title": "Different title",
                "artists": [{"id": "other", "name": "Other artist"}],
                "duration": "3:01", "thumbnails": [],
            }]
        return super().call_ytmusic(method_name, *args, **kwargs)


def trending_metadata_service(settings_factory, **client_options):
    from flask import Flask

    app = Flask(__name__)
    clients = TrendingMetadataClients(**client_options)
    layer = CacheLayer(app, settings_factory(), logger=app.logger)
    return HotEndpointsService(clients, layer, layer.settings, logger=app.logger), clients


def test_complete_trending_tracks_reuse_country_cache_without_searches(settings_factory):
    service, clients = trending_metadata_service(settings_factory)
    first, state, _ = service.trending(" us ", "20")
    smaller, second_state, _ = service.trending("US", "10")
    larger, _, _ = service.trending("US", "999")

    assert first == [clients.track(index) for index in range(20)]
    assert smaller == first[:10]
    assert len(larger) == 50
    assert state == "miss" and second_state == "hit"
    assert clients.method_counts == {"get_charts": 1}
    service.trending("CA", "1")
    assert clients.method_counts["get_charts"] == 2


def test_trending_enriches_only_requested_tracks_and_preserves_chart_metadata(settings_factory):
    service, clients = trending_metadata_service(settings_factory, complete=False)
    first, first_state, _ = service.trending("US", "2")
    assert first_state == "miss"
    assert clients.method_counts["search"] == 2
    assert first[0] == {**clients.track(0), "duration": "3:01"}
    _, cached_state, _ = service.trending("US", "1")
    assert cached_state == "hit"
    assert clients.method_counts["search"] == 2
    _, larger_state, _ = service.trending("US", "3")
    assert larger_state == "miss"
    assert clients.method_counts == {"get_charts": 1, "search": 3}
    # Shared enrichment survives a chart refresh or a different country.
    service.trending("CA", "2")
    assert clients.method_counts == {"get_charts": 2, "search": 3}
    cached = service.cache_layer.get_envelope(key_for_trending_country("US"))
    assert "duration" not in cached.payload[0]


def test_trending_mixed_limit_concurrency_shares_chart_and_enrichment(settings_factory):
    service, clients = trending_metadata_service(settings_factory, complete=False)
    limits = [1, 3, 2, 3, 1, 2]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda limit: service.trending("US", str(limit)), limits))
    assert clients.method_counts == {"get_charts": 1, "search": 3}
    for limit, (payload, _, _) in zip(limits, results):
        assert [track["videoId"] for track in payload] == [f"chart-{index}" for index in range(limit)]


def test_failed_trending_enrichment_falls_back_without_caching_failure(settings_factory):
    service, clients = trending_metadata_service(settings_factory, complete=False, fail_search=True)
    fallback, _, _ = service.trending("US", "1")
    assert fallback[0]["videoId"] == "chart-0"
    assert "duration" not in fallback[0]
    clients.fail_search = False
    recovered, _, _ = service.trending("US", "1")
    assert recovered[0]["duration"] == "3:01"
    service.trending("US", "1")
    assert clients.method_counts == {"get_charts": 1, "search": 2}


def test_stale_trending_enrichment_marks_response_stale(settings_factory):
    service, clients = trending_metadata_service(settings_factory, complete=False)
    expected, _, _ = service.trending("US", "1")
    key = service._subcache_key("trending_enrichment", {
        "video_id": "chart-0", "query": "song 0 artist a",
    })
    envelope = service.cache_layer.get_envelope(key).envelope
    envelope["fresh_until"] = time.time() - 1
    service.cache_layer.cache_set_safe(key, envelope, timeout=120)
    clients.fail_search = True

    payload, state, stale = service.trending("US", "1")
    assert payload == expected
    assert state == "stale" and stale is True
    assert service.cache_layer.headers_for_state(state, stale_fallback=stale)["X-Data-Stale"] == "1"


@pytest.mark.parametrize("search_result", [[], [{}], [None]])
def test_empty_trending_matches_remain_retryable(settings_factory, monkeypatch, search_result):
    service, clients = trending_metadata_service(settings_factory, complete=False)
    original_call = clients.call_ytmusic
    search_calls = []

    def lookup(method, *args, **kwargs):
        if method == "search":
            search_calls.append(args)
            return search_result
        return original_call(method, *args, **kwargs)

    monkeypatch.setattr(clients, "call_ytmusic", lookup)
    first, _, _ = service.trending("US", "1")
    second, _, _ = service.trending("US", "1")
    assert first == second
    assert first[0]["videoId"] == "chart-0"
    assert len(search_calls) == 2


def test_trending_route_preserves_order_quality_and_cache_headers(settings_factory):
    from app import create_app

    clients = TrendingMetadataClients()
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        first = client.get("/trending?country=us&limit=2")
        second = client.get("/trending?country=US&limit=1")
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Cache"] == "miss"
    assert second.headers["X-Cache"] == "hit"
    assert first.get_json() == [clients.track(0), clients.track(1)]
    assert second.get_json() == [clients.track(0)]
    assert clients.method_counts == {"get_charts": 1}


def test_singleflight_prevents_duplicate_trending_fetches(settings_factory):
    settings = settings_factory()
    from flask import Flask

    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients(sleep_get_charts=0.2)
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    futures = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        for _ in range(6):
            futures.append(executor.submit(service.trending, "US", "1"))

    results = [future.result() for future in as_completed(futures)]
    assert fake_clients.method_counts["get_charts"] == 1
    assert len(results) == 6
    for payload, _, _ in results:
        assert len(payload) == 1


def test_distributed_singleflight_waits_for_leader_result(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)
    service._distributed_singleflight_enabled = True
    service._distributed_singleflight_client = object()

    def _never_fetch():
        raise AssertionError("fetch_fn should not run for waiting follower")

    service._try_acquire_distributed_lock = lambda cache_key, token: False
    service._wait_for_distributed_refresh = (
        lambda cache_key, stale_payload=None: ({"value": "leader"}, "hit", False)
    )

    payload, state, stale_flag = service._with_cache_sync("distributed:key", 60, 120, _never_fetch)

    assert payload == {"value": "leader"}
    assert state == "hit"
    assert stale_flag is False


def test_distributed_singleflight_releases_lock_when_leader_fetches(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)
    service._distributed_singleflight_enabled = True
    service._distributed_singleflight_client = object()

    release_calls = []
    service._try_acquire_distributed_lock = lambda cache_key, token: True
    service._release_distributed_lock = lambda cache_key, token: release_calls.append((cache_key, token))

    payload, state, stale_flag = service._with_cache_sync(
        "distributed:leader:key",
        60,
        120,
        lambda: {"ok": True},
    )

    assert payload == {"ok": True}
    assert state == "miss"
    assert stale_flag is False
    assert len(release_calls) == 1
    assert release_calls[0][0] == "distributed:leader:key"


def test_distributed_singleflight_async_waits_for_leader_result(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)
    service._distributed_singleflight_enabled = True
    service._distributed_singleflight_client = object()

    async def _never_fetch():
        raise AssertionError("fetch_coro should not run for waiting follower")

    service._try_acquire_distributed_lock = lambda cache_key, token: False
    service._wait_for_distributed_refresh = (
        lambda cache_key, stale_payload=None: ({"value": "leader-async"}, "hit", False)
    )

    payload, state, stale_flag = asyncio.run(
        service._with_cache_async("distributed:async:key", 60, 120, _never_fetch)
    )

    assert payload == {"value": "leader-async"}
    assert state == "hit"
    assert stale_flag is False


@pytest.mark.parametrize("include_browse_id", [True, False])
def test_mix_release_lookup_uses_section_browse_id_and_cache(settings_factory, include_browse_id):
    from flask import Flask

    class ReleaseClients(ServiceFakeClients):
        def __init__(self):
            super().__init__()
            self.release_calls = []

        def call_ytmusic(self, method_name, *args, **kwargs):
            if method_name == "get_artist":
                return {
                    section: {
                        "params": section,
                        **({"browseId": "MPADartist-a"} if include_browse_id else {}),
                    }
                    for section in ["albums", "singles"]
                }
            if method_name == "get_artist_albums":
                self.release_calls.append(args)
            return super().call_ytmusic(method_name, *args, **kwargs)

    settings = settings_factory()
    app = Flask(__name__)
    clients = ReleaseClients()
    layer = CacheLayer(app, settings, logger=app.logger)
    service = HotEndpointsService(clients, layer, settings, logger=app.logger)
    assert service._fetch_artist_songs("artist-a")
    assert service._fetch_artist_songs("artist-a")
    browse_id = "MPADartist-a" if include_browse_id else "artist-a"
    assert clients.release_calls == [(browse_id, "albums"), (browse_id, "singles")]


def test_recommendations_handles_partial_seed_failures(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)

    payload, state, stale_flag = service.recommendations(["good-1", "bad-seed", "good-2"])

    assert isinstance(payload, list)
    assert len(payload) > 0
    assert len(payload) <= 50
    assert state in {"miss", "hit"}
    assert stale_flag is False


def test_recommendations_subcache_reuses_seed_watch_playlist_calls(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients()
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    service.recommendations(["seed-a"])
    service.recommendations(["seed-a", "seed-b"])

    assert fake_clients.watch_playlist_counts["seed-a"] == 1
    assert fake_clients.watch_playlist_counts["seed-b"] == 1


def test_artist_songs_is_cached_and_reuses_album_subcache(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients()
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    first_payload, first_state, first_stale_flag = service.artist_songs("artist-a")
    second_payload, second_state, second_stale_flag = service.artist_songs("artist-a")

    assert first_payload
    assert first_state == "miss"
    assert first_stale_flag is False
    assert second_payload == first_payload
    assert second_state == "hit"
    assert second_stale_flag is False
    assert fake_clients.method_counts["get_artist"] == 1
    assert fake_clients.method_counts["get_album"] == 1

    song = first_payload[0]
    assert {"title", "videoId", "artist", "album", "duration", "year"}.issubset(song.keys())


def test_songs_parallel_path_uses_song_subcache_and_batch_cache(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients()
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    def transform(song_data):
        return {"videoId": song_data.get("videoDetails", {}).get("videoId")}

    song_ids = ["song-a", "song-a", "song-b"]
    first_payload, first_state, first_stale_flag = service.songs(song_ids, transform)
    second_payload, second_state, second_stale_flag = service.songs(song_ids, transform)

    assert first_state == "miss"
    assert first_stale_flag is False
    assert [item.get("videoId") for item in first_payload] == song_ids
    assert second_payload == first_payload
    assert second_state == "hit"
    assert second_stale_flag is False
    assert fake_clients.method_counts["get_song"] == 2


def test_billboard_artist_search_subcache_reuses_lookup(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients()
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    async def _run():
        artist_sem = asyncio.Semaphore(settings.max_concurrency_artist_lookup)
        first = await service._resolve_artist("Artist Alpha", artist_sem)
        second = await service._resolve_artist("  artist   alpha  ", artist_sem)
        return first, second

    first_result, second_result = asyncio.run(_run())

    assert fake_clients.method_counts["search"] == 1
    assert first_result["id"] == second_result["id"]


def test_mix_subcache_reuses_artist_fanout_calls(settings_factory):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    fake_clients = ServiceFakeClients()
    service = HotEndpointsService(fake_clients, cache_layer, settings, logger=flask_app.logger)

    service.mix(["artist-a"], 10)
    service.mix(["artist-a", "artist-b"], 10)

    assert fake_clients.artist_counts["artist-a"] == 1
    assert fake_clients.artist_counts["artist-b"] == 1


def test_mix_route_missing_artists_and_schema(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    app = create_app(
        settings_obj=settings,
        clients_obj=RouteFakeClients(),
        hot_service_obj=RouteFakeHotService(),
    )
    client = app.test_client()

    missing = client.get("/mix")
    assert missing.status_code == 400
    assert "error" in missing.get_json()

    ok = client.get("/mix?artists=a1")
    assert ok.status_code == 200
    body = ok.get_json()
    assert isinstance(body, list)
    assert {"title", "videoId", "artists", "album", "duration", "thumbnails"}.issubset(
        body[0].keys()
    )
    assert ok.headers["X-Cache"] == "hit"


def test_billboard_returns_stale_on_upstream_failure(settings_factory, monkeypatch):
    from flask import Flask

    settings = settings_factory()
    flask_app = Flask(__name__)
    cache_layer = CacheLayer(flask_app, settings, logger=flask_app.logger)
    service = HotEndpointsService(ServiceFakeClients(), cache_layer, settings, logger=flask_app.logger)

    week_key = service._billboard_week_key()
    cache_key = key_for_billboard(week_key)
    now = int(time.time())
    stale_payload = {"data": [{"rank": 1}], "metadata": {"total_items": 1}}
    cache_layer.cache_set_safe(
        cache_key,
        {
            "payload": stale_payload,
            "fetched_at": now - 100,
            "fresh_until": now - 10,
            "stale_until": now + 120,
        },
        timeout=120,
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("billboard failed")

    monkeypatch.setattr("services.hot_endpoints.BillboardChart", _raise)

    payload, state, stale_flag = asyncio.run(service.billboard())

    assert payload == stale_payload
    assert state == "stale"
    assert stale_flag is True
