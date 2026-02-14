class ErrorRouteFakeClients:
    def call_ytmusic(self, method_name, *args, **kwargs):
        if method_name == "search":
            raise RuntimeError("forced search failure")
        if method_name == "get_search_suggestions":
            return []
        if method_name == "get_song":
            return {"videoDetails": {}}
        return {}

    def http_get(self, *args, **kwargs):
        raise RuntimeError("http_get not used in this test")


class StableHotService:
    def trending(self, country, limit_value):
        return [{"title": "t"}], "miss", False

    def recommendations(self, song_ids):
        return [{"videoId": "v"}], "miss", False

    def mix(self, artist_ids, limit_value):
        return [{"title": "mix"}], "miss", False

    async def billboard(self):
        return {"data": [], "metadata": {}}, "miss", False


def test_invalid_json_on_songs_returns_400(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    app = create_app(
        settings_obj=settings,
        clients_obj=ErrorRouteFakeClients(),
        hot_service_obj=StableHotService(),
    )
    client = app.test_client()

    response = client.post("/songs", data="not-json", content_type="application/json")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_error_envelope_for_400_404_500_and_429(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="true")
    app = create_app(
        settings_obj=settings,
        clients_obj=ErrorRouteFakeClients(),
        hot_service_obj=StableHotService(),
    )
    client = app.test_client()

    bad_request = client.get("/search")
    assert bad_request.status_code == 400
    assert "error" in bad_request.get_json()

    not_found = client.get("/unknown-endpoint")
    assert not_found.status_code == 404
    assert "error" in not_found.get_json()

    internal = client.get("/search?query=test")
    assert internal.status_code == 500
    assert "error" in internal.get_json()

    too_many = None
    for _ in range(31):
        too_many = client.get("/mix?artists=a1")
    assert too_many is not None
    assert too_many.status_code == 429
    assert "error" in too_many.get_json()


def test_redis_fail_open_falls_back_and_api_still_serves(settings_factory):
    from app import create_app

    settings = settings_factory(
        CACHE_BACKEND="redis",
        REDIS_URL="redis://127.0.0.1:6399/0",
        CACHE_REDIS_CONNECT_TIMEOUT="0.1",
        CACHE_FAIL_OPEN="true",
        ENABLE_RATE_LIMITS="false",
    )
    app = create_app(
        settings_obj=settings,
        clients_obj=ErrorRouteFakeClients(),
        hot_service_obj=StableHotService(),
    )
    client = app.test_client()

    health = client.get("/health")
    assert health.status_code == 200
    body = health.get_json()
    assert body["cache"]["backend"] == "simple"

    mix = client.get("/mix?artists=a1")
    assert mix.status_code == 200
