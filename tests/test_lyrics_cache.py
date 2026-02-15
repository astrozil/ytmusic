import time
from urllib.parse import parse_qs, urlparse


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

    def _param(self, url, params, key):
        if isinstance(params, dict) and key in params:
            return params[key]
        parsed = parse_qs(urlparse(url).query)
        values = parsed.get(key, [])
        return values[0] if values else None

    def _handle_lrclib(self, url, params):
        artist_name = self._param(url, params, "artist_name")
        if self.mode == "lrclib_success":
            return LyricsFakeResponse(
                200,
                json_body={
                    "syncedLyrics": "[00:01.00] line-1\n[00:02.00] line-2",
                    "plainLyrics": "line-1\nline-2",
                },
            )
        if self.mode == "lrclib_plain_only":
            return LyricsFakeResponse(
                200,
                json_body={"plainLyrics": "plain-line-1\nplain-line-2"},
            )
        if self.mode == "artist_primary_fallback":
            if artist_name == "The Weeknd":
                return LyricsFakeResponse(
                    200,
                    json_body={
                        "syncedLyrics": "[00:01.00] save your tears",
                        "plainLyrics": "save your tears",
                    },
                )
            return LyricsFakeResponse(404, json_body={})
        if self.mode in {"genius_success", "not_found"}:
            return LyricsFakeResponse(404, json_body={})
        raise RuntimeError(f"unexpected mode={self.mode}")

    def _handle_genius(self):
        if self.mode == "genius_success":
            return LyricsFakeResponse(200, text='{"lyrics":"line-1\\\\nline-2"}')
        return LyricsFakeResponse(404, text="")

    def _handle_lyrics_ovh(self):
        if self.mode == "lyrics_ovh_success":
            return LyricsFakeResponse(200, json_body={"lyrics": "fallback line-1"})
        return LyricsFakeResponse(404, json_body={})

    def http_get(self, url, timeout=None, **kwargs):
        params = kwargs.get("params")
        self.http_calls.append({"url": url, "params": params})
        if "lrclib.net" in url:
            return self._handle_lrclib(url, params)
        if "genius.com" in url:
            return self._handle_genius()
        if "api.lyrics.ovh" in url:
            return self._handle_lyrics_ovh()
        raise RuntimeError(f"unknown url in fake client: {url}")


def test_lyrics_success_response_is_cached(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    fake_clients = LyricsRouteFakeClients(mode="lrclib_success")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()

    first = client.get("/lyrics?title=Song%20A&artist=Artist%20A")
    assert first.status_code == 200
    first_body = first.get_json()
    assert first_body["song_title"] == "Song A"
    assert first_body["artist"] == "Artist A"
    assert first_body["lyrics"] == "line-1\nline-2"
    assert first_body["syncedLyrics"] == "[00:01.00] line-1\n[00:02.00] line-2"
    assert first_body["isSynced"] is True
    assert first_body["source"] == "lrclib"
    assert first_body["normalizedArtist"] == "Artist A"

    second = client.get("/lyrics?title=Song%20A&artist=Artist%20A")
    assert second.status_code == 200
    assert second.get_json() == first_body
    assert len(fake_clients.http_calls) == 1


def test_lyrics_plain_only_payload_is_supported(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    fake_clients = LyricsRouteFakeClients(mode="lrclib_plain_only")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()

    response = client.get("/lyrics?title=Song%20B&artist=Artist%20B")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["lyrics"] == "plain-line-1\nplain-line-2"
    assert payload["syncedLyrics"] is None
    assert payload["isSynced"] is False
    assert payload["source"] == "lrclib"


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
    assert len(fake_clients.http_calls) == 3

    second = client.get("/lyrics?title=Missing&artist=Nobody")
    assert second.status_code == 404
    assert len(fake_clients.http_calls) == 3


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


def test_lyrics_artist_normalization_falls_back_to_primary_artist(settings_factory):
    from app import create_app

    settings = settings_factory(ENABLE_RATE_LIMITS="false")
    fake_clients = LyricsRouteFakeClients(mode="artist_primary_fallback")
    app = create_app(settings_obj=settings, clients_obj=fake_clients)
    client = app.test_client()

    response = client.get(
        "/lyrics?title=Save%20Your%20Tears&artist=The%20Weeknd,%20Ariana%20Grande"
    )
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["lyrics"] == "save your tears"
    assert payload["source"] == "lrclib"
    assert payload["normalizedArtist"] == "The Weeknd"

    lrclib_calls = [
        call for call in fake_clients.http_calls if "lrclib.net" in call["url"]
    ]
    assert len(lrclib_calls) == 2
    assert lrclib_calls[0]["params"]["artist_name"] == "The Weeknd, Ariana Grande"
    assert lrclib_calls[1]["params"]["artist_name"] == "The Weeknd"
