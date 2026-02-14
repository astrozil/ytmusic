import atexit
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, abort, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from waitress import serve

from cache_layer import CacheLayer, stable_sha256
from clients import UpstreamClients
from errors import register_error_handlers, register_request_hooks
from services.hot_endpoints import HotEndpointsService
from services.prewarm import PrewarmManager
from settings import Settings


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def format_genius_url(artist, song_title):
    artist = re.sub(r"[^\w\s-]", "", artist).strip().replace(" ", "-")
    song_title = re.sub(r"[^\w\s-]", "", song_title).strip().replace(" ", "-")
    return f"https://genius.com/{artist}-{song_title}-lyrics"


def scrape_lyrics(clients, lyrics_url):
    try:
        logger.info("Fetching lyrics from: %s", lyrics_url)
        response = clients.http_get(lyrics_url, timeout=10)
        logger.info("Response status code: %s", response.status_code)
        if response.status_code != 200:
            logger.error("Failed to fetch lyrics: HTTP %s", response.status_code)
            return None

        lyrics_pattern = re.compile(r'"lyrics":"(.*?)"', re.DOTALL)
        match = lyrics_pattern.search(response.text)
        if not match:
            logger.warning("No lyrics found in the page")
            return None
        return match.group(1).replace("\\n", "\n").replace("\\", "")
    except Exception as exc:
        logger.error("Lyrics fetch error: %s", exc)
        return None


def normalize_lyrics_token(value):
    return " ".join(str(value or "").strip().lower().split())


def key_for_lyrics(song_title, artist_name):
    normalized_title = normalize_lyrics_token(song_title)
    normalized_artist = normalize_lyrics_token(artist_name)
    digest = stable_sha256({"title": normalized_title, "artist": normalized_artist})
    return f"lyrics:{digest}"


def key_for_lyrics_negative(song_title, artist_name):
    return f"{key_for_lyrics(song_title, artist_name)}:negative"


def lyrics_negative_backoff_ttl(settings_obj, failure_count):
    safe_failure_count = max(1, int(failure_count))
    raw_ttl = float(settings_obj.cache_ttl_lyrics_negative_base_sec) * (
        float(settings_obj.cache_lyrics_negative_backoff_factor)
        ** float(safe_failure_count - 1)
    )
    bounded_ttl = min(float(settings_obj.cache_ttl_lyrics_negative_max_sec), raw_ttl)
    return max(1, int(bounded_ttl))


def transform_song_data(song_data):
    try:
        video_details = song_data.get("videoDetails", {})
        microformat = song_data.get("microformat", {}).get("microformatDataRenderer", {})
        music_analytics = song_data.get("musicAnalytics", {})

        artists = video_details.get("author", "")
        artist_name = artists if isinstance(artists, str) else ", ".join(artists)
        artist_id = video_details.get("channelId", "")

        thumbnails = video_details.get("thumbnail", {}).get("thumbnails", [])
        formatted_thumbnails = [
            {
                "url": thumb.get("url"),
                "width": thumb.get("width"),
                "height": thumb.get("height"),
            }
            for thumb in thumbnails
        ]

        feedback_tokens = music_analytics.get("feedbackTokens", {})
        add_token = feedback_tokens.get("add", "")
        remove_token = feedback_tokens.get("remove", "")

        return {
            "album": None,
            "artists": [{"id": artist_id, "name": artist_name}],
            "category": microformat.get("category", None),
            "duration": video_details.get("lengthSeconds", "0"),
            "duration_seconds": int(video_details.get("lengthSeconds", "0")),
            "feedbackTokens": {"add": add_token, "remove": remove_token},
            "inLibrary": False,
            "isExplicit": video_details.get("isLive", False),
            "resultType": "song",
            "thumbnails": formatted_thumbnails,
            "title": video_details.get("title", ""),
            "videoId": video_details.get("videoId", ""),
            "videoType": "LIVE"
            if video_details.get("isLive", False)
            else "MUSIC_VIDEO_TYPE_ATV",
            "year": None,
        }
    except Exception as exc:
        logger.error("Error transforming song data: %s", exc)
        return None


