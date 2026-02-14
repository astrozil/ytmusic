import time

from flask import Flask

from cache_layer import CacheLayer, key_for_mix, key_for_recommendations


def test_fresh_hit_returns_payload_and_hit_header(settings_factory):
    app = Flask(__name__)
    settings = settings_factory()
    cache_layer = CacheLayer(app, settings, logger=app.logger)

    payload = {"items": [1, 2, 3]}
    cache_layer.set_envelope("test:fresh", payload, fresh_ttl=60, stale_ttl=120)

    lookup = cache_layer.get_envelope("test:fresh")
    headers = cache_layer.headers_for_state(lookup.state)

    assert lookup.state == "hit"
    assert lookup.payload == payload
    assert headers["X-Cache"] == "hit"
    assert "X-Data-Stale" not in headers


def test_stale_entry_can_be_served_with_stale_header(settings_factory):
    app = Flask(__name__)
    settings = settings_factory()
    cache_layer = CacheLayer(app, settings, logger=app.logger)

    now = int(time.time())
    stale_payload = {"items": ["stale"]}
    cache_layer.cache_set_safe(
        "test:stale",
        {
            "payload": stale_payload,
            "fetched_at": now - 120,
            "fresh_until": now - 60,
            "stale_until": now + 60,
        },
        timeout=120,
    )

    lookup = cache_layer.get_envelope("test:stale")
    cache_layer.record_stale_served()
    headers = cache_layer.headers_for_state(lookup.state, stale_fallback=True)

    assert lookup.state == "stale"
    assert lookup.payload == stale_payload
    assert headers["X-Cache"] == "stale"
    assert headers["X-Data-Stale"] == "1"
    assert cache_layer.health_snapshot()["metrics"]["stale_served"] == 1


def test_miss_without_cache_data(settings_factory):
    app = Flask(__name__)
    settings = settings_factory()
    cache_layer = CacheLayer(app, settings, logger=app.logger)

    lookup = cache_layer.get_envelope("test:missing")

    assert lookup.state == "miss"
    assert lookup.payload is None


def test_cache_key_determinism_for_reordered_ids():
    assert key_for_recommendations(["b", "a", "c"]) == key_for_recommendations(
        ["c", "b", "a"]
    )
    assert key_for_mix(["artist2", "artist1"], 50) == key_for_mix(
        ["artist1", "artist2"],
        50,
    )
