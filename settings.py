import os
from dataclasses import dataclass


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def parse_float(value, default, minimum=None, maximum=None):
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def parse_csv_tokens(value):
    if value is None:
        return []
    return [token.strip() for token in str(value).split(",") if token.strip()]


def parse_country_list(value, fallback=("US",)):
    tokens = parse_csv_tokens(value)
    normalized = [token.upper() for token in tokens]
    if not normalized:
        return tuple(fallback)
    return tuple(dict.fromkeys(normalized))


def parse_limited_int_list(value, fallback=(50,), minimum=1, maximum=50):
    tokens = parse_csv_tokens(value)
    parsed = []
    for token in tokens:
        try:
            int_value = int(token)
        except ValueError:
            continue
        int_value = max(minimum, int_value)
        int_value = min(maximum, int_value)
        parsed.append(int_value)

    if not parsed:
        return tuple(fallback)
    return tuple(dict.fromkeys(parsed))


@dataclass(frozen=True)
class Settings:
    cache_backend: str
    cache_fail_open: bool
    redis_url: str
    cache_redis_connect_timeout: float
    cache_default_timeout: int
    cache_threshold: int
    cache_key_prefix: str
    cache_jitter_pct: float
    enable_stale_fallback: bool
    enable_distributed_singleflight: bool
    distributed_singleflight_lock_ttl_sec: int
    distributed_singleflight_wait_timeout_sec: float
    distributed_singleflight_poll_ms: int

    upstream_timeout_sec: float
    upstream_retry_attempts: int
    upstream_retry_backoff_ms: int

    max_workers_trending: int
    max_workers_recommendations: int
    max_workers_mix: int
    max_workers_artist_songs: int
    max_workers_songs: int
    max_concurrency_billboard: int
    max_concurrency_artist_lookup: int

    cache_ttl_trending_sec: int
    cache_ttl_recommendations_sec: int
    cache_ttl_mix_sec: int
    cache_ttl_artist_songs_sec: int
    cache_ttl_songs_sec: int
    cache_ttl_lyrics_sec: int
    cache_ttl_billboard_sec: int
    cache_ttl_subcache_seed_sec: int
    cache_ttl_subcache_artist_sec: int
    cache_ttl_subcache_song_sec: int
    cache_stale_trending_sec: int
    cache_stale_recommendations_sec: int
    cache_stale_mix_sec: int
    cache_stale_artist_songs_sec: int
    cache_stale_songs_sec: int
    cache_stale_lyrics_sec: int
    cache_stale_billboard_sec: int
    cache_stale_subcache_seed_sec: int
    cache_stale_subcache_artist_sec: int
    cache_stale_subcache_song_sec: int
    cache_ttl_lyrics_negative_base_sec: int
    cache_ttl_lyrics_negative_max_sec: int
    cache_lyrics_negative_backoff_factor: float

    enable_prewarm: bool
    prewarm_loop_tick_sec: int
    prewarm_trending_countries: tuple[str, ...]
    prewarm_trending_limits: tuple[int, ...]

    enable_rate_limits: bool
    rate_limit_storage_uri: str
    ytmusic_auth_file: str

    @classmethod
    def from_env(cls):
        cache_backend = str(os.getenv("CACHE_BACKEND", "redis")).strip().lower()
        if cache_backend not in {"redis", "simple"}:
            cache_backend = "redis"

        cache_ttl_billboard = parse_int(
            os.getenv("CACHE_TTL_BILLBOARD_SEC", "604800"),
            default=604800,
            minimum=60,
        )
        cache_stale_billboard = parse_int(
            os.getenv("CACHE_STALE_BILLBOARD_SEC", "1209600"),
            default=1209600,
            minimum=cache_ttl_billboard,
        )
        cache_ttl_lyrics = parse_int(
            os.getenv("CACHE_TTL_LYRICS_SEC", "300"),
            default=300,
            minimum=30,
        )
        cache_stale_lyrics = parse_int(
            os.getenv("CACHE_STALE_LYRICS_SEC", "900"),
            default=900,
            minimum=cache_ttl_lyrics,
        )
        cache_ttl_lyrics_negative_base = parse_int(
            os.getenv("CACHE_TTL_LYRICS_NEGATIVE_BASE_SEC", "30"),
            default=30,
            minimum=5,
            maximum=3600,
        )
        cache_ttl_lyrics_negative_max = parse_int(
            os.getenv("CACHE_TTL_LYRICS_NEGATIVE_MAX_SEC", "300"),
            default=300,
            minimum=cache_ttl_lyrics_negative_base,
            maximum=86400,
        )

        return cls(
            cache_backend=cache_backend,
            cache_fail_open=parse_bool(os.getenv("CACHE_FAIL_OPEN", "true"), default=True),
            redis_url=str(os.getenv("REDIS_URL", "redis://localhost:6379/0")).strip(),
            cache_redis_connect_timeout=parse_float(
                os.getenv("CACHE_REDIS_CONNECT_TIMEOUT", "2"),
                default=2.0,
                minimum=0.1,
                maximum=30.0,
            ),
            cache_default_timeout=parse_int(
                os.getenv("CACHE_DEFAULT_TIMEOUT", "86400"),
                default=86400,
                minimum=1,
            ),
            cache_threshold=parse_int(
                os.getenv("CACHE_THRESHOLD", "5000"),
                default=5000,
                minimum=100,
            ),
            cache_key_prefix=str(os.getenv("CACHE_KEY_PREFIX", "ytmusic_api_")).strip(),
            cache_jitter_pct=parse_float(
                os.getenv("CACHE_JITTER_PCT", "0.1"),
                default=0.1,
                minimum=0.0,
                maximum=0.5,
            ),
            enable_stale_fallback=parse_bool(
                os.getenv("ENABLE_STALE_FALLBACK", "true"),
                default=True,
            ),
            enable_distributed_singleflight=parse_bool(
                os.getenv("ENABLE_DISTRIBUTED_SINGLEFLIGHT", "true"),
                default=True,
            ),
            distributed_singleflight_lock_ttl_sec=parse_int(
                os.getenv("DISTRIBUTED_SINGLEFLIGHT_LOCK_TTL_SEC", "30"),
                default=30,
                minimum=1,
                maximum=300,
            ),
            distributed_singleflight_wait_timeout_sec=parse_float(
                os.getenv("DISTRIBUTED_SINGLEFLIGHT_WAIT_TIMEOUT_SEC", "8"),
                default=8.0,
                minimum=0.1,
                maximum=60.0,
            ),
            distributed_singleflight_poll_ms=parse_int(
                os.getenv("DISTRIBUTED_SINGLEFLIGHT_POLL_MS", "100"),
                default=100,
                minimum=10,
                maximum=2000,
            ),
            upstream_timeout_sec=parse_float(
                os.getenv("UPSTREAM_TIMEOUT_SEC", "6"),
                default=6.0,
                minimum=0.5,
                maximum=60.0,
            ),
            upstream_retry_attempts=parse_int(
                os.getenv("UPSTREAM_RETRY_ATTEMPTS", "2"),
                default=2,
                minimum=0,
                maximum=5,
            ),
            upstream_retry_backoff_ms=parse_int(
                os.getenv("UPSTREAM_RETRY_BACKOFF_MS", "200"),
                default=200,
                minimum=25,
                maximum=5000,
            ),
            max_workers_trending=parse_int(
                os.getenv("MAX_WORKERS_TRENDING", "8"),
                default=8,
                minimum=1,
                maximum=64,
            ),
            max_workers_recommendations=parse_int(
                os.getenv("MAX_WORKERS_RECOMMENDATIONS", "10"),
                default=10,
                minimum=1,
                maximum=64,
            ),
            max_workers_mix=parse_int(
                os.getenv("MAX_WORKERS_MIX", "8"),
                default=8,
                minimum=1,
                maximum=64,
            ),
            max_workers_artist_songs=parse_int(
                os.getenv("MAX_WORKERS_ARTIST_SONGS", "8"),
                default=8,
                minimum=1,
                maximum=64,
            ),
            max_workers_songs=parse_int(
                os.getenv("MAX_WORKERS_SONGS", "12"),
                default=12,
                minimum=1,
                maximum=64,
            ),
            max_concurrency_billboard=parse_int(
                os.getenv("MAX_CONCURRENCY_BILLBOARD", "12"),
                default=12,
                minimum=1,
                maximum=64,
            ),
            max_concurrency_artist_lookup=parse_int(
                os.getenv("MAX_CONCURRENCY_ARTIST_LOOKUP", "8"),
                default=8,
                minimum=1,
                maximum=64,
            ),
            cache_ttl_trending_sec=parse_int(
                os.getenv("CACHE_TTL_TRENDING_SEC", "1200"),
                default=1200,
                minimum=60,
            ),
            cache_ttl_recommendations_sec=parse_int(
                os.getenv("CACHE_TTL_RECOMMENDATIONS_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_mix_sec=parse_int(
                os.getenv("CACHE_TTL_MIX_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_artist_songs_sec=parse_int(
                os.getenv("CACHE_TTL_ARTIST_SONGS_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_songs_sec=parse_int(
                os.getenv("CACHE_TTL_SONGS_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_lyrics_sec=cache_ttl_lyrics,
            cache_ttl_billboard_sec=cache_ttl_billboard,
            cache_ttl_subcache_seed_sec=parse_int(
                os.getenv("CACHE_TTL_SUBCACHE_SEED_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_subcache_artist_sec=parse_int(
                os.getenv("CACHE_TTL_SUBCACHE_ARTIST_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_ttl_subcache_song_sec=parse_int(
                os.getenv("CACHE_TTL_SUBCACHE_SONG_SEC", "21600"),
                default=21600,
                minimum=60,
            ),
            cache_stale_trending_sec=parse_int(
                os.getenv("CACHE_STALE_TRENDING_SEC", "43200"),
                default=43200,
                minimum=300,
            ),
            cache_stale_recommendations_sec=parse_int(
                os.getenv("CACHE_STALE_RECOMMENDATIONS_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_mix_sec=parse_int(
                os.getenv("CACHE_STALE_MIX_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_artist_songs_sec=parse_int(
                os.getenv("CACHE_STALE_ARTIST_SONGS_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_songs_sec=parse_int(
                os.getenv("CACHE_STALE_SONGS_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_lyrics_sec=cache_stale_lyrics,
            cache_stale_billboard_sec=cache_stale_billboard,
            cache_stale_subcache_seed_sec=parse_int(
                os.getenv("CACHE_STALE_SUBCACHE_SEED_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_subcache_artist_sec=parse_int(
                os.getenv("CACHE_STALE_SUBCACHE_ARTIST_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_stale_subcache_song_sec=parse_int(
                os.getenv("CACHE_STALE_SUBCACHE_SONG_SEC", "86400"),
                default=86400,
                minimum=300,
            ),
            cache_ttl_lyrics_negative_base_sec=cache_ttl_lyrics_negative_base,
            cache_ttl_lyrics_negative_max_sec=cache_ttl_lyrics_negative_max,
            cache_lyrics_negative_backoff_factor=parse_float(
                os.getenv("CACHE_LYRICS_NEGATIVE_BACKOFF_FACTOR", "2.0"),
                default=2.0,
                minimum=1.0,
                maximum=8.0,
            ),
            enable_prewarm=parse_bool(
                os.getenv("ENABLE_PREWARM", "false"),
                default=False,
            ),
            prewarm_loop_tick_sec=parse_int(
                os.getenv("PREWARM_LOOP_TICK_SEC", "30"),
                default=30,
                minimum=5,
                maximum=300,
            ),
            prewarm_trending_countries=parse_country_list(
                os.getenv("PREWARM_TRENDING_COUNTRIES", "US"),
                fallback=("US",),
            ),
            prewarm_trending_limits=parse_limited_int_list(
                os.getenv("PREWARM_TRENDING_LIMITS", "50"),
                fallback=(50,),
                minimum=1,
                maximum=50,
            ),
            enable_rate_limits=parse_bool(
                os.getenv("ENABLE_RATE_LIMITS", "true"),
                default=True,
            ),
            rate_limit_storage_uri=str(
                os.getenv("RATELIMIT_STORAGE_URI", "memory://")
            ).strip(),
            ytmusic_auth_file=str(os.getenv("YTMUSIC_AUTH_FILE", "browser.json")).strip(),
        )
