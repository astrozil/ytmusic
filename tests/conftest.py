import pytest

from settings import Settings


@pytest.fixture
def settings_factory(monkeypatch):
    def _factory(**overrides):
        defaults = {
            "CACHE_BACKEND": "simple",
            "CACHE_FAIL_OPEN": "true",
            "CACHE_JITTER_PCT": "0",
            "ENABLE_STALE_FALLBACK": "true",
            "ENABLE_DISTRIBUTED_SINGLEFLIGHT": "true",
            "DISTRIBUTED_SINGLEFLIGHT_LOCK_TTL_SEC": "5",
            "DISTRIBUTED_SINGLEFLIGHT_WAIT_TIMEOUT_SEC": "0.2",
            "DISTRIBUTED_SINGLEFLIGHT_POLL_MS": "10",
            "ENABLE_RATE_LIMITS": "false",
            "RATELIMIT_STORAGE_URI": "memory://",
            "UPSTREAM_TIMEOUT_SEC": "1",
            "UPSTREAM_RETRY_ATTEMPTS": "0",
            "UPSTREAM_RETRY_BACKOFF_MS": "25",
            "MAX_WORKERS_TRENDING": "4",
            "MAX_WORKERS_RECOMMENDATIONS": "4",
            "MAX_WORKERS_MIX": "4",
            "MAX_WORKERS_ARTIST_SONGS": "4",
            "MAX_WORKERS_SONGS": "4",
            "MAX_CONCURRENCY_BILLBOARD": "4",
            "MAX_CONCURRENCY_ARTIST_LOOKUP": "4",
            "CACHE_TTL_TRENDING_SEC": "60",
            "CACHE_TTL_RECOMMENDATIONS_SEC": "60",
            "CACHE_TTL_MIX_SEC": "60",
            "CACHE_TTL_ARTIST_SONGS_SEC": "60",
            "CACHE_TTL_SONGS_SEC": "60",
            "CACHE_TTL_LYRICS_SEC": "60",
            "CACHE_TTL_BILLBOARD_SEC": "60",
            "CACHE_TTL_SUBCACHE_SEED_SEC": "60",
            "CACHE_TTL_SUBCACHE_ARTIST_SEC": "60",
            "CACHE_TTL_SUBCACHE_SONG_SEC": "60",
            "CACHE_STALE_TRENDING_SEC": "120",
            "CACHE_STALE_RECOMMENDATIONS_SEC": "120",
            "CACHE_STALE_MIX_SEC": "120",
            "CACHE_STALE_ARTIST_SONGS_SEC": "120",
            "CACHE_STALE_SONGS_SEC": "120",
            "CACHE_STALE_LYRICS_SEC": "120",
            "CACHE_STALE_BILLBOARD_SEC": "120",
            "CACHE_STALE_SUBCACHE_SEED_SEC": "120",
            "CACHE_STALE_SUBCACHE_ARTIST_SEC": "120",
            "CACHE_STALE_SUBCACHE_SONG_SEC": "120",
            "CACHE_TTL_LYRICS_NEGATIVE_BASE_SEC": "10",
            "CACHE_TTL_LYRICS_NEGATIVE_MAX_SEC": "60",
            "CACHE_LYRICS_NEGATIVE_BACKOFF_FACTOR": "2.0",
            "ENABLE_PREWARM": "false",
            "PREWARM_LOOP_TICK_SEC": "30",
            "PREWARM_TRENDING_COUNTRIES": "US",
            "PREWARM_TRENDING_LIMITS": "50",
        }
        defaults.update({key: str(value) for key, value in overrides.items()})
        for key, value in defaults.items():
            monkeypatch.setenv(key, value)
        return Settings.from_env()

    return _factory
