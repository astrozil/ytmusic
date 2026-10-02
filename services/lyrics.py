import logging
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from urllib.parse import quote

from cache_layer import stable_sha256


logger = logging.getLogger(__name__)
ARTIST_SPLIT_PATTERN = re.compile(
    r"\s*(?:,|;|/|&|\||\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*", re.IGNORECASE,
)
_CACHE_MISS = object()


class LyricsLookupUnavailable(RuntimeError):
    """A timeout or provider failure must not become a cached missing song."""


def normalize_lyrics_token(value):
    return " ".join(str(value or "").strip().lower().split())


def key_for_lyrics(song_title, artist_name):
    digest = stable_sha256({
        "title": normalize_lyrics_token(song_title),
        "artist": normalize_lyrics_token(artist_name),
    })
    return f"lyrics:{digest}"


def key_for_lyrics_negative(song_title, artist_name):
    return f"{key_for_lyrics(song_title, artist_name)}:negative"


def lyrics_negative_backoff_ttl(settings, failure_count):
    try:
        raw_ttl = settings.cache_ttl_lyrics_negative_base_sec * (
            settings.cache_lyrics_negative_backoff_factor ** (max(1, failure_count) - 1)
        )
    except OverflowError:
        raw_ttl = settings.cache_ttl_lyrics_negative_max_sec
    return max(1, int(min(settings.cache_ttl_lyrics_negative_max_sec, raw_ttl)))


def clean_lyrics_text(value):
    if not isinstance(value, str):
        return None
    return value.replace("\r\n", "\n").replace("\r", "\n").strip() or None


def lrc_to_plain_text(value):
    if not isinstance(value, str):
        return None
    lines = [re.sub(r"\[[^\]]+\]", "", line).strip() for line in value.splitlines()]
    return clean_lyrics_text("\n".join(line for line in lines if line))


def extract_primary_artist(artist_name):
    value = str(artist_name or "").strip()
    return ARTIST_SPLIT_PATTERN.split(value, maxsplit=1)[0].strip() or value


def lyrics_artist_candidates(artist_name):
    candidates = []
    seen = set()
    for value in (artist_name, extract_primary_artist(artist_name)):
        candidate = " ".join(str(value or "").strip().split())
        if candidate and candidate.lower() not in seen:
            seen.add(candidate.lower())
            candidates.append(candidate)
    return candidates


def format_genius_url(artist, song_title):
    artist = re.sub(r"[^\w\s-]", "", artist).strip().replace(" ", "-")
    title = re.sub(r"[^\w\s-]", "", song_title).strip().replace(" ", "-")
    return f"https://genius.com/{artist}-{title}-lyrics"


def _provider_lookups(song_title, artist_name):
    candidates = lyrics_artist_candidates(artist_name)
    lookups = []
    seen = set()
    for source in ("lrclib", "genius", "lyrics_ovh"):
        for artist in candidates:
            params = None
            if source == "lrclib":
                url = "https://lrclib.net/api/get"
                params = {"track_name": song_title, "artist_name": artist}
            elif source == "genius":
                url = format_genius_url(artist, song_title)
            else:
                url = f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(song_title.strip(), safe='')}"
            key = (url, tuple(sorted((params or {}).items())))
            if key not in seen:
                seen.add(key)
                lookups.append((source, artist, url, params))
    return lookups


def _parse_response(source, response):
    if source == "genius":
        match = re.search(r'"lyrics":"(.*?)"', response.text, re.DOTALL)
        lyrics = clean_lyrics_text(match.group(1).replace("\\n", "\n").replace("\\", "")) if match else None
        return lyrics, None
    payload = response.json()
    if not isinstance(payload, dict):
        return None, None
    synced = clean_lyrics_text(payload.get("syncedLyrics")) if source == "lrclib" else None
    plain = clean_lyrics_text(payload.get("plainLyrics") or payload.get("lyrics"))
    if not plain and synced:
        plain = lrc_to_plain_text(synced)
    return plain, synced


