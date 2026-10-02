import copy
import hashlib
import json

from app import create_app
from cache_layer import stable_sha256


class ResponseClients:
    def __init__(self):
        self.calls = 0
        self.payload = {
            "zeta": {"z": 1, "a": 2}, "name": "Björk", "alpha": [2, 1],
            "thumbnails": [{"url": "https://lh3.googleusercontent.com/art=w120-h120", "width": 120, "height": 120}],
        }

    def call_ytmusic(self, method, identifier, **kwargs):
        self.calls += 1
        return self.payload


def test_response_json_preserves_values_and_arrays_without_sorting_object_keys(settings_factory):
    clients = ResponseClients()
    original = copy.deepcopy(clients.payload)
    app = create_app(settings_obj=settings_factory(), clients_obj=clients)
    assert app.json.sort_keys is False
    with app.test_client() as client:
        first = client.get("/artist/artist")
        second = client.get("/artist/artist")
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Cache"] == "miss"
    assert second.headers["X-Cache"] == "hit"
    body = first.get_json()
    assert second.get_json() == body
    assert list(body) == ["zeta", "name", "alpha", "thumbnails"]
    assert list(body["zeta"]) == ["z", "a"]
    assert body["name"] == "Björk"
    assert body["alpha"] == [2, 1]
    assert len(body["thumbnails"]) == 2
    assert clients.payload == original
    assert clients.calls == 1
    service = app.extensions["ytmusic_hot_service"]
    key = service._subcache_key("artist", {"artist_id": "artist"})
    assert app.extensions["ytmusic_cache_layer"].get_envelope(key).payload == original


def test_cache_hashes_still_sort_keys_independently_of_response_json(settings_factory):
    app = create_app(settings_obj=settings_factory())
    first = {"z": {"z": 1, "a": 2}, "a": [2, 1], "artist": "Björk"}
    reordered = {"artist": "Björk", "a": [2, 1], "z": {"a": 2, "z": 1}}
    assert app.json.dumps(first) != app.json.dumps(reordered)
    expected = hashlib.sha256(json.dumps(
        first, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    assert stable_sha256(first) == stable_sha256(reordered) == expected