def create_app(
    settings_obj=None,
    clients_obj=None,
    cache_layer_obj=None,
    hot_service_obj=None,
):
    settings_obj = settings_obj or Settings.from_env()
    app = Flask(__name__)

    cache_layer = cache_layer_obj or CacheLayer(app, settings_obj, logger)
    clients = clients_obj
    hot_service = hot_service_obj

    def get_clients():
        nonlocal clients
        if clients is None:
            clients = UpstreamClients(settings_obj, logger)
            app.extensions["ytmusic_clients"] = clients
        return clients

    def get_hot_service():
        nonlocal hot_service
        if hot_service is None:
            hot_service = HotEndpointsService(
                clients=get_clients(),
                cache_layer=cache_layer,
                settings=settings_obj,
                logger=logger,
            )
            app.extensions["ytmusic_hot_service"] = hot_service
        return hot_service

    limiter = None
    if settings_obj.enable_rate_limits:
        limiter = Limiter(
            key_func=get_remote_address,
            app=app,
            default_limits=[],
            storage_uri=settings_obj.rate_limit_storage_uri,
        )

    def maybe_limit(limit_value):
        def decorator(func):
            if limiter is None:
                return func
            return limiter.limit(limit_value)(func)

        return decorator

    app.extensions["ytmusic_settings"] = settings_obj
    app.extensions["ytmusic_cache_layer"] = cache_layer
    app.extensions["ytmusic_clients"] = clients
    app.extensions["ytmusic_hot_service"] = hot_service

    prewarm_manager = PrewarmManager(
        hot_service_getter=get_hot_service,
        settings=settings_obj,
        logger=logger,
    )
    app.extensions["ytmusic_prewarm_manager"] = prewarm_manager
    prewarm_manager.start()
    atexit.register(prewarm_manager.stop)

    register_request_hooks(app, logger)
    register_error_handlers(app, logger)

    @app.route("/search", methods=["GET"])
    def search():
        query = request.args.get("query")
        filter_value = request.args.get("filter")
        if not query or not isinstance(query, str):
            abort(400, description="Query parameter is required and must be a string")
        try:
            results = get_clients().call_ytmusic("search", query, filter=filter_value)
            return jsonify(results)
        except Exception as exc:
            logger.error("Error searching YTMusic: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/song/<song_id>", methods=["GET"])
    def get_song(song_id):
        if not song_id or not isinstance(song_id, str):
            abort(400, description="Song ID is required and must be a string")
        try:
            details = get_clients().call_ytmusic("get_song", song_id)
            return jsonify(details)
        except Exception as exc:
            logger.error("Error fetching song details: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/lyrics", methods=["GET"])
    def get_lyrics():
        song_title = request.args.get("title")
        artist_name = request.args.get("artist")

        if (
            not song_title
            or not artist_name
            or not isinstance(song_title, str)
            or not isinstance(artist_name, str)
        ):
            abort(400, description="Title and artist parameters are required and must be strings")

        lyrics_cache_key = key_for_lyrics(song_title, artist_name)
        lyrics_negative_key = key_for_lyrics_negative(song_title, artist_name)

        cached_lyrics = cache_layer.get_envelope(lyrics_cache_key)
        if cached_lyrics.state in {"hit", "stale"} and isinstance(cached_lyrics.payload, dict):
            payload = cached_lyrics.payload
            if payload.get("lyrics"):
                return jsonify(payload)

        negative_state = cache_layer.cache_get_safe(lyrics_negative_key)
        now_ts = int(time.time())
        if isinstance(negative_state, dict):
            next_retry_at = int(negative_state.get("next_retry_at", 0))
            if now_ts < next_retry_at:
                logger.info(
                    "Lyrics negative cache hit for '%s' by '%s' (retry_after=%s)",
                    song_title,
                    artist_name,
                    next_retry_at,
                )
                abort(404, description="Lyrics not found on Genius or fallback service")

        lyrics_url = format_genius_url(artist_name, song_title)
        lyrics = scrape_lyrics(get_clients(), lyrics_url)

        if not lyrics:
            fallback_url = f"https://api.lyrics.ovh/v1/{artist_name}/{song_title}"
            try:
                fallback_response = get_clients().http_get(fallback_url, timeout=10)
                if fallback_response.status_code == 200:
                    lyrics = fallback_response.json().get("lyrics")
                    logger.info("Fallback lyrics found for %s by %s", song_title, artist_name)
            except Exception as exc:
                logger.error("Fallback lyrics error: %s", exc)

        if not lyrics:
            previous_failures = 0
            if isinstance(negative_state, dict):
                try:
                    previous_failures = int(negative_state.get("failures", 0))
                except (TypeError, ValueError):
                    previous_failures = 0
            failure_count = max(1, previous_failures + 1)
            backoff_ttl = lyrics_negative_backoff_ttl(settings_obj, failure_count)
            cache_layer.cache_set_safe(
                lyrics_negative_key,
                {
                    "failures": failure_count,
                    "next_retry_at": now_ts + backoff_ttl,
                },
                timeout=max(backoff_ttl, settings_obj.cache_ttl_lyrics_negative_max_sec),
            )
            logger.warning("Lyrics not found for %s by %s", song_title, artist_name)
            abort(404, description="Lyrics not found on Genius or fallback service")

        payload = {"song_title": song_title, "artist": artist_name, "lyrics": lyrics}
        cache_layer.set_envelope(
            lyrics_cache_key,
            payload,
            settings_obj.cache_ttl_lyrics_sec,
            settings_obj.cache_stale_lyrics_sec,
        )
        cache_layer.cache_set_safe(
            lyrics_negative_key,
            {"failures": 0, "next_retry_at": 0},
            timeout=1,
        )
        return jsonify(payload)

    @app.route("/related/<song_id>", methods=["GET"])
    def get_related_songs(song_id):
        if not song_id or not isinstance(song_id, str):
            abort(400, description="Song ID is required and must be a string")
        try:
            watch_playlist = get_clients().call_ytmusic("get_watch_playlist", song_id)
            return jsonify(watch_playlist.get("tracks", []))
        except Exception as exc:
            logger.error("Error fetching related songs: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/artist/<artist_id>", methods=["GET"])
    def get_artist_details(artist_id):
        if not artist_id or not isinstance(artist_id, str):
            abort(400, description="Artist ID is required and must be a string")
        try:
            details = get_clients().call_ytmusic("get_artist", artist_id)
            return jsonify(details)
        except Exception as exc:
            logger.error("Error fetching artist details: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/artist/<artist_id>/songs", methods=["GET"])
    def get_artist_songs(artist_id):
        if not artist_id or not isinstance(artist_id, str):
            abort(400, description="Artist ID is required and must be a string")
        try:
            payload, _, _ = get_hot_service().artist_songs(artist_id)
            return jsonify(payload)
        except Exception as exc:
            logger.error("Error fetching artist songs: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/trending", methods=["GET"])
    @maybe_limit("60 per minute")
    def trending_songs():
        country = request.args.get("country", "US")
        limit_value = request.args.get("limit", "50")
        try:
            payload, cache_state, stale_fallback = get_hot_service().trending(
                country, limit_value
            )
            response = jsonify(payload)
            response.headers.update(
                cache_layer.headers_for_state(cache_state, stale_fallback=stale_fallback)
            )
            return response
        except Exception as exc:
            logger.error("Error fetching trending songs: %s", exc)
            abort(500, description="An error occurred while fetching trending songs")

    @app.route("/billboard", methods=["GET"])
    @maybe_limit("20 per minute")
    async def billboard_songs():
        try:
            payload, cache_state, stale_fallback = await get_hot_service().billboard()
            response = jsonify(payload)
            response.headers.update(
                cache_layer.headers_for_state(cache_state, stale_fallback=stale_fallback)
            )
            return response
        except Exception as exc:
            logger.error("Error fetching billboard songs: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/album/<album_id>", methods=["GET"])
    def get_album_details(album_id):
        if not album_id or not isinstance(album_id, str):
            abort(400, description="Album ID is required and must be a string")
        try:
            details = get_clients().call_ytmusic("get_album", album_id)
            return jsonify(details)
        except Exception as exc:
            logger.error("Error fetching album details: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/mix", methods=["GET"])
    @maybe_limit("30 per minute")
    def mix_songs():
        artist_ids_param = request.args.get("artists")
        limit_value = request.args.get("limit", "50")
        if not artist_ids_param:
            abort(400, description="artists parameter is required (comma-separated artist IDs)")
        artist_ids = [item.strip() for item in artist_ids_param.split(",")]
        try:
            payload, cache_state, stale_fallback = get_hot_service().mix(
                artist_ids, limit_value
            )
            response = jsonify(payload)
            response.headers.update(
                cache_layer.headers_for_state(cache_state, stale_fallback=stale_fallback)
            )
            return response
        except Exception as exc:
            logger.error("Error fetching mix songs: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/songs", methods=["POST"])
    def get_songs():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, description="A list of song IDs is required.")
        song_ids = data.get("song_ids", [])
        if not song_ids or not isinstance(song_ids, list):
            abort(400, description="A list of song IDs is required.")
        try:
            payload, _, _ = get_hot_service().songs(song_ids, transform_song_data)
            return jsonify(payload)
        except Exception as exc:
            logger.error("Error fetching songs: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/recommendations", methods=["POST"])
    @maybe_limit("60 per minute")
    def get_recommendations():
        data = request.get_json(silent=True)
        if not data or "song_ids" not in data:
            abort(400, description="Missing 'song_ids' in request body")
        song_ids = data["song_ids"]
        if not isinstance(song_ids, list) or len(song_ids) < 1 or len(song_ids) > 50:
            abort(400, description="song_ids must be a list containing 1 to 50 song IDs")
        try:
            payload, cache_state, stale_fallback = get_hot_service().recommendations(
                song_ids
            )
            response = jsonify(payload)
            response.headers.update(
                cache_layer.headers_for_state(cache_state, stale_fallback=stale_fallback)
            )
            return response
        except Exception as exc:
            logger.error("Error fetching recommendations: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/search_suggestions", methods=["GET"])
    def get_search_suggestions():
        query = request.args.get("query")
        if not query or not isinstance(query, str):
            abort(400, description="Query parameter is required and must be a string")
        try:
            suggestions = get_clients().call_ytmusic("get_search_suggestions", query)
            return jsonify(suggestions)
        except Exception as exc:
            logger.error("Error fetching search suggestions: %s", exc)
            abort(500, description="An error occurred while processing your request")

    @app.route("/artists", methods=["POST"])
    def get_multiple_artists():
        data = request.get_json(silent=True)
        if not data or "artist_ids" not in data:
            abort(400, description="Missing 'artist_ids' in request body")

        artist_ids = data["artist_ids"]
        if not isinstance(artist_ids, list):
            abort(400, description="artist_ids must be a list")
        if len(artist_ids) == 0:
            abort(400, description="artist_ids list cannot be empty")
        if len(artist_ids) > 50:
            abort(400, description="Maximum 50 artist IDs allowed per request")
        for artist_id in artist_ids:
            if not isinstance(artist_id, str) or not artist_id.strip():
                abort(400, description="All artist IDs must be non-empty strings")

        def fetch_artist_info(artist_id):
            try:
                artist_details = get_clients().call_ytmusic("get_artist", artist_id)
                thumbnails = artist_details.get("thumbnails", [])
                high_quality_thumbnail = None
                if thumbnails:
                    sorted_thumbnails = sorted(
                        thumbnails,
                        key=lambda thumb: (
                            thumb.get("width", 0) * thumb.get("height", 0)
                        ),
                        reverse=True,
                    )
                    high_quality_thumbnail = {
                        "url": sorted_thumbnails[0].get("url"),
                        "width": sorted_thumbnails[0].get("width"),
                        "height": sorted_thumbnails[0].get("height"),
                    }
                return {
                    "browseId": artist_id,
                    "name": artist_details.get("name"),
                    "thumbnail": high_quality_thumbnail,
                    "success": True,
                }
            except Exception as exc:
                logger.error("Error fetching artist %s: %s", artist_id, exc)
                return {
                    "browseId": artist_id,
                    "name": None,
                    "thumbnail": None,
                    "success": False,
                    "error": str(exc),
                }

        artists_data = []
        with ThreadPoolExecutor(max_workers=settings_obj.max_concurrency_artist_lookup) as executor:
            future_to_artist = {
                executor.submit(fetch_artist_info, artist_id): artist_id for artist_id in artist_ids
            }
            for future in as_completed(future_to_artist):
                try:
                    artists_data.append(future.result())
                except Exception as exc:
                    artist_id = future_to_artist[future]
                    logger.error("Unhandled artist fetch error for %s: %s", artist_id, exc)
                    artists_data.append(
                        {
                            "browseId": artist_id,
                            "name": None,
                            "thumbnail": None,
                            "success": False,
                            "error": str(exc),
                        }
                    )

        id_to_result = {result["browseId"]: result for result in artists_data}
        ordered_results = [id_to_result[artist_id] for artist_id in artist_ids]
        successful_artists = [artist for artist in ordered_results if artist["success"]]
        failed_artists = [artist for artist in ordered_results if not artist["success"]]

        for artist in successful_artists:
            artist.pop("success", None)
            artist.pop("error", None)

        response_data = {
            "artists": successful_artists,
            "total_requested": len(artist_ids),
            "total_successful": len(successful_artists),
            "total_failed": len(failed_artists),
        }
        if failed_artists:
            response_data["failed_requests"] = [
                {"browseId": artist["browseId"], "error": artist["error"]}
                for artist in failed_artists
            ]
        return jsonify(response_data)

    @app.route("/health", methods=["GET"])
    def health_check():
        return (
            jsonify(
                {
                    "status": "healthy",
                    "cache": cache_layer.health_snapshot(),
                    "rate_limits": {"enabled": settings_obj.enable_rate_limits},
                    "prewarm": prewarm_manager.snapshot(),
                }
            ),
            200,
        )

    return app


app = create_app()


if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
