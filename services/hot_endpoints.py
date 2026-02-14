import asyncio
import random
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import billboard
from redis import from_url as redis_from_url
from redis.exceptions import RedisError

from cache_layer import (
    key_for_billboard,
    key_for_mix,
    key_for_recommendations,
    key_for_trending,
    stable_sha256,
)


def format_thumbnails(thumbnails):
    if not thumbnails:
        return []
    return [
        {
            "height": thumb.get("height"),
            "width": thumb.get("width"),
            "url": thumb.get("url"),
        }
        for thumb in thumbnails
    ]


def parse_limit(value, default=50, minimum=1, maximum=50):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    parsed = min(maximum, parsed)
    return parsed


class HotEndpointsService:
    _DISTRIBUTED_UNLOCK_LUA = (
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "return redis.call('DEL', KEYS[1]) "
        "else return 0 end"
    )

    def __init__(self, clients, cache_layer, settings, logger):
        self.clients = clients
        self.cache_layer = cache_layer
        self.settings = settings
        self.logger = logger
        self._singleflight_locks = {}
        self._singleflight_lock_guard = threading.Lock()
        self._distributed_singleflight_client = None
        self._distributed_singleflight_enabled = bool(
            settings.enable_distributed_singleflight
            and str(cache_layer.status.get("backend", "")).lower() == "redis"
        )
        if self._distributed_singleflight_enabled:
            self._initialize_distributed_singleflight()

    def _get_singleflight_lock(self, cache_key):
        with self._singleflight_lock_guard:
            existing = self._singleflight_locks.get(cache_key)
            if existing is not None:
                return existing
            lock = threading.Lock()
            self._singleflight_locks[cache_key] = lock
            return lock

    def _subcache_key(self, namespace, key_data):
        return f"subcache:{namespace}:{stable_sha256(key_data)}"

    def _initialize_distributed_singleflight(self):
        try:
            self._distributed_singleflight_client = redis_from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.cache_redis_connect_timeout,
                socket_timeout=self.settings.cache_redis_connect_timeout,
            )
            self._distributed_singleflight_client.ping()
            self.logger.info("Distributed single-flight enabled")
        except (RedisError, OSError, ConnectionError, ValueError) as exc:
            self.logger.warning(
                "Distributed single-flight disabled due to Redis init failure: %s",
                exc,
            )
            self._distributed_singleflight_enabled = False
            self._distributed_singleflight_client = None

    def _distributed_lock_key(self, cache_key):
        return f"{self.settings.cache_key_prefix}singleflight:{cache_key}"

    @staticmethod
    def _new_distributed_lock_token():
        return uuid.uuid4().hex

    def _try_acquire_distributed_lock(self, cache_key, token):
        if not self._distributed_singleflight_enabled:
            return True
        if self._distributed_singleflight_client is None:
            return True

        lock_key = self._distributed_lock_key(cache_key)
        try:
            acquired = self._distributed_singleflight_client.set(
                lock_key,
                token,
                nx=True,
                ex=int(self.settings.distributed_singleflight_lock_ttl_sec),
            )
            return bool(acquired)
        except (RedisError, OSError, ConnectionError) as exc:
            self.logger.warning(
                "Distributed lock acquire failed for key '%s': %s",
                cache_key,
                exc,
            )
            return True
        except Exception as exc:
            self.logger.warning(
                "Distributed lock acquire unexpected failure for key '%s': %s",
                cache_key,
                exc,
            )
            return True

    def _release_distributed_lock(self, cache_key, token):
        if not self._distributed_singleflight_enabled:
            return
        if self._distributed_singleflight_client is None:
            return

        lock_key = self._distributed_lock_key(cache_key)
        try:
            self._distributed_singleflight_client.eval(
                self._DISTRIBUTED_UNLOCK_LUA,
                1,
                lock_key,
                token,
            )
        except (RedisError, OSError, ConnectionError) as exc:
            self.logger.warning(
                "Distributed lock release failed for key '%s': %s",
                cache_key,
                exc,
            )
        except Exception as exc:
            self.logger.warning(
                "Distributed lock release unexpected failure for key '%s': %s",
                cache_key,
                exc,
            )

    def _wait_for_distributed_refresh(self, cache_key, stale_payload=None):
        poll_interval_sec = max(
            0.01,
            float(self.settings.distributed_singleflight_poll_ms) / 1000.0,
        )
        deadline = time.monotonic() + float(
            self.settings.distributed_singleflight_wait_timeout_sec
        )
        latest_stale_payload = stale_payload

        while time.monotonic() < deadline:
            cached = self.cache_layer.get_envelope(cache_key)
            if cached.state == "hit":
                return cached.payload, "hit", False
            if cached.state == "stale":
                latest_stale_payload = cached.payload
            time.sleep(poll_interval_sec)

        if latest_stale_payload is not None and self.settings.enable_stale_fallback:
            self.logger.warning(
                "Serving stale data for key '%s' after distributed wait timeout",
                cache_key,
            )
            self.cache_layer.record_stale_served()
            return latest_stale_payload, "stale", True
        return None

    def _with_cache_sync(self, cache_key, fresh_ttl, stale_ttl, fetch_fn):
        cached = self.cache_layer.get_envelope(cache_key)
        if cached.state == "hit":
            return cached.payload, "hit", False

        stale_payload = cached.payload if cached.state == "stale" else None
        lock = self._get_singleflight_lock(cache_key)

        with lock:
            cached_after_lock = self.cache_layer.get_envelope(cache_key)
            if cached_after_lock.state == "hit":
                return cached_after_lock.payload, "hit", False
            if cached_after_lock.state == "stale":
                stale_payload = cached_after_lock.payload

            distributed_lock_token = None
            distributed_lock_acquired = True
            if self._distributed_singleflight_enabled:
                distributed_lock_token = self._new_distributed_lock_token()
                distributed_lock_acquired = self._try_acquire_distributed_lock(
                    cache_key,
                    distributed_lock_token,
                )
                if not distributed_lock_acquired:
                    waited_result = self._wait_for_distributed_refresh(
                        cache_key,
                        stale_payload=stale_payload,
                    )
                    if waited_result is not None:
                        return waited_result
                    distributed_lock_acquired = self._try_acquire_distributed_lock(
                        cache_key,
                        distributed_lock_token,
                    )
                    if not distributed_lock_acquired:
                        self.logger.warning(
                            "Distributed lock wait timed out for key '%s'; proceeding locally",
                            cache_key,
                        )

            try:
                payload = fetch_fn()
                self.cache_layer.set_envelope(cache_key, payload, fresh_ttl, stale_ttl)
                return payload, "miss", False
            except Exception as exc:
                if stale_payload is not None and self.settings.enable_stale_fallback:
                    self.logger.warning("Serving stale data for key '%s' due to: %s", cache_key, exc)
                    self.cache_layer.record_stale_served()
                    return stale_payload, "stale", True
                raise
            finally:
                if (
                    self._distributed_singleflight_enabled
                    and distributed_lock_acquired
                    and distributed_lock_token is not None
                ):
                    self._release_distributed_lock(cache_key, distributed_lock_token)

    async def _with_cache_async(self, cache_key, fresh_ttl, stale_ttl, fetch_coro):
        cached = self.cache_layer.get_envelope(cache_key)
        if cached.state == "hit":
            return cached.payload, "hit", False

        stale_payload = cached.payload if cached.state == "stale" else None
        lock = self._get_singleflight_lock(cache_key)
        await asyncio.to_thread(lock.acquire)
        try:
            cached_after_lock = self.cache_layer.get_envelope(cache_key)
            if cached_after_lock.state == "hit":
                return cached_after_lock.payload, "hit", False
            if cached_after_lock.state == "stale":
                stale_payload = cached_after_lock.payload

            distributed_lock_token = None
            distributed_lock_acquired = True
            if self._distributed_singleflight_enabled:
                distributed_lock_token = self._new_distributed_lock_token()
                distributed_lock_acquired = await asyncio.to_thread(
                    self._try_acquire_distributed_lock,
                    cache_key,
                    distributed_lock_token,
                )
                if not distributed_lock_acquired:
                    waited_result = await asyncio.to_thread(
                        self._wait_for_distributed_refresh,
                        cache_key,
                        stale_payload,
                    )
                    if waited_result is not None:
                        return waited_result
                    distributed_lock_acquired = await asyncio.to_thread(
                        self._try_acquire_distributed_lock,
                        cache_key,
                        distributed_lock_token,
                    )
                    if not distributed_lock_acquired:
                        self.logger.warning(
                            "Distributed lock wait timed out for key '%s'; proceeding locally",
                            cache_key,
                        )

            try:
                payload = await fetch_coro()
                self.cache_layer.set_envelope(cache_key, payload, fresh_ttl, stale_ttl)
                return payload, "miss", False
            except Exception as exc:
                if stale_payload is not None and self.settings.enable_stale_fallback:
                    self.logger.warning("Serving stale data for key '%s' due to: %s", cache_key, exc)
                    self.cache_layer.record_stale_served()
                    return stale_payload, "stale", True
                raise
            finally:
                if (
                    self._distributed_singleflight_enabled
                    and distributed_lock_acquired
                    and distributed_lock_token is not None
                ):
                    await asyncio.to_thread(
                        self._release_distributed_lock,
                        cache_key,
                        distributed_lock_token,
                    )
        finally:
            lock.release()

    def get_trending_video_items(self, charts, limit):
        if isinstance(charts, dict):
            raw_videos = charts.get("videos", [])
            if isinstance(raw_videos, dict):
                items = raw_videos.get("items", [])
            elif isinstance(raw_videos, list):
                items = raw_videos
            else:
                items = []
        elif isinstance(charts, list):
            items = charts
        else:
            items = []
        valid_items = [item for item in items if isinstance(item, dict)]
        return valid_items[:limit]

    def _extract_artists(self, song_like):
        if song_like.get("artists"):
            return [
                {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                for a in song_like.get("artists")
            ]
        if song_like.get("artist"):
            return [{"id": None, "name": song_like.get("artist")}]
        return []

    def _enrich_trending_song(self, video):
        if not isinstance(video, dict):
            return video

        title = video.get("title", "")
        artists = video.get("artists", [])
        artist_name = ""
        if isinstance(artists, list) and artists:
            first_artist = artists[0]
            if isinstance(first_artist, dict):
                artist_name = first_artist.get("name", "")
            elif isinstance(first_artist, str):
                artist_name = first_artist
        elif isinstance(artists, str):
            artist_name = artists

        query = f"{title} {artist_name}".strip()
        if not query:
            return video

        try:
            search_results = self.clients.call_ytmusic("search", query, filter="songs")
            return search_results[0] if isinstance(search_results, list) and search_results else video
        except Exception as exc:
            self.logger.warning("Failed to enrich trending song '%s': %s", query, exc)
            return video

    def trending(self, country, limit_value):
        limit = parse_limit(limit_value, default=50, minimum=1, maximum=50)
        normalized_country = (country or "US").strip().upper() or "US"
        cache_key = key_for_trending(normalized_country, limit)

        def fetch():
            charts = self.clients.call_ytmusic("get_charts", country=normalized_country)
            trending_video_items = self.get_trending_video_items(charts, limit)
            with ThreadPoolExecutor(max_workers=self.settings.max_workers_trending) as executor:
                futures = [executor.submit(self._enrich_trending_song, item) for item in trending_video_items]
                output = []
                for index, future in enumerate(futures):
                    try:
                        output.append(
                            future.result(timeout=self.settings.upstream_timeout_sec + 1.0)
                        )
                    except Exception as exc:
                        self.logger.warning("Trending enrichment failed at index %s: %s", index, exc)
                        output.append(trending_video_items[index])
            return output

        return self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_trending_sec,
            self.settings.cache_stale_trending_sec,
            fetch,
        )

    def _with_subcache_sync(self, namespace, key_data, fetch_fn):
        cache_key = self._subcache_key(namespace, key_data)
        payload, _, _ = self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_artist_sec,
            stale_ttl=self.settings.cache_stale_subcache_artist_sec,
            fetch_fn=fetch_fn,
        )
        return payload

    def _get_cached_watch_playlist(self, song_id):
        cache_key = self._subcache_key(
            "watch_playlist",
            {"song_id": str(song_id).strip()},
        )
        payload, _, _ = self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_seed_sec,
            stale_ttl=self.settings.cache_stale_subcache_seed_sec,
            fetch_fn=lambda: self.clients.call_ytmusic(
                "get_watch_playlist",
                song_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )
        return payload

    def _get_cached_artist(self, artist_id):
        return self._with_subcache_sync(
            "artist",
            {"artist_id": str(artist_id).strip()},
            lambda: self.clients.call_ytmusic(
                "get_artist",
                artist_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_playlist(self, playlist_id):
        return self._with_subcache_sync(
            "playlist",
            {"playlist_id": str(playlist_id).strip()},
            lambda: self.clients.call_ytmusic(
                "get_playlist",
                playlist_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_artist_albums(self, artist_id, params):
        return self._with_subcache_sync(
            "artist_albums",
            {"artist_id": str(artist_id).strip(), "params": str(params).strip()},
            lambda: self.clients.call_ytmusic(
                "get_artist_albums",
                artist_id,
                params,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_album(self, album_id):
        return self._with_subcache_sync(
            "album",
            {"album_id": str(album_id).strip()},
            lambda: self.clients.call_ytmusic(
                "get_album",
                album_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_song(self, song_id):
        cache_key = self._subcache_key(
            "song",
            {"song_id": str(song_id).strip()},
        )
        payload, _, _ = self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_song_sec,
            stale_ttl=self.settings.cache_stale_subcache_song_sec,
            fetch_fn=lambda: self.clients.call_ytmusic(
                "get_song",
                song_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )
        return payload

    @staticmethod
    def _normalize_artist_name(value):
        return " ".join(str(value or "").strip().lower().split())

    def _get_cached_artist_search_results(self, artist_name):
        normalized_artist_name = self._normalize_artist_name(artist_name)
        if not normalized_artist_name:
            return []
        return self._with_subcache_sync(
            "artist_search",
            {"artist_name": normalized_artist_name},
            lambda: self.clients.call_ytmusic(
                "search",
                str(artist_name).strip(),
                filter="artists",
                timeout=self.settings.upstream_timeout_sec,
            ),
        )

    def _fetch_recommendation_seed(self, song_id):
        return self._get_cached_watch_playlist(song_id)

    def recommendations(self, song_ids):
        cache_key = key_for_recommendations(song_ids)

        def fetch():
            all_tracks = []
            with ThreadPoolExecutor(max_workers=self.settings.max_workers_recommendations) as executor:
                futures = {
                    executor.submit(self._fetch_recommendation_seed, song_id): song_id
                    for song_id in song_ids
                }
                for future in as_completed(futures):
                    song_id = futures[future]
                    try:
                        result = future.result(timeout=self.settings.upstream_timeout_sec * 2)
                        tracks = result.get("tracks", [])
                        for track in tracks:
                            if not isinstance(track, dict):
                                continue
                            track_with_seed = dict(track)
                            track_with_seed["seedSongId"] = song_id
                            all_tracks.append(track_with_seed)
                    except Exception as exc:
                        self.logger.error("Error processing recommendation seed %s: %s", song_id, exc)

            seen = set()
            unique_tracks = []
            for track in all_tracks:
                video_id = track.get("videoId")
                if video_id and video_id not in seen:
                    seen.add(video_id)
                    track.pop("seedSongId", None)
                    unique_tracks.append(track)

            track_counts = {}
            for track in all_tracks:
                video_id = track.get("videoId")
                if video_id:
                    track_counts[video_id] = track_counts.get(video_id, 0) + 1

            frequency_groups = {}
            for track in unique_tracks:
                count = track_counts.get(track.get("videoId"), 1)
                frequency_groups.setdefault(count, []).append(track)

            for count in frequency_groups:
                random.shuffle(frequency_groups[count])

            result_tracks = []
            counts = sorted(frequency_groups.keys(), reverse=True)
            while len(result_tracks) < 50 and frequency_groups:
                weights = [c for c in counts if frequency_groups.get(c)]
                if not weights:
                    break
                selected_count = random.choices(weights, weights=weights, k=1)[0]
                if frequency_groups[selected_count]:
                    track = frequency_groups[selected_count].pop(0)
                    result_tracks.append(track)
                    if not frequency_groups[selected_count]:
                        frequency_groups.pop(selected_count, None)
                        counts.remove(selected_count)

            if len(result_tracks) < 50:
                remaining_ids = {
                    t.get("videoId") for t in unique_tracks
                } - {t.get("videoId") for t in result_tracks}
                remaining_tracks = [t for t in unique_tracks if t.get("videoId") in remaining_ids]
                random.shuffle(remaining_tracks)
                result_tracks.extend(remaining_tracks[: 50 - len(result_tracks)])

            return result_tracks[:50]

        return self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_recommendations_sec,
            self.settings.cache_stale_recommendations_sec,
            fetch,
        )

    @staticmethod
    def _extract_primary_artist_name(track):
        artists = track.get("artists", [])
        if not isinstance(artists, list) or not artists:
            return None
        first_artist = artists[0]
        if isinstance(first_artist, dict):
            return first_artist.get("name")
        if isinstance(first_artist, str):
            return first_artist
        return None

    def artist_songs(self, artist_id):
        normalized_artist_id = str(artist_id).strip()
        cache_key = f"artist_songs:{stable_sha256({'artist_id': normalized_artist_id})}"

        def fetch():
            artist_details = self._get_cached_artist(normalized_artist_id)

            release_album_ids = []
            seen_album_ids = set()
            for section in artist_details.get("sections", []):
                section_title = str(section.get("title", "")).lower()
                if not any(
                    keyword in section_title
                    for keyword in ["album", "single", "ep", "compilation"]
                ):
                    continue
                for item in section.get("items", []):
                    album_id = item.get("browseId")
                    if not album_id or album_id in seen_album_ids:
                        continue
                    seen_album_ids.add(album_id)
                    release_album_ids.append(album_id)

            album_details_by_id = {}
            if release_album_ids:
                worker_count = min(
                    max(1, self.settings.max_workers_artist_songs),
                    len(release_album_ids),
                )
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    future_to_album = {
                        executor.submit(self._get_cached_album, album_id): album_id
                        for album_id in release_album_ids
                    }
                    for future in as_completed(future_to_album):
                        album_id = future_to_album[future]
                        try:
                            album_details_by_id[album_id] = future.result(
                                timeout=self.settings.upstream_timeout_sec * 2 + 1.0
                            )
                        except Exception as exc:
                            self.logger.error("Error processing album %s: %s", album_id, exc)

            all_songs = []
            for album_id in release_album_ids:
                album_details = album_details_by_id.get(album_id)
                if not isinstance(album_details, dict):
                    continue
                for track in album_details.get("tracks", []):
                    if not isinstance(track, dict):
                        continue
                    all_songs.append(
                        {
                            "title": track.get("title"),
                            "videoId": track.get("videoId"),
                            "artist": self._extract_primary_artist_name(track),
                            "album": album_details.get("title"),
                            "duration": track.get("duration"),
                            "year": album_details.get("year"),
                        }
                    )

            seen_song_ids = set()
            unique_songs = []
            for song in all_songs:
                identifier = song.get("videoId") or song.get("title")
                if identifier and identifier not in seen_song_ids:
                    seen_song_ids.add(identifier)
                    unique_songs.append(song)

            return unique_songs

        return self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_artist_songs_sec,
            self.settings.cache_stale_artist_songs_sec,
            fetch,
        )

    def _fetch_single_song(self, song_id, transform_fn):
        try:
            song_data = self._get_cached_song(song_id)
        except Exception as exc:
            self.logger.error("Error fetching data for song ID %s: %s", song_id, exc)
            return {"error": f"Failed to fetch data for song ID {song_id}"}

        try:
            transformed_data = transform_fn(song_data)
        except Exception as exc:
            self.logger.error("Error transforming data for song ID %s: %s", song_id, exc)
            return {"error": f"Failed to transform data for song ID {song_id}"}

        if transformed_data:
            return transformed_data
        return {"error": f"Failed to transform data for song ID {song_id}"}

    def songs(self, song_ids, transform_fn):
        normalized_song_ids = [str(song_id) for song_id in song_ids]
        cache_key = f"songs_batch:{stable_sha256({'song_ids': normalized_song_ids})}"

        def fetch():
            if not song_ids:
                return []

            ordered_results = [None] * len(song_ids)
            worker_count = min(max(1, self.settings.max_workers_songs), len(song_ids))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(self._fetch_single_song, song_id, transform_fn): idx
                    for idx, song_id in enumerate(song_ids)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        ordered_results[idx] = future.result(
                            timeout=self.settings.upstream_timeout_sec * 2 + 1.0
                        )
                    except Exception as exc:
                        song_id = song_ids[idx]
                        self.logger.error(
                            "Unhandled song processing error for song ID %s: %s",
                            song_id,
                            exc,
                        )
                        ordered_results[idx] = {
                            "error": f"Failed to fetch data for song ID {song_id}"
                        }
            return ordered_results

        return self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_songs_sec,
            self.settings.cache_stale_songs_sec,
            fetch,
        )

    def _fetch_artist_songs(self, artist_id):
        artist_songs = []
        try:
            artist_info = self._get_cached_artist(artist_id)
        except Exception as exc:
            self.logger.error("Error retrieving artist %s: %s", artist_id, exc)
            return []

        if "songs" in artist_info:
            songs_data = artist_info["songs"]
            songs_browse_id = songs_data.get("browseId")
            if songs_browse_id:
                try:
                    playlist_data = self._get_cached_playlist(songs_browse_id)
                    for track in playlist_data.get("tracks", []):
                        artist_songs.append(
                            {
                                "title": track.get("title"),
                                "videoId": track.get("videoId"),
                                "artists": self._extract_artists(track),
                                "album": None,
                                "duration": track.get("duration"),
                                "thumbnails": format_thumbnails(track.get("thumbnails")),
                            }
                        )
                except Exception as exc:
                    self.logger.error(
                        "Error fetching songs playlist for artist %s: %s", artist_id, exc
                    )
            else:
                for song in songs_data.get("results", []):
                    artist_songs.append(
                        {
                            "title": song.get("title"),
                            "videoId": song.get("videoId"),
                            "artists": self._extract_artists(song),
                            "album": song.get("album"),
                            "duration": song.get("duration"),
                            "thumbnails": format_thumbnails(song.get("thumbnails")),
                        }
                    )

        for content_type in ["albums", "singles"]:
            content_data = artist_info.get(content_type, {})
            params = content_data.get("params")
            if not params:
                continue
            try:
                releases = self._get_cached_artist_albums(artist_id, params)
            except Exception as exc:
                self.logger.error("Error fetching %s for artist %s: %s", content_type, artist_id, exc)
                continue

            for release in releases:
                album_id = release.get("browseId")
                if not album_id:
                    continue
                try:
                    album_info = self._get_cached_album(album_id)
                    album_thumbnails = album_info.get("thumbnails", [])
                    for track in album_info.get("tracks", []):
                        raw_thumbnails = track.get("thumbnails") or album_thumbnails
                        artists = self._extract_artists(track)
                        if not artists and album_info.get("artists"):
                            artists = [
                                {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                                for a in album_info.get("artists")
                            ]
                        artist_songs.append(
                            {
                                "title": track.get("title"),
                                "videoId": track.get("videoId"),
                                "artists": artists,
                                "album": album_info.get("title"),
                                "duration": track.get("duration"),
                                "thumbnails": format_thumbnails(raw_thumbnails),
                            }
                        )
                except Exception as exc:
                    self.logger.error("Error fetching album %s: %s", album_id, exc)

        seen = set()
        unique_artist_songs = []
        for song in artist_songs:
            identifier = song.get("videoId") or song.get("title")
            if identifier and identifier not in seen:
                seen.add(identifier)
                unique_artist_songs.append(song)

        random.shuffle(unique_artist_songs)
        return unique_artist_songs

    @staticmethod
    def _create_balanced_mix(songs_by_artist, total_limit):
        if not songs_by_artist:
            return []

        num_artists = len(songs_by_artist)
        base_songs_per_artist = total_limit // num_artists
        extra_songs = total_limit % num_artists

        result = []
        artist_indices = {artist_id: 0 for artist_id in songs_by_artist.keys()}
        artists_list = list(songs_by_artist.keys())

        for _ in range(base_songs_per_artist):
            for artist_id in artists_list:
                songs = songs_by_artist[artist_id]
                if artist_indices[artist_id] < len(songs):
                    result.append(songs[artist_indices[artist_id]])
                    artist_indices[artist_id] += 1

        artist_idx = 0
        for _ in range(extra_songs):
            while artist_idx < len(artists_list):
                artist_id = artists_list[artist_idx]
                songs = songs_by_artist[artist_id]
                if artist_indices[artist_id] < len(songs):
                    result.append(songs[artist_indices[artist_id]])
                    artist_indices[artist_id] += 1
                    artist_idx += 1
                    break
                artist_idx += 1

        random.shuffle(result)
        return result[:total_limit]

    def mix(self, artist_ids, limit_value):
        limit = parse_limit(limit_value, default=50, minimum=1, maximum=200)
        normalized_artist_ids = [artist_id.strip() for artist_id in artist_ids if artist_id and artist_id.strip()]
        cache_key = key_for_mix(normalized_artist_ids, limit)

        def fetch():
            all_songs_by_artist = {}
            with ThreadPoolExecutor(max_workers=self.settings.max_workers_mix) as executor:
                futures = {
                    executor.submit(self._fetch_artist_songs, artist_id): artist_id
                    for artist_id in normalized_artist_ids
                }
                for future in as_completed(futures):
                    artist_id = futures[future]
                    try:
                        all_songs_by_artist[artist_id] = future.result(
                            timeout=self.settings.upstream_timeout_sec * 6
                        )
                    except Exception as exc:
                        self.logger.error("Error building mix songs for artist %s: %s", artist_id, exc)
                        all_songs_by_artist[artist_id] = []

            result = self._create_balanced_mix(all_songs_by_artist, limit)

            artist_count = defaultdict(int)
            for song in result:
                for artist in song.get("artists", []):
                    artist_name = artist.get("name", "Unknown")
                    artist_count[artist_name] += 1
            self.logger.info("Artist distribution in mix: %s", dict(artist_count))
            return result

        return self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_mix_sec,
            self.settings.cache_stale_mix_sec,
            fetch,
        )

    @staticmethod
    def _split_billboard_artists(artist_string):
        separators = [" & ", " and ", " feat. ", " featuring ", " ft. ", " with ", ", "]
        artists = [artist_string]
        for separator in separators:
            new_artists = []
            for artist in artists:
                new_artists.extend([a.strip() for a in artist.split(separator)])
            artists = new_artists
        return list(dict.fromkeys([artist for artist in artists if artist.strip()]))

    async def _resolve_artist(self, artist_name, artist_sem):
        async with artist_sem:
            try:
                search_results = await asyncio.to_thread(
                    self._get_cached_artist_search_results,
                    artist_name,
                )
                if search_results:
                    artist_data = search_results[0]
                    artist_id = artist_data.get("browseId") or artist_data.get("id")
                    return {"id": artist_id, "name": artist_name}
                return {"id": None, "name": artist_name}
            except Exception as exc:
                self.logger.error("Error fetching artist ID for %s: %s", artist_name, exc)
                return {"id": None, "name": artist_name}

    async def _parse_and_fetch_artists(self, artist_string, artist_sem):
        artist_names = self._split_billboard_artists(artist_string)
        tasks = [asyncio.create_task(self._resolve_artist(name, artist_sem)) for name in artist_names]
        return await asyncio.gather(*tasks)

    async def _fetch_billboard_song(self, entry, artist_sem):
        try:
            query = f"{entry.title} {entry.artist}"
            search_results = await asyncio.to_thread(
                self.clients.call_ytmusic,
                "search",
                query,
                filter="songs",
                timeout=self.settings.upstream_timeout_sec * 2,
            )
            best_match = search_results[0] if search_results else {}
            artists = await self._parse_and_fetch_artists(entry.artist, artist_sem)
            return {
                "rank": entry.rank,
                "title": entry.title,
                "artists": artists,
                "lastPos": entry.lastPos,
                "peakPos": entry.peakPos,
                "weeks": entry.weeks,
                "ytmusic_result": best_match,
            }
        except Exception as exc:
            self.logger.error("Error fetching details for %s: %s", entry.title, exc)
            return {
                "rank": entry.rank,
                "title": entry.title,
                "artists": [{"id": None, "name": entry.artist}],
                "lastPos": entry.lastPos,
                "peakPos": entry.peakPos,
                "weeks": entry.weeks,
                "ytmusic_result": {},
            }

    @staticmethod
    def _billboard_week_key():
        today = datetime.now()
        days_since_tuesday = (today.weekday() - 1) % 7
        current_tuesday = today - timedelta(days=days_since_tuesday)
        return current_tuesday.strftime("%Y-%m-%d")

    async def billboard(self):
        week_key = self._billboard_week_key()
        cache_key = key_for_billboard(week_key)

        async def fetch():
            chart = await asyncio.to_thread(billboard.ChartData, "hot-100")
            chart_entries = list(chart)
            billboard_sem = asyncio.Semaphore(self.settings.max_concurrency_billboard)
            artist_sem = asyncio.Semaphore(self.settings.max_concurrency_artist_lookup)

            async def process_entry(entry):
                async with billboard_sem:
                    return await self._fetch_billboard_song(entry, artist_sem)

            tasks = [asyncio.create_task(process_entry(entry)) for entry in chart_entries]
            songs = await asyncio.gather(*tasks)
            return {
                "data": songs,
                "metadata": {
                    "total_items": len(chart_entries),
                    "chart_date": getattr(chart, "date", None),
                    "last_updated": datetime.now().isoformat(),
                },
            }

        return await self._with_cache_async(
            cache_key,
            self.settings.cache_ttl_billboard_sec,
            self.settings.cache_stale_billboard_sec,
            fetch,
        )
