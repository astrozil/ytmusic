import asyncio
import random
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from redis import from_url as redis_from_url
from redis.exceptions import RedisError

from cache_layer import (
    key_for_billboard,
    key_for_mix,
    key_for_recommendations,
    key_for_trending_country,
    stable_sha256,
)
from services.thumbnail_quality import normalize_thumbnails
from services.billboard_chart import BillboardChart


def format_thumbnails(thumbnails, video_id=None):
    return normalize_thumbnails(thumbnails, video_id=video_id)


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

    def _with_cache_sync(self, cache_key, fresh_ttl, stale_ttl, fetch_fn, ttl_resolver=None):
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
                effective_fresh, effective_stale = (
                    ttl_resolver(payload) if ttl_resolver else (fresh_ttl, stale_ttl)
                )
                if effective_fresh > 0 and effective_stale > 0:
                    self.cache_layer.set_envelope(
                        cache_key, payload, effective_fresh, effective_stale
                    )
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

    async def _with_cache_async(self, cache_key, fresh_ttl, stale_ttl, fetch_coro, ttl_resolver=None):
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
                effective_fresh, effective_stale = (
                    ttl_resolver(payload) if ttl_resolver else (fresh_ttl, stale_ttl)
                )
                if effective_fresh > 0 and effective_stale > 0:
                    self.cache_layer.set_envelope(
                        cache_key, payload, effective_fresh, effective_stale
                    )
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
            raw_videos = next(
                (charts[key] for key in ("videos", "daily", "weekly") if charts.get(key)),
                [],
            )
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
        # Current charts contain playlist cards; older responses contain tracks.
        playlists = [item for item in valid_items if item.get("playlistId") and not item.get("videoId")]
        if playlists:
            def playlist_priority(item):
                title = str(item.get("title", "")).lower()
                return next((i for i, token in enumerate(("trending", "daily", "weekly")) if token in title), 3)

            last_error = None
            for playlist in sorted(playlists, key=playlist_priority):
                try:
                    payload = self._get_cached_playlist(playlist["playlistId"])
                    tracks = [
                        track for track in payload.get("tracks", [])
                        if isinstance(track, dict) and track.get("videoId")
                    ]
                    if tracks:
                        return tracks[:limit]
                except Exception as exc:
                    last_error = exc
                    self.logger.warning("Failed to fetch chart playlist %s: %s", playlist["playlistId"], exc)
            if last_error is not None:
                raise last_error
            raise ValueError("Chart playlists returned no playable tracks")
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

    @staticmethod
    def _needs_trending_enrichment(video):
        artists = video.get("artists")
        return not (
            video.get("videoId")
            and video.get("title")
            and isinstance(artists, list)
            and artists
            and all(isinstance(artist, dict) and artist.get("name") for artist in artists)
            and (video.get("duration") or video.get("duration_seconds"))
            and any(
                isinstance(thumb, dict) and thumb.get("url")
                for thumb in video.get("thumbnails") or []
            )
        )

    def _enrich_trending_song(self, video):
        if not isinstance(video, dict) or not self._needs_trending_enrichment(video):
            return video, "hit", False

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
            return video, "hit", False

        def fetch_match():
            search_results = self.clients.call_ytmusic("search", query, filter="songs")
            if (
                not isinstance(search_results, list)
                or not search_results
                or not isinstance(search_results[0], dict)
                or not search_results[0].get("videoId")
            ):
                raise ValueError("No song metadata found")
            return search_results[0]

        cache_key = self._subcache_key("trending_enrichment", {
            "video_id": video.get("videoId"),
            "query": " ".join(query.split()).casefold(),
        })
        try:
            match, state, stale = self._with_cache_sync(
                cache_key,
                self.settings.cache_ttl_subcache_song_sec,
                self.settings.cache_stale_subcache_song_sec,
                fetch_match,
            )
            # Keep chart identity and ranking fields; fill only missing metadata.
            enriched = dict(match)
            enriched.update({
                key: value for key, value in video.items()
                if value not in (None, "", [], {})
            })
            return enriched, state, stale
        except Exception as exc:
            self.logger.warning("Failed to enrich trending song '%s': %s", query, exc)
            return video, "miss", False

    def trending(self, country, limit_value):
        limit = parse_limit(limit_value, default=50, minimum=1, maximum=50)
        normalized_country = (country or "US").strip().upper() or "US"
        cache_key = key_for_trending_country(normalized_country)

        def fetch():
            charts = self.clients.call_ytmusic("get_charts", country=normalized_country)
            return self.get_trending_video_items(charts, 50)

        tracks, cache_state, stale_fallback = self._with_cache_sync(
            cache_key,
            self.settings.cache_ttl_trending_sec,
            self.settings.cache_stale_trending_sec,
            fetch,
        )
        output = tracks[:limit]
        incomplete = [
            index for index, track in enumerate(output)
            if self._needs_trending_enrichment(track)
        ]
        if incomplete:
            worker_count = min(self.settings.max_workers_trending, len(incomplete))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    index: executor.submit(self._enrich_trending_song, output[index])
                    for index in incomplete
                }
                for index, future in futures.items():
                    try:
                        output[index], enrichment_state, enrichment_stale = future.result(
                            timeout=self.settings.upstream_timeout_sec + 1.0
                        )
                        if enrichment_stale:
                            cache_state, stale_fallback = "stale", True
                        elif enrichment_state == "miss" and cache_state == "hit":
                            cache_state = "miss"
                    except Exception as exc:
                        self.logger.warning("Trending enrichment failed at index %s: %s", index, exc)
                        if cache_state == "hit":
                            cache_state = "miss"
        return output, cache_state, stale_fallback

    def _with_subcache_sync(self, namespace, key_data, fetch_fn):
        payload, _, _ = self._with_subcache_result_sync(namespace, key_data, fetch_fn)
        return payload

    def _with_subcache_result_sync(self, namespace, key_data, fetch_fn):
        cache_key = self._subcache_key(namespace, key_data)
        return self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_artist_sec,
            stale_ttl=self.settings.cache_stale_subcache_artist_sec,
            fetch_fn=fetch_fn,
        )

    def _get_cached_watch_playlist(self, song_id):
        return self.watch_playlist(song_id)[0]

    def watch_playlist(self, song_id):
        cache_key = self._subcache_key(
            "watch_playlist",
            {"song_id": str(song_id).strip()},
        )
        return self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_seed_sec,
            stale_ttl=self.settings.cache_stale_subcache_seed_sec,
            fetch_fn=lambda: self.clients.call_ytmusic(
                "get_watch_playlist",
                song_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_artist(self, artist_id):
        return self.artist(artist_id)[0]

    def artist(self, artist_id):
        return self._with_subcache_result_sync(
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

    def _get_cached_artist_albums(self, browse_id, params):
        return self._with_subcache_sync(
            "artist_albums",
            {"browse_id": str(browse_id).strip(), "params": str(params).strip()},
            lambda: self.clients.call_ytmusic(
                "get_artist_albums",
                browse_id,
                params,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_album(self, album_id):
        return self.album(album_id)[0]

    def album(self, album_id):
        return self._with_subcache_result_sync(
            "album",
            {"album_id": str(album_id).strip()},
            lambda: self.clients.call_ytmusic(
                "get_album",
                album_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
        )

    def _get_cached_song(self, song_id):
        return self.song(song_id)[0]

    def _song_cache_ttls(self, payload):
        fresh_ttl = self.settings.cache_ttl_subcache_song_sec
        stale_ttl = self.settings.cache_stale_subcache_song_sec
        streaming_data = payload.get("streamingData") if isinstance(payload, dict) else None
        if isinstance(streaming_data, dict):
            try:
                remaining_seconds = int(streaming_data["expiresInSeconds"])
            except (KeyError, TypeError, ValueError):
                return 0, 0
            # Leave a minute for transit/clock differences and allow for TTL jitter.
            max_ttl = int((remaining_seconds - 60) / (1 + self.settings.cache_jitter_pct))
            if max_ttl < 2:
                return 0, 0
            fresh_ttl = min(fresh_ttl, max_ttl - 1)
            stale_ttl = min(stale_ttl, max_ttl)
        return fresh_ttl, stale_ttl

    def song(self, song_id):
        cache_key = self._subcache_key(
            "song",
            {"song_id": str(song_id).strip()},
        )
        return self._with_cache_sync(
            cache_key=cache_key,
            fresh_ttl=self.settings.cache_ttl_subcache_song_sec,
            stale_ttl=self.settings.cache_stale_subcache_song_sec,
            fetch_fn=lambda: self.clients.call_ytmusic(
                "get_song",
                song_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            ),
            ttl_resolver=self._song_cache_ttls,
        )

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

    def _fetch_artist_songs(self, artist_id, target_count=None):
        artist_songs = []
        seen = set()

        def enough_candidates():
            return target_count is not None and len(artist_songs) >= target_count

        def add_tracks(tracks, album_info=None):
            album_info = album_info or {}
            for track in tracks:
                if not isinstance(track, dict) or track.get("isAvailable") is False:
                    continue
                video_id = track.get("videoId")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                artists = self._extract_artists(track) or self._extract_artists(album_info)
                artist_songs.append({
                    "title": track.get("title"),
                    "videoId": video_id,
                    "artists": artists,
                    "album": album_info.get("title") or track.get("album"),
                    "duration": track.get("duration"),
                    "thumbnails": format_thumbnails(
                        track.get("thumbnails") or album_info.get("thumbnails"),
                        video_id=video_id,
                    ),
                })

        try:
            artist_info = self._get_cached_artist(artist_id)
        except Exception as exc:
            self.logger.error("Error retrieving artist %s: %s", artist_id, exc)
            return []

        songs_data = artist_info.get("songs") or {}
        add_tracks(songs_data.get("results") or [])
        songs_browse_id = songs_data.get("browseId")
        if songs_browse_id and not enough_candidates():
            try:
                playlist_data = self._get_cached_playlist(songs_browse_id)
                add_tracks(playlist_data.get("tracks") or [])
            except Exception as exc:
                self.logger.error(
                    "Error fetching songs playlist for artist %s: %s", artist_id, exc
                )

        release_ids = []
        seen_release_ids = set()
        for content_type in ["albums", "singles"]:
            if enough_candidates():
                break
            content_data = artist_info.get(content_type) or {}
            releases = content_data.get("results") or []
            params = content_data.get("params")
            if params:
                try:
                    releases_browse_id = content_data.get("browseId") or artist_id
                    releases = self._get_cached_artist_albums(releases_browse_id, params)
                except Exception as exc:
                    self.logger.error("Error fetching %s for artist %s: %s", content_type, artist_id, exc)

            for release in releases or []:
                album_id = release.get("browseId") if isinstance(release, dict) else None
                if not album_id or album_id in seen_release_ids:
                    continue
                seen_release_ids.add(album_id)
                release_ids.append(album_id)

        # Sample the release catalog rather than always taking the first albums.
        random.shuffle(release_ids)
        for album_id in release_ids:
            if enough_candidates():
                break
            try:
                album_info = self._get_cached_album(album_id)
                add_tracks(album_info.get("tracks") or [], album_info)
            except Exception as exc:
                self.logger.error("Error fetching album %s: %s", album_id, exc)

        random.shuffle(artist_songs)
        return artist_songs[:target_count]

    @staticmethod
    def _create_balanced_mix(songs_by_artist, total_limit):
        if not songs_by_artist:
            return []

        result = []
        seen = set()
        artist_indices = {artist_id: 0 for artist_id in songs_by_artist.keys()}
        active_artists = [artist_id for artist_id, songs in songs_by_artist.items() if songs]
        random.shuffle(active_artists)

        while active_artists and len(result) < total_limit:
            next_round = []
            for artist_id in active_artists:
                songs = songs_by_artist[artist_id]
                while artist_indices[artist_id] < len(songs):
                    song = songs[artist_indices[artist_id]]
                    artist_indices[artist_id] += 1
                    identifier = song.get("videoId") or song.get("title")
                    if identifier and identifier not in seen:
                        seen.add(identifier)
                        result.append(song)
                        break
                if artist_indices[artist_id] < len(songs):
                    next_round.append(artist_id)
                if len(result) >= total_limit:
                    break
            active_artists = next_round

        random.shuffle(result)
        return result[:total_limit]

    def mix(self, artist_ids, limit_value):
        limit = parse_limit(limit_value, default=50, minimum=1, maximum=200)
        normalized_artist_ids = list(dict.fromkeys(
            artist_id.strip() for artist_id in artist_ids
            if isinstance(artist_id, str) and artist_id.strip()
        ))
        cache_key = key_for_mix(normalized_artist_ids, limit)

        def fetch():
            if not normalized_artist_ids:
                return []
            # Two candidates per allocated slot leave room for variety and overlap.
            artist_order = list(normalized_artist_ids)
            random.shuffle(artist_order)
            initial_artist_count = min(limit, len(artist_order))
            initial_artists = artist_order[:initial_artist_count]
            remaining_artists = artist_order[initial_artist_count:]
            initial_target = 2 * ((limit + initial_artist_count - 1) // initial_artist_count)
            requested_sizes = {artist_id: initial_target for artist_id in initial_artists}
            all_songs_by_artist = {artist_id: [] for artist_id in initial_artists}
            exhausted_artists = set()
            targets = dict(requested_sizes)
            with ThreadPoolExecutor(max_workers=self.settings.max_workers_mix) as executor:
                while targets:
                    futures = {
                        executor.submit(self._fetch_artist_songs, artist_id, target): artist_id
                        for artist_id, target in targets.items()
                    }
                    for future in as_completed(futures):
                        artist_id = futures[future]
                        try:
                            songs = future.result()
                        except Exception as exc:
                            self.logger.error("Error building mix songs for artist %s: %s", artist_id, exc)
                            songs = []
                        if len(songs) < targets[artist_id]:
                            exhausted_artists.add(artist_id)
                        combined = {
                            song["videoId"]: song
                            for song in all_songs_by_artist[artist_id] + songs
                        }
                        all_songs_by_artist[artist_id] = list(combined.values())
                    result = self._create_balanced_mix(all_songs_by_artist, limit)
                    if len(result) >= limit:
                        break
                    targets = {
                        artist_id: min(size * 2, limit * 2)
                        for artist_id, size in requested_sizes.items()
                        if artist_id not in exhausted_artists and size < limit * 2
                    }
                    # If the requested limit cannot represent every artist, only
                    # try additional artists when the sampled ones leave gaps.
                    if remaining_artists:
                        next_count = min(limit - len(result), len(remaining_artists))
                        for artist_id in remaining_artists[:next_count]:
                            all_songs_by_artist[artist_id] = []
                            targets[artist_id] = initial_target
                        remaining_artists = remaining_artists[next_count:]
                    requested_sizes.update(targets)

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

    def _get_cached_billboard_match(self, title, artist):
        cache_key = self._subcache_key("billboard_match", {
            "title": " ".join(str(title).casefold().split()),
            "artist": " ".join(str(artist).casefold().split()),
        })

        def fetch():
            results = self.clients.call_ytmusic(
                "search", f"{title} {artist}", filter="songs",
                timeout=self.settings.upstream_timeout_sec * 2,
            )
            best_match = results[0] if results else None
            video_id = best_match.get("videoId") if isinstance(best_match, dict) else None
            if not isinstance(video_id, str) or not video_id.strip():
                raise LookupError("No playable Billboard song match")
            return best_match

        try:
            return self._with_cache_sync(
                cache_key,
                self.settings.cache_ttl_billboard_match_sec,
                self.settings.cache_stale_billboard_match_sec,
                fetch,
            )
        except LookupError:
            # Keep an empty search retryable without changing the row schema.
            return {}, "miss", False

    async def _fetch_billboard_song(self, entry, artist_sem):
        try:
            best_match, _, stale_match = await asyncio.to_thread(
                self._get_cached_billboard_match, entry.title, entry.artist,
            )
            artists = await self._parse_and_fetch_artists(entry.artist, artist_sem)
            return {
                "rank": entry.rank,
                "title": entry.title,
                "artists": artists,
                "lastPos": entry.lastPos,
                "peakPos": entry.peakPos,
                "weeks": entry.weeks,
                "ytmusic_result": best_match,
            }, stale_match
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
            }, False

    @staticmethod
    def _billboard_week_key():
        today = datetime.now()
        days_since_tuesday = (today.weekday() - 1) % 7
        current_tuesday = today - timedelta(days=days_since_tuesday)
        return current_tuesday.strftime("%Y-%m-%d")

    async def billboard(self):
        week_key = self._billboard_week_key()
        cache_key = key_for_billboard(week_key)
        stale_matches = False

        async def fetch():
            nonlocal stale_matches
            chart = await asyncio.to_thread(
                BillboardChart, "hot-100", timeout=self.settings.upstream_timeout_sec * 2,
                max_retries=self.settings.upstream_retry_attempts,
            )
            chart_entries = list(chart)
            billboard_sem = asyncio.Semaphore(self.settings.max_concurrency_billboard)
            artist_sem = asyncio.Semaphore(self.settings.max_concurrency_artist_lookup)

            async def process_entry(entry):
                async with billboard_sem:
                    return await self._fetch_billboard_song(entry, artist_sem)

            tasks = [asyncio.create_task(process_entry(entry)) for entry in chart_entries]
            results = await asyncio.gather(*tasks)
            songs = [song for song, _ in results]
            stale_matches = any(stale for _, stale in results)
            return {
                "data": songs,
                "metadata": {
                    "total_items": len(chart_entries),
                    "chart_date": getattr(chart, "date", None),
                    "last_updated": datetime.now().isoformat(),
                },
            }

        def cache_ttls(payload):
            if stale_matches:
                # Retry stale matches rather than storing them as a fresh weekly chart.
                return 0, 0
            if any(not song["ytmusic_result"].get("videoId") for song in payload["data"]):
                # Avoid retaining a temporary lookup failure for the entire chart week.
                return min(60, self.settings.cache_ttl_billboard_sec), min(
                    120, self.settings.cache_stale_billboard_sec
                )
            return self.settings.cache_ttl_billboard_sec, self.settings.cache_stale_billboard_sec

        payload, state, stale_fallback = await self._with_cache_async(
            cache_key,
            self.settings.cache_ttl_billboard_sec,
            self.settings.cache_stale_billboard_sec,
            fetch,
            ttl_resolver=cache_ttls,
        )
        if stale_matches:
            return payload, "stale", True
        return payload, state, stale_fallback
