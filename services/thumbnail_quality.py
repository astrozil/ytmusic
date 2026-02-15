import copy
import re
from urllib.parse import urlparse


TARGET_MAX_DIMENSION = 544
_GOOGLE_SIZE_PATTERN = re.compile(r"w(\d+)-h(\d+)")


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_googleusercontent_url(url):
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return "googleusercontent.com" in host


def _parse_dimensions_from_url(url):
    if not isinstance(url, str):
        return 0, 0
    match = _GOOGLE_SIZE_PATTERN.search(url)
    if not match:
        return 0, 0
    return _safe_int(match.group(1)), _safe_int(match.group(2))


def _thumbnail_area(thumb):
    width = _safe_int(thumb.get("width"))
    height = _safe_int(thumb.get("height"))
    if width <= 0 or height <= 0:
        inferred_width, inferred_height = _parse_dimensions_from_url(thumb.get("url", ""))
        if width <= 0:
            width = inferred_width
        if height <= 0:
            height = inferred_height
    return width * height, width, height


def upscale_googleusercontent_url(url, width, height):
    if not _is_googleusercontent_url(url):
        return url
    safe_width = max(1, _safe_int(width, 1))
    safe_height = max(1, _safe_int(height, 1))
    replacement = f"w{safe_width}-h{safe_height}"
    if _GOOGLE_SIZE_PATTERN.search(url):
        return _GOOGLE_SIZE_PATTERN.sub(replacement, url, count=1)
    return url


def _normalize_thumbnail_item(item):
    if not isinstance(item, dict):
        return None

    url = str(item.get("url") or "").strip()
    if not url:
        return None

    width = _safe_int(item.get("width"))
    height = _safe_int(item.get("height"))

    inferred_width, inferred_height = _parse_dimensions_from_url(url)
    if width <= 0:
        width = inferred_width
    if height <= 0:
        height = inferred_height

    return {
        "url": url,
        "width": width,
        "height": height,
    }


def _fallback_thumbnails_for_video(video_id):
    safe_video_id = str(video_id or "").strip()
    if not safe_video_id:
        return []
    base = f"https://i.ytimg.com/vi/{safe_video_id}"
    return [
        {
            "url": f"{base}/mqdefault.jpg",
            "width": 320,
            "height": 180,
        },
        {
            "url": f"{base}/hqdefault.jpg",
            "width": 480,
            "height": 360,
        },
    ]


def normalize_thumbnails(thumbnails, video_id=None, target_max_dimension=TARGET_MAX_DIMENSION):
    normalized = []
    seen_urls = set()

    if isinstance(thumbnails, list):
        for item in thumbnails:
            normalized_item = _normalize_thumbnail_item(item)
            if not normalized_item:
                continue
            url = normalized_item["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            normalized.append(normalized_item)

    if not normalized:
        for fallback_item in _fallback_thumbnails_for_video(video_id):
            if fallback_item["url"] in seen_urls:
                continue
            seen_urls.add(fallback_item["url"])
            normalized.append(fallback_item)
        return normalized

    best_thumb = max(normalized, key=lambda thumb: _thumbnail_area(thumb)[0])
    _, best_width, best_height = _thumbnail_area(best_thumb)
    max_dimension = max(best_width, best_height)

    if (
        _is_googleusercontent_url(best_thumb.get("url", ""))
        and 0 < max_dimension < int(target_max_dimension)
    ):
        scale = float(target_max_dimension) / float(max_dimension)
        upscaled_width = max(1, int(round(best_width * scale)))
        upscaled_height = max(1, int(round(best_height * scale)))
        upscaled_url = upscale_googleusercontent_url(
            best_thumb.get("url", ""),
            upscaled_width,
            upscaled_height,
        )
        if upscaled_url and upscaled_url not in seen_urls:
            normalized.append(
                {
                    "url": upscaled_url,
                    "width": upscaled_width,
                    "height": upscaled_height,
                }
            )

    return normalized


def select_best_thumbnail(thumbnails, video_id=None, target_max_dimension=TARGET_MAX_DIMENSION):
    normalized = normalize_thumbnails(
        thumbnails=thumbnails,
        video_id=video_id,
        target_max_dimension=target_max_dimension,
    )
    if not normalized:
        return None
    return max(normalized, key=lambda thumb: _thumbnail_area(thumb)[0])


def normalize_single_thumbnail(thumbnail, target_max_dimension=TARGET_MAX_DIMENSION):
    if not isinstance(thumbnail, dict):
        return thumbnail
    best = select_best_thumbnail(
        [thumbnail],
        target_max_dimension=target_max_dimension,
    )
    return best if best is not None else thumbnail


def _resolve_video_id(node, inherited_video_id):
    if not isinstance(node, dict):
        return inherited_video_id
    direct_video_id = node.get("videoId")
    if isinstance(direct_video_id, str) and direct_video_id.strip():
        return direct_video_id.strip()
    return inherited_video_id


def _enhance_node(node, inherited_video_id=None, target_max_dimension=TARGET_MAX_DIMENSION):
    if isinstance(node, list):
        return [
            _enhance_node(
                item,
                inherited_video_id=inherited_video_id,
                target_max_dimension=target_max_dimension,
            )
            for item in node
        ]

    if not isinstance(node, dict):
        return node

    current_video_id = _resolve_video_id(node, inherited_video_id)

    if "thumbnails" in node:
        node["thumbnails"] = normalize_thumbnails(
            thumbnails=node.get("thumbnails"),
            video_id=current_video_id,
            target_max_dimension=target_max_dimension,
        )

    thumbnail_obj = node.get("thumbnail")
    if isinstance(thumbnail_obj, dict) and "url" in thumbnail_obj:
        node["thumbnail"] = normalize_single_thumbnail(
            thumbnail_obj,
            target_max_dimension=target_max_dimension,
        )

    for key, value in list(node.items()):
        if key == "thumbnails":
            continue
        if key == "thumbnail" and isinstance(value, dict) and "url" in value and "thumbnails" not in value:
            continue
        node[key] = _enhance_node(
            value,
            inherited_video_id=current_video_id,
            target_max_dimension=target_max_dimension,
        )

    return node


def enhance_payload_thumbnails(payload, target_max_dimension=TARGET_MAX_DIMENSION):
    copied_payload = copy.deepcopy(payload)
    return _enhance_node(copied_payload, target_max_dimension=target_max_dimension)
