import asyncio
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from cache_layer import CacheLayer, key_for_billboard
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

    monkeypatch.setattr("services.hot_endpoints.billboard.ChartData", _raise)

    payload, state, stale_flag = asyncio.run(service.billboard())

    assert payload == stale_payload
    assert state == "stale"
    assert stale_flag is True
