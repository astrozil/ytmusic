import asyncio
import time
from cache_layer import CacheLayer, key_for_billboard
from services.hot_endpoints import HotEndpointsService


class ServiceFakeClients:
    def call_ytmusic(self, method_name, *args, **kwargs):
        if method_name == "get_charts":
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
            if seed == "bad-seed":
                raise RuntimeError("seed failed")
            return {
                "tracks": [
                    {"videoId": "shared-track", "title": "shared"},
                    {"videoId": f"{seed}-track", "title": f"track-{seed}"},
                ]
            }

        if method_name == "get_artist":
            return {"songs": {"results": []}}

        if method_name in {"get_playlist", "get_artist_albums", "get_album"}:
            return {}

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
