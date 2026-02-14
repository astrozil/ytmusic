import asyncio
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import billboard

from cache_layer import (
    key_for_billboard,
    key_for_mix,
    key_for_recommendations,
    key_for_trending,
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
    def __init__(self, clients, cache_layer, settings, logger):
        self.clients = clients
        self.cache_layer = cache_layer
        self.settings = settings
        self.logger = logger

    def _with_cache_sync(self, cache_key, fresh_ttl, stale_ttl, fetch_fn):
        cached = self.cache_layer.get_envelope(cache_key)
        if cached.state == "hit":
            return cached.payload, "hit", False

        try:
            payload = fetch_fn()
            self.cache_layer.set_envelope(cache_key, payload, fresh_ttl, stale_ttl)
            return payload, "miss", False
        except Exception as exc:
            if cached.state == "stale" and self.settings.enable_stale_fallback:
                self.logger.warning("Serving stale data for key '%s' due to: %s", cache_key, exc)
                self.cache_layer.record_stale_served()
                return cached.payload, "stale", True
            raise

    async def _with_cache_async(self, cache_key, fresh_ttl, stale_ttl, fetch_coro):
        cached = self.cache_layer.get_envelope(cache_key)
        if cached.state == "hit":
            return cached.payload, "hit", False

        try:
            payload = await fetch_coro()
            self.cache_layer.set_envelope(cache_key, payload, fresh_ttl, stale_ttl)
            return payload, "miss", False
        except Exception as exc:
            if cached.state == "stale" and self.settings.enable_stale_fallback:
                self.logger.warning("Serving stale data for key '%s' due to: %s", cache_key, exc)
                self.cache_layer.record_stale_served()
                return cached.payload, "stale", True
            raise

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

    def _fetch_recommendation_seed(self, song_id):
        return self.clients.call_ytmusic(
            "get_watch_playlist",
            song_id,
            timeout=self.settings.upstream_timeout_sec * 2,
        )

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
                            track["seedSongId"] = song_id
                        all_tracks.extend(tracks)
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

    def _fetch_artist_songs(self, artist_id):
        artist_songs = []
        try:
            artist_info = self.clients.call_ytmusic(
                "get_artist",
                artist_id,
                timeout=self.settings.upstream_timeout_sec * 2,
            )
        except Exception as exc:
            self.logger.error("Error retrieving artist %s: %s", artist_id, exc)
            return []

        if "songs" in artist_info:
            songs_data = artist_info["songs"]
            songs_browse_id = songs_data.get("browseId")
            if songs_browse_id:
                try:
                    playlist_data = self.clients.call_ytmusic(
                        "get_playlist",
                        songs_browse_id,
                        timeout=self.settings.upstream_timeout_sec * 2,
                    )
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
                releases = self.clients.call_ytmusic(
                    "get_artist_albums",
                    artist_id,
                    params,
                    timeout=self.settings.upstream_timeout_sec * 2,
                )
            except Exception as exc:
                self.logger.error("Error fetching %s for artist %s: %s", content_type, artist_id, exc)
                continue

            for release in releases:
                album_id = release.get("browseId")
                if not album_id:
                    continue
                try:
                    album_info = self.clients.call_ytmusic(
                        "get_album",
                        album_id,
                        timeout=self.settings.upstream_timeout_sec * 2,
                    )
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
                    self.clients.call_ytmusic,
                    "search",
                    artist_name,
                    filter="artists",
                    timeout=self.settings.upstream_timeout_sec,
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
