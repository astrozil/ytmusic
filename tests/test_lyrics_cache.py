import time


class LyricsFakeResponse:
    def __init__(self, status_code, text="", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body or {}

    def json(self):
        return self._json_body


class LyricsRouteFakeClients:
    def __init__(self, mode):
        self.mode = mode
        self.http_calls = []

    def call_ytmusic(self, method_name, *args, **kwargs):
        if method_name in {"search", "get_search_suggestions"}:
            return []
        return {}

    def http_get(self, url, timeout=None, **kwargs):
        self.http_calls.append(url)
        if self.mode == "success":
            if "genius.com" in url:
                return LyricsFakeResponse(200, text='{"lyrics":"line-1\\\\nline-2"}')
            return LyricsFakeResponse(404, json_body={})
        if self.mode == "not_found":
            return LyricsFakeResponse(404, json_body={})
        raise RuntimeError(f"unexpected mode={self.mode}")


def test_lyrics_success_response_is_cached(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    fake_clients = LyricsRouteFakeClients(mode="success")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()

    first = client.get("/lyrics?title=Song%20A&artist=Artist%20A")
    assert first.status_code == 200
    first_body = first.get_json()
    assert first_body["lyrics"] == "line-1\nline-2"

    second = client.get("/lyrics?title=Song%20A&artist=Artist%20A")
    assert second.status_code == 200
    assert second.get_json() == first_body
    assert len(fake_clients.http_calls) == 1


def test_lyrics_negative_cache_skips_repeated_upstream_calls(settings_factory):
    from app import create_app

    settings = settings_factory(
        ENABLE_RATE_LIMITS="false",
        CACHE_TTL_LYRICS_NEGATIVE_BASE_SEC="10",
        CACHE_TTL_LYRICS_NEGATIVE_MAX_SEC="60",
        CACHE_LYRICS_NEGATIVE_BACKOFF_FACTOR="2.0",
    )
    fake_clients = LyricsRouteFakeClients(mode="not_found")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()

    first = client.get("/lyrics?title=Missing&artist=Nobody")
    assert first.status_code == 404
    assert len(fake_clients.http_calls) == 2

    second = client.get("/lyrics?title=Missing&artist=Nobody")
    assert second.status_code == 404
    assert len(fake_clients.http_calls) == 2


def test_lyrics_negative_cache_backoff_increases_failure_ttl(settings_factory):
    from app import create_app, key_for_lyrics_negative

    settings = settings_factory(
        ENABLE_RATE_LIMITS="false",
        CACHE_TTL_LYRICS_NEGATIVE_BASE_SEC="5",
        CACHE_TTL_LYRICS_NEGATIVE_MAX_SEC="60",
        CACHE_LYRICS_NEGATIVE_BACKOFF_FACTOR="2.0",
    )
    fake_clients = LyricsRouteFakeClients(mode="not_found")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()
    cache_layer = app.extensions["ytmusic_cache_layer"]

    first = client.get("/lyrics?title=Missing&artist=Nobody")
    assert first.status_code == 404

    negative_key = key_for_lyrics_negative("Missing", "Nobody")
    state_after_first = cache_layer.cache_get_safe(negative_key)
    assert isinstance(state_after_first, dict)
    assert state_after_first["failures"] == 1

    cache_layer.cache_set_safe(
        negative_key,
        {"failures": 1, "next_retry_at": 0},
        timeout=60,
    )

    now_ts = int(time.time())
    second = client.get("/lyrics?title=Missing&artist=Nobody")
    assert second.status_code == 404

    state_after_second = cache_layer.cache_get_safe(negative_key)
    assert isinstance(state_after_second, dict)
    assert state_after_second["failures"] == 2
    assert int(state_after_second["next_retry_at"]) >= now_ts + 10