def resolve_lyrics_payload(clients, song_title, artist_name, *, deadline=None, provider_timeout=3.0):
    deadline = time.monotonic() + 12.0 if deadline is None else deadline
    lookups = _provider_lookups(" ".join(song_title.split()), artist_name)
    provider_failed = False
    for index, (source, artist, url, params) in enumerate(lookups):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LyricsLookupUnavailable("Lyrics lookup exceeded its total budget")
        # Reserve time for later providers even if every earlier lookup stalls.
        timeout = min(provider_timeout, remaining / (len(lookups) - index))
        response = None
        try:
            response = clients.http_get(url, timeout=timeout, **({"params": params} if params else {}))
            if time.monotonic() >= deadline:
                raise LyricsLookupUnavailable("Lyrics lookup exceeded its total budget")
            if response.status_code != 200:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    provider_failed = True
                continue
            plain, synced = _parse_response(source, response)
            if time.monotonic() >= deadline:
                raise LyricsLookupUnavailable("Lyrics lookup exceeded its total budget")
            if plain:
                return {
                    "song_title": song_title, "artist": artist_name, "lyrics": plain,
                    "syncedLyrics": synced, "isSynced": bool(synced),
                    "source": source, "normalizedArtist": artist,
                }
        except LyricsLookupUnavailable:
            raise
        except Exception as exc:
            provider_failed = True
            logger.warning("%s lyrics lookup failed: %s", source, exc)
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()
    if time.monotonic() >= deadline:
        raise LyricsLookupUnavailable("Lyrics lookup exceeded its total budget")
    if provider_failed:
        raise LyricsLookupUnavailable("Lyrics providers are temporarily unavailable")
    return None


class LyricsService:
    def __init__(self, clients_getter, cache_layer, settings):
        self.clients_getter = clients_getter
        self.cache_layer = cache_layer
        self.settings = settings
        # Only active lookups are retained. Followers keep a Future reference after
        # the owner removes it, so results/errors can be shared even when caching fails.
        self._inflight = {}
        self._flight_guard = threading.Lock()

    def _cached(self, key):
        cached = self.cache_layer.get_envelope(key)
        if cached.state in ("hit", "stale") and isinstance(cached.payload, dict):
            if cached.payload.get("lyrics"):
                return cached.payload
        negative = self.cache_layer.cache_get_safe(f"{key}:negative")
        if isinstance(negative, dict):
            try:
                if time.time() < float(negative.get("next_retry_at", 0)):
                    return None
            except (TypeError, ValueError):
                pass
        return _CACHE_MISS

    def _fetch(self, key, title, artist, deadline):
        cached = self._cached(key)
        if cached is not _CACHE_MISS:
            return cached
        if time.monotonic() >= deadline:
            raise LyricsLookupUnavailable("Lyrics lookup exceeded its total budget")
        payload = resolve_lyrics_payload(
            self.clients_getter(), title, artist, deadline=deadline,
            provider_timeout=self.settings.lyrics_provider_timeout_sec,
        )
        negative_key = f"{key}:negative"
        if payload is not None:
            self.cache_layer.set_envelope(
                key, payload, self.settings.cache_ttl_lyrics_sec, self.settings.cache_stale_lyrics_sec,
            )
            self.cache_layer.cache_set_safe(negative_key, {"failures": 0, "next_retry_at": 0}, timeout=1)
            return payload
        negative = self.cache_layer.cache_get_safe(negative_key)
        try:
            failures = max(0, int(negative.get("failures", 0))) if isinstance(negative, dict) else 0
        except (TypeError, ValueError):
            failures = 0
        ttl = lyrics_negative_backoff_ttl(self.settings, failures + 1)
        self.cache_layer.cache_set_safe(
            negative_key, {"failures": failures + 1, "next_retry_at": time.time() + ttl},
            timeout=max(ttl, self.settings.cache_ttl_lyrics_negative_max_sec),
        )
        return None

    def lookup(self, title, artist):
        deadline = time.monotonic() + self.settings.lyrics_timeout_sec
        key = key_for_lyrics(title, artist)
        cached = self._cached(key)
        if cached is not _CACHE_MISS:
            return cached
        with self._flight_guard:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        if owner:
            try:
                future.set_result(self._fetch(key, title, artist, deadline))
            except BaseException as exc:
                future.set_exception(exc)
                raise
            finally:
                with self._flight_guard:
                    self._inflight.pop(key, None)
        try:
            payload = future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeoutError as exc:
            # A follower timing out must not cancel work needed by other requests.
            raise LyricsLookupUnavailable("Lyrics lookup wait exceeded its total budget") from exc
        return dict(payload) if payload is not None else None
