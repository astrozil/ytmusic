import threading
from collections import Counter

from flask import Flask

from cache_layer import CacheLayer
from services.hot_endpoints import HotEndpointsService


def tracks(artist, count, prefix=None):
    return [
        {
            "videoId": f"{prefix or artist}-{index}", "title": f"Song {index}",
            "artists": [{"id": artist, "name": artist}], "duration": "3:00",
            "thumbnails": [{"url": "https://example.com/cover.jpg", "width": 544, "height": 544}],
        }
        for index in range(count)
    ]


class MixClients:
    def __init__(self, artists, albums=None, playlists=None, releases=None):
        self.artists = artists
        self.albums = albums or {}
        self.playlists = playlists or {}
        self.releases = releases or {}
        self.counts = Counter()
        self.album_requests = []
        self.lock = threading.Lock()

    def call_ytmusic(self, method, *args, **kwargs):
        with self.lock:
            self.counts[method] += 1
            if method == "get_album":
                self.album_requests.append(args[0])
        if method == "get_artist":
            return self.artists[args[0]]
        if method == "get_playlist":
            return {"tracks": self.playlists[args[0]]}
        if method == "get_artist_albums":
            releases = self.releases[args]
            if isinstance(releases, Exception):
                raise releases
            return releases
        if method == "get_album":
            album = self.albums[args[0]]
            if isinstance(album, Exception):
                raise album
            return album
        raise AssertionError(f"Unexpected upstream method: {method}")


def service_for(settings_factory, **catalog):
    app = Flask(__name__)
    settings = settings_factory()
    clients = MixClients(**catalog)
    layer = CacheLayer(app, settings, logger=app.logger)
    return HotEndpointsService(clients, layer, settings, logger=app.logger), clients


def test_small_mix_uses_existing_top_tracks_without_fetching_releases(settings_factory):
    artist = {
        "songs": {"results": tracks("a", 20), "browseId": "top-a"},
        "albums": {"browseId": "releases-a", "params": "albums"},
        "singles": {"browseId": "releases-a", "params": "singles"},
    }
    service, clients = service_for(settings_factory, artists={"a": artist})
    first, state, stale = service.mix(["a", " a ", "a"], 5)
    assert len(first) == 5
    assert len({song["videoId"] for song in first}) == 5
    assert state == "miss" and stale is False
    assert clients.counts == {"get_artist": 1}
    cached, state, _ = service.mix(["a"], 5)
    assert cached == first and state == "hit"
    assert clients.counts == {"get_artist": 1}
    assert artist["songs"]["results"] == tracks("a", 20)


def test_mix_fetches_top_playlist_only_when_inline_candidates_are_insufficient(settings_factory):
    service, clients = service_for(
        settings_factory,
        artists={"a": {"songs": {"results": tracks("a", 2), "browseId": "top-a"}}},
        playlists={"top-a": tracks("a", 30)},
    )
    payload, _, _ = service.mix(["a"], 5)
    assert len(payload) == 5
    assert clients.counts == {"get_artist": 1, "get_playlist": 1}
    assert len({song["videoId"] for song in payload}) == 5


def test_mix_samples_catalog_and_stops_after_enough_candidates(settings_factory, monkeypatch):
    monkeypatch.setattr("services.hot_endpoints.random.shuffle", lambda values: values.reverse())
    albums = {
        f"album-{index}": {"title": f"Album {index}", "tracks": tracks("a", 10, f"release-{index}")}
        for index in range(100)
    }
    releases = [{"browseId": album_id} for album_id in albums]
    service, clients = service_for(
        settings_factory,
        artists={"a": {"albums": {"browseId": "releases-a", "params": "albums"},
                       "singles": {"browseId": "releases-a", "params": "singles"}}},
        albums=albums,
        releases={("releases-a", "albums"): releases,
                  ("releases-a", "singles"): releases[:10]},
    )
    payload, _, _ = service.mix(["a"], 5)
    assert len(payload) == 5
    assert clients.album_requests == ["album-99"]
    assert clients.counts == {"get_artist": 1, "get_artist_albums": 2, "get_album": 1}
    assert {song["album"] for song in payload} == {"Album 99"}


def test_mix_skips_failed_empty_duplicate_and_unavailable_release_tracks(settings_factory, monkeypatch):
    monkeypatch.setattr("services.hot_endpoints.random.shuffle", lambda _: None)
    top = tracks("a", 1)
    releases = [{"browseId": key} for key in ("failed", "empty", "duplicate", "good", "unused")]
    service, clients = service_for(
        settings_factory,
        artists={"a": {"songs": {"results": top}, "albums": {"results": releases},
                       "singles": {"results": releases}}},
        albums={
            "failed": RuntimeError("temporary failure"),
            "empty": {"tracks": None},
            "duplicate": {"tracks": [None, {"title": "No ID"},
                                      {**tracks("a", 1, "unavailable")[0], "isAvailable": False}] + top},
            "good": {"title": "Good album", "tracks": tracks("a", 10, "good")},
        },
    )
    payload, _, _ = service.mix(["a"], 4)
    assert len(payload) == len({song["videoId"] for song in payload}) == 4
    assert clients.album_requests == ["failed", "empty", "duplicate", "good"]
    assert all(song["videoId"] == "a-0" or song["videoId"].startswith("good-") for song in payload)


