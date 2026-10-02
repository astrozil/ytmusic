import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.thumbnail_quality import (
    enhance_payload_thumbnails,
    normalize_thumbnails,
    upscale_googleusercontent_url,
)


def test_upscale_googleusercontent_url_rewrites_requested_size():
    source = (
        "https://lh3.googleusercontent.com/"
        "abc123=w120-h120-l90-rj"
    )
    upscaled = upscale_googleusercontent_url(source, 544, 544)
    assert upscaled.endswith("=w544-h544-l90-rj")


def test_normalize_thumbnails_appends_higher_quality_variant_with_ratio_preserved():
    source = [
        {
            "url": "https://lh3.googleusercontent.com/demo=w120-h60-l90-rj",
            "width": 120,
            "height": 60,
        }
    ]

    normalized = normalize_thumbnails(source, target_max_dimension=544)
    assert len(normalized) == 2

    best = normalized[-1]
    assert "w544-h272" in best["url"]
    assert best["width"] == 544
    assert best["height"] == 272


def test_normalize_thumbnails_builds_video_fallback_when_empty():
    normalized = normalize_thumbnails([], video_id="video-123")
    assert len(normalized) == 2
    assert normalized[0]["url"].endswith("/video-123/mqdefault.jpg")
    assert normalized[1]["url"].endswith("/video-123/hqdefault.jpg")


def test_enhance_payload_thumbnails_recursively_updates_nested_nodes():
    payload = {
        "videoId": "root-video",
        "thumbnails": [],
        "children": [
            {
                "videoId": "child-video",
                "thumbnails": [
                    {
                        "url": "https://lh3.googleusercontent.com/demo=w120-h120-l90-rj",
                        "width": 120,
                        "height": 120,
                    }
                ],
            }
        ],
        "artist": {
            "thumbnail": {
                "url": "https://lh3.googleusercontent.com/demo=w120-h120-l90-rj",
                "width": 120,
                "height": 120,
            }
        },
    }

    enhanced = enhance_payload_thumbnails(payload)

    assert len(enhanced["thumbnails"]) == 2
    assert any("hqdefault" in thumb["url"] for thumb in enhanced["thumbnails"])

    child_thumbnails = enhanced["children"][0]["thumbnails"]
    assert len(child_thumbnails) == 2
    assert any("w544-h544" in thumb["url"] for thumb in child_thumbnails)

    assert "w544-h544" in enhanced["artist"]["thumbnail"]["url"]

    # Ensure original payload is unchanged.
    assert payload["thumbnails"] == []


def test_enhancement_clones_all_mutable_json_fields_without_a_deepcopy(monkeypatch):
    payload = {
        "videoId": "song",
        "thumbnails": [{"url": "https://example.test/art", "width": 100, "height": 50}],
        "metadata": {"artists": [{"name": "Artist", "tags": ["pop"]}], "flag": True},
        "values": [None, False, 1, 1.5, "text"],
    }
    expected = copy.deepcopy(payload)

    def unexpected(value):
        pytest.fail("Normal JSON formatting performed a separate deepcopy")

    monkeypatch.setattr("services.thumbnail_quality.copy.deepcopy", unexpected)
    enhanced = enhance_payload_thumbnails(payload)
    assert enhanced == expected
    enhanced["metadata"]["artists"][0]["tags"].append("new")
    enhanced["thumbnails"][0]["width"] = 999
    enhanced["values"].append("new")
    assert payload == expected
    payload["metadata"]["artists"][0]["name"] = "Changed"
    assert enhanced["metadata"]["artists"][0]["name"] == "Artist"


def test_invalid_single_thumbnail_keeps_extra_fields_without_sharing_mutable_values():
    payload = {"thumbnail": {"url": "", "metadata": {"labels": ["original"]}}}
    enhanced = enhance_payload_thumbnails(payload)
    assert enhanced == payload
    enhanced["thumbnail"]["metadata"]["labels"].append("changed")
    assert payload["thumbnail"]["metadata"]["labels"] == ["original"]


