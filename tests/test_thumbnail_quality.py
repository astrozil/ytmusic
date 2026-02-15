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
