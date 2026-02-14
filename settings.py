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

    upstream_timeout_sec: float
    upstream_retry_attempts: int
    upstream_retry_backoff_ms: int

    max_workers_trending: int
    max_workers_recommendations: int
    max_workers_mix: int
    max_concurrency_billboard: int
    max_concurrency_artist_lookup: int

    cache_ttl_trending_sec: int
    cache_ttl_recommendations_sec: int
    cache_ttl_mix_sec: int
    cache_ttl_billboard_sec: int
    cache_stale_trending_sec: int
    cache_stale_recommendations_sec: int
    cache_stale_mix_sec: int
    cache_stale_billboard_sec: int

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
            cache_ttl_billboard_sec=cache_ttl_billboard,
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
            cache_stale_billboard_sec=cache_stale_billboard,
            enable_rate_limits=parse_bool(
                os.getenv("ENABLE_RATE_LIMITS", "true"),
                default=True,
            ),
            rate_limit_storage_uri=str(
                os.getenv("RATELIMIT_STORAGE_URI", "memory://")
            ).strip(),
            ytmusic_auth_file=str(os.getenv("YTMUSIC_AUTH_FILE", "browser.json")).strip(),
        )
