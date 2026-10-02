from settings import Settings


def test_default_memory_cache_ignores_unused_redis_url(monkeypatch):
    monkeypatch.delenv("CACHE_BACKEND", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://invalid.example:6379/0")
    assert Settings.from_env().cache_backend == "simple"


def test_redis_remains_explicitly_configurable(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "redis")
    assert Settings.from_env().cache_backend == "redis"


def test_invalid_cache_backend_uses_memory(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "unknown")
    assert Settings.from_env().cache_backend == "simple"