def test_mix_stays_balanced_for_artists_with_sufficient_catalogs(settings_factory):
    service, clients = service_for(
        settings_factory,
        artists={artist: {"songs": {"results": tracks(artist, 20)}} for artist in ("a", "b", "c")},
    )
    payload, _, _ = service.mix(["a", "b", "c"], 10)
    counts = Counter(song["artists"][0]["id"] for song in payload)
    assert sorted(counts.values()) == [3, 3, 4]
    assert clients.counts == {"get_artist": 3}


def test_mix_uses_inline_releases_when_full_release_lookup_fails(settings_factory):
    service, clients = service_for(
        settings_factory,
        artists={"a": {"albums": {"params": "albums", "results": [{"browseId": "good"}]}}},
        releases={("a", "albums"): RuntimeError("release lookup failed")},
        albums={"good": {"title": "Good album", "tracks": tracks("a", 20)}},
    )
    payload, _, _ = service.mix(["a"], 5)
    assert len(payload) == 5
    assert clients.counts == {"get_artist": 1, "get_artist_albums": 1, "get_album": 1}


def test_mix_preserves_album_metadata_and_high_quality_thumbnail_fallback(settings_factory):
    album = {
        "title": "Catalog album", "artists": [{"id": "a", "name": "Artist A"}],
        "thumbnails": [{"url": "https://lh3.googleusercontent.com/cover=w120-h120-l90-rj",
                        "width": 120, "height": 120}],
        "tracks": [{"videoId": f"catalog-{index}", "title": f"Song {index}", "duration": "3:10"}
                   for index in range(20)],
    }
    service, _ = service_for(settings_factory,
                             artists={"a": {"albums": {"results": [{"browseId": "catalog"}]}}},
                             albums={"catalog": album})
    payload, _, _ = service.mix(["a"], 5)
    for song in payload:
        assert song["album"] == "Catalog album" and song["duration"] == "3:10"
        assert song["artists"] == album["artists"]
        assert max(thumb["width"] for thumb in song["thumbnails"]) == 544
    assert all("artists" not in track and "thumbnails" not in track for track in album["tracks"])


def test_mix_expands_remaining_artist_pool_when_other_catalogs_are_sparse(settings_factory):
    service, clients = service_for(
        settings_factory,
        artists={"a": {"songs": {"results": tracks("a", 1)}}, "b": {},
                 "c": {"songs": {"results": tracks("c", 40)}}},
    )
    payload, _, _ = service.mix(["a", "b", "c"], 10)
    assert len(payload) == 10
    assert Counter(song["artists"][0]["id"] for song in payload) == {"a": 1, "c": 9}
    assert clients.counts == {"get_artist": 3}


def test_mix_deduplicates_collaborations_and_expands_overlapping_pools(settings_factory, monkeypatch):
    monkeypatch.setattr("services.hot_endpoints.random.shuffle", lambda _: None)
    service, clients = service_for(
        settings_factory,
        artists={artist: {"songs": {"results": tracks(artist, 20, "shared")}} for artist in ("a", "b", "c")},
    )
    payload, _, _ = service.mix(["a", "b", "c"], 10)
    assert len(payload) == len({song["videoId"] for song in payload}) == 10
    assert clients.counts == {"get_artist": 3}


def test_small_limit_only_fetches_artists_that_can_be_represented(settings_factory, monkeypatch):
    monkeypatch.setattr("services.hot_endpoints.random.shuffle", lambda _: None)
    service, clients = service_for(
        settings_factory,
        artists={artist: {"songs": {"results": tracks(artist, 20)}} for artist in ("a", "b", "c")},
    )
    payload, _, _ = service.mix(["a", "b", "c"], 1)
    assert len(payload) == 1
    assert clients.counts == {"get_artist": 1}


def test_small_limit_tries_another_artist_when_sampled_catalog_is_empty(settings_factory, monkeypatch):
    monkeypatch.setattr("services.hot_endpoints.random.shuffle", lambda _: None)
    service, clients = service_for(
        settings_factory, artists={"a": {}, "b": {"songs": {"results": tracks("b", 20)}}},
    )
    payload, _, _ = service.mix(["a", "b"], 1)
    assert len(payload) == 1 and payload[0]["artists"][0]["id"] == "b"
    assert clients.counts == {"get_artist": 2}


def test_mix_returns_available_tracks_when_catalogs_are_exhausted(settings_factory):
    service, clients = service_for(settings_factory, artists={"a": {"songs": {"results": tracks("a", 2)}}})
    payload, _, _ = service.mix(["a"], 50)
    assert len(payload) == 2
    assert clients.counts == {"get_artist": 1}


def test_mix_route_preserves_schema_thumbnail_quality_and_cache_headers(settings_factory):
    from app import create_app

    clients = MixClients(artists={"a": {"songs": {"results": tracks("a", 20)}}})
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    with app.test_client() as client:
        first = client.get("/mix?artists=a,a&limit=5")
        second = client.get("/mix?artists=a&limit=5")
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Cache"] == "miss" and second.headers["X-Cache"] == "hit"
    assert first.get_json() == second.get_json()
    assert len(first.get_json()) == 5
    for song in first.get_json():
        assert {"title", "videoId", "artists", "album", "duration", "thumbnails"}.issubset(song)
        assert song["thumbnails"][0]["width"] == 544