def test_invalid_single_thumbnail_with_nested_thumbnails_inherits_video_id():
    payload = {
        "videoId": "parent", "thumbnail": {"url": "", "thumbnails": [], "metadata": {"ids": [1]}},
    }
    enhanced = enhance_payload_thumbnails(payload)
    assert [thumb["url"] for thumb in enhanced["thumbnail"]["thumbnails"]] == [
        "https://i.ytimg.com/vi/parent/mqdefault.jpg",
        "https://i.ytimg.com/vi/parent/hqdefault.jpg",
    ]
    enhanced["thumbnail"]["metadata"]["ids"].append(2)
    assert payload["thumbnail"] == {"url": "", "thumbnails": [], "metadata": {"ids": [1]}}


def test_shared_source_nodes_use_each_parent_video_context():
    shared = {"thumbnails": [], "labels": ["shared"]}
    payload = [{"videoId": "first", "child": shared}, {"videoId": "second", "child": shared}]
    enhanced = enhance_payload_thumbnails(payload)
    assert "/first/" in enhanced[0]["child"]["thumbnails"][0]["url"]
    assert "/second/" in enhanced[1]["child"]["thumbnails"][0]["url"]
    enhanced[0]["child"]["labels"].append("changed")
    assert enhanced[1]["child"]["labels"] == shared["labels"] == ["shared"]
    assert shared["thumbnails"] == []


def test_single_thumbnail_and_list_normalization_remain_idempotent():
    payload = {
        "thumbnail": {"url": "https://lh3.googleusercontent.com/cover=w120-h60", "width": 120, "height": 60},
        "thumbnails": [
            {"url": "https://lh3.googleusercontent.com/cover=w120-h60", "width": 120, "height": 60},
            {"url": "https://lh3.googleusercontent.com/cover=w120-h60", "width": 120, "height": 60},
            {"notUrl": "invalid"},
        ],
    }
    enhanced = enhance_payload_thumbnails(payload, target_max_dimension=400)
    assert enhanced["thumbnail"] == {
        "url": "https://lh3.googleusercontent.com/cover=w400-h200", "width": 400, "height": 200,
    }
    assert len(enhanced["thumbnails"]) == 2
    assert enhance_payload_thumbnails(enhanced, target_max_dimension=400) == enhanced
    assert len(payload["thumbnails"]) == 3


def test_opaque_mutable_values_and_tuple_contents_keep_copy_isolation():
    payload = {
        "opaque": {1, 2}, "bytes": bytearray(b"abc"),
        "tuple": ({"thumbnails": [], "ids": [1]},),
    }
    enhanced = enhance_payload_thumbnails(payload)
    assert enhanced == payload
    enhanced["opaque"].add(3)
    enhanced["bytes"][0] = ord("z")
    enhanced["tuple"][0]["ids"].append(2)
    assert payload == {"opaque": {1, 2}, "bytes": bytearray(b"abc"), "tuple": ({"thumbnails": [], "ids": [1]},)}


def test_concurrent_formatting_keeps_shared_source_and_outputs_isolated():
    payload = {"videoId": "song", "thumbnails": [], "metadata": {"tags": ["original"]}}
    original = copy.deepcopy(payload)
    with ThreadPoolExecutor(max_workers=8) as executor:
        outputs = list(executor.map(lambda _: enhance_payload_thumbnails(payload), range(32)))
    for index, output in enumerate(outputs):
        output["metadata"]["tags"].append(index)
        assert output["metadata"]["tags"] == ["original", index]
        assert len(output["thumbnails"]) == 2
    assert payload == original


@pytest.mark.parametrize("payload", [None, True, 5, 3.5, "text", [], {}])
def test_empty_payloads_and_scalar_roots_keep_their_values(payload):
    assert enhance_payload_thumbnails(payload) == payload
