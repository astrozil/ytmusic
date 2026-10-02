import asyncio
import gc
import pickle
import socket
import threading
import weakref
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from flask import Flask
from flask_caching import Cache

from cache_layer import CacheLayer
from memory_cache import BoundedMemoryCache
from services.hot_endpoints import HotEndpointsService


@pytest.mark.parametrize("ignore", [False, True])
@pytest.mark.parametrize("renamed_option", [False, True])
def test_flask_cache_factory_accepts_legacy_and_renamed_delete_options(ignore, renamed_option):
    app = Flask(__name__)
    options = {"max_bytes": 1024, "max_entry_bytes": 512}
    if renamed_option:
        options["ignore_delete_many_errors"] = ignore
    cache = Cache(app, config={
        "CACHE_TYPE": "memory_cache.BoundedMemoryCache", "CACHE_THRESHOLD": 2,
        "CACHE_DEFAULT_TIMEOUT": 77, "CACHE_IGNORE_ERRORS": ignore, "CACHE_OPTIONS": options,
    })
    backend = cache.cache
    assert backend.ignore_errors is backend.ignore_delete_many_errors is ignore
    assert backend.default_timeout == 77
    assert cache.set("a", {"items": [1]})
    assert cache.set("b", {"items": [2]})
    assert cache.get("a") == {"items": [1]}
    assert cache.delete_many("a", "b") == ["a", "b"]
    assert backend.snapshot()["serialized_bytes"] == 0
    for index in range(10):
        cache.set(str(index), "x" * 200)
    assert backend.snapshot()["entries"] <= 2
    assert backend.snapshot()["serialized_bytes"] <= 1024


def test_explicit_renamed_delete_option_overrides_legacy_option():
    cache = BoundedMemoryCache(ignore_errors=True, ignore_delete_many_errors=False)
    assert cache.ignore_errors is cache.ignore_delete_many_errors is False


def test_ignored_delete_failure_continues_to_later_keys(monkeypatch):
    cache = BoundedMemoryCache(ignore_delete_many_errors=True)
    for key in ("blocked", "a", "b"):
        cache.set(key, key)
    delete = cache.delete
    monkeypatch.setattr(cache, "delete", lambda key: False if key == "blocked" else delete(key))
    assert cache.delete_many("blocked", "a", "b") == ["a", "b"]
    assert cache.has("blocked")
    assert not cache.has("a") and not cache.has("b")


def test_memory_limit_defaults_and_invalid_values(settings_factory, monkeypatch):
    monkeypatch.delenv("CACHE_MEMORY_MAX_BYTES", raising=False)
    monkeypatch.delenv("CACHE_MEMORY_MAX_ENTRY_BYTES", raising=False)
    settings = settings_factory()
    assert settings.cache_memory_max_bytes == 64 * 1024 * 1024
    assert settings.cache_memory_max_entry_bytes == 8 * 1024 * 1024
    settings = settings_factory(CACHE_MEMORY_MAX_BYTES="bad", CACHE_MEMORY_MAX_ENTRY_BYTES="bad")
    assert settings.cache_memory_max_bytes == 64 * 1024 * 1024
    settings = settings_factory(CACHE_MEMORY_MAX_BYTES="0", CACHE_MEMORY_MAX_ENTRY_BYTES="-1")
    assert settings.cache_memory_max_bytes == 1024 * 1024
    assert settings.cache_memory_max_entry_bytes == 1024


def test_count_limit_is_strict_and_lru_preserves_recent_reads():
    cache = BoundedMemoryCache(threshold=2, max_bytes=1024, max_entry_bytes=1024)
    cache.set("a", {"value": 1})
    cache.set("b", {"value": 2})
    assert cache.get("a") == {"value": 1}
    cache.set("c", {"value": 3})
    assert cache.get("b") is None
    assert cache.get("a") == {"value": 1}
    assert cache.get("c") == {"value": 3}
    assert cache.snapshot()["entries"] == 2
    assert cache.snapshot()["evictions"] == 1


def test_byte_limit_evicts_even_when_count_limit_is_not_reached():
    cache = BoundedMemoryCache(threshold=100, max_bytes=512, max_entry_bytes=512)
    for index in range(100):
        assert cache.set(f"key-{index}", "x" * 200)
        snapshot = cache.snapshot()
        assert snapshot["serialized_bytes"] <= 512
        assert snapshot["entries"] <= 2
    assert cache.get("key-99") == "x" * 200
    assert cache.get("key-0") is None
    assert cache.snapshot()["evictions"] == 98


def test_accounting_counts_encoded_keys_and_replacement_delete_and_clear():
    cache = BoundedMemoryCache(max_bytes=4096, max_entry_bytes=4096)
    key = "音乐"
    payload = {"data": [1, 2, 3]}
    cache.set(key, payload)
    expected = len(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)) + len(key.encode("utf-8"))
    assert cache.snapshot()["serialized_bytes"] == expected
    cache.set(key, "short")
    assert cache.snapshot()["serialized_bytes"] == len(pickle.dumps("short")) + len(key.encode("utf-8"))
    assert cache.delete(key)
    assert not cache.delete(key)
    assert cache.snapshot()["serialized_bytes"] == 0
    cache.set("other", "value")
    assert cache.clear()
    assert cache.snapshot()["entries"] == cache.snapshot()["serialized_bytes"] == 0


def test_oversized_value_is_skipped_without_evicting_unrelated_values():
    cache = BoundedMemoryCache(max_bytes=1024, max_entry_bytes=256)
    cache.set("keep", "kept")
    cache.set("replace", "old")
    assert not cache.set("replace", "x" * 400)
    assert cache.get("replace") is None
    assert cache.get("keep") == "kept"
    assert cache.snapshot()["oversized_skips"] == 1
    assert cache.snapshot()["evictions"] == 0


def test_entry_limit_is_capped_to_total_budget():
    cache = BoundedMemoryCache(max_bytes=128, max_entry_bytes=1024)
    assert cache.snapshot()["max_entry_bytes"] == 128
    assert not cache.set("large", "x" * 200)


def test_expired_entry_is_removed_before_live_lru_eviction(monkeypatch):
    now = [1000]
    monkeypatch.setattr("memory_cache.time.time", lambda: now[0])
    cache = BoundedMemoryCache(threshold=2)
    cache.set("keep", "live", timeout=0)
    cache.set("expire", "dead", timeout=2)
    now[0] += 3
    cache.set("new", "new")
    assert cache.get("keep") == "live"
    assert not cache.has("expire")
    assert cache.snapshot()["evictions"] == 0
    assert cache.snapshot()["expired_removed"] == 1


@pytest.mark.parametrize("operation", ["get", "has", "snapshot"])
def test_expiry_removal_releases_bytes_on_read_or_health_check(monkeypatch, operation):
    now = [1000]
    monkeypatch.setattr("memory_cache.time.time", lambda: now[0])
    cache = BoundedMemoryCache()
    cache.set("expire", "value", timeout=2)
    now[0] += 2
    getattr(cache, operation)("expire") if operation != "snapshot" else cache.snapshot()
    assert cache.snapshot()["serialized_bytes"] == 0


def test_add_replaces_expired_entries_but_not_live_entries_and_copies_payloads(monkeypatch):
    now = [1000]
    monkeypatch.setattr("memory_cache.time.time", lambda: now[0])
    cache = BoundedMemoryCache(default_timeout=2)
    payload = {"items": [1]}
    assert cache.add("key", payload)
    payload["items"].append(2)
    loaded = cache.get("key")
    loaded["items"].append(3)
    assert cache.get("key") == {"items": [1]}
    assert not cache.add("key", "replacement")
    now[0] += 3
    assert cache.add("key", "replacement")
    assert cache.get_many("key", "missing") == ["replacement", None]


def test_concurrent_reads_writes_and_deletes_keep_byte_and_entry_bounds():
    cache = BoundedMemoryCache(threshold=20, max_bytes=2048, max_entry_bytes=512)
    barrier = threading.Barrier(8)

    def worker(worker_id):
        barrier.wait(timeout=3)
        for index in range(200):
            key = f"{worker_id}-{index % 30}"
            cache.set(key, "x" * (index % 300))
            cache.get(key)
            cache.add(key, "other")
            if index % 3 == 0:
                cache.delete(key)
            snapshot = cache.snapshot()
            assert 0 <= snapshot["serialized_bytes"] <= 2048
            assert snapshot["entries"] <= 20

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(8)))
    actual = sum(entry[2] for entry in cache._cache.values())
    assert cache.snapshot()["serialized_bytes"] == actual


def test_concurrent_add_and_increment_are_atomic():
    cache = BoundedMemoryCache()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: cache.add("once", 1), range(40)))
        assert results.count(True) == 1
        list(executor.map(lambda _: cache.inc("count"), range(1000)))
    assert cache.get("count") == 1000
    assert cache.dec("count", 2) == 998


@pytest.mark.parametrize("redis_fallback", [False, True])
def test_app_memory_backend_and_redis_fallback_share_the_configured_limits(settings_factory, monkeypatch, redis_fallback):
    if redis_fallback:
        def offline(*args, **kwargs):
            raise socket.gaierror("offline")
        monkeypatch.setattr("cache_layer.redis_from_url", offline)
    settings = replace(settings_factory(CACHE_BACKEND="redis" if redis_fallback else "simple"),
                       cache_memory_max_bytes=1024, cache_memory_max_entry_bytes=512)
    app = Flask(__name__)
    layer = CacheLayer(app, settings, app.logger)
    for index in range(20):
        assert layer.cache_set_safe(str(index), "x" * 200)
    health = layer.health_snapshot()
    assert health["backend"] == "simple"
    assert health["memory"]["max_bytes"] == 1024
    assert health["memory"]["max_entry_bytes"] == 512
    assert health["memory"]["serialized_bytes"] <= 1024
    assert health["degraded"] == redis_fallback


def test_healthy_redis_backend_does_not_apply_memory_eviction(settings_factory, monkeypatch):
    monkeypatch.setattr("cache_layer.redis_from_url", lambda *args, **kwargs: SimpleNamespace(ping=lambda: True))
    app = Flask(__name__)
    layer = CacheLayer(app, settings_factory(CACHE_BACKEND="redis"), app.logger)
    assert layer.health_snapshot()["backend"] == "redis"
    assert "memory" not in layer.health_snapshot()
    assert app.config["CACHE_TYPE"] == "RedisCache"
    assert "CACHE_OPTIONS" not in app.config


def make_service(settings_factory):
    settings = replace(settings_factory(), cache_threshold=100, cache_memory_max_bytes=4096,
                       cache_memory_max_entry_bytes=2048)
    app = Flask(__name__)
    layer = CacheLayer(app, settings, app.logger)
    return HotEndpointsService(object(), layer, settings, app.logger)


def test_skipped_cache_write_still_returns_fresh_response(settings_factory):
    service = make_service(settings_factory)
    payload = {"large": "x" * 3000}
    assert service._with_cache_sync("large", 60, 120, lambda: payload) == (payload, "miss", False)
    assert service.cache_layer.get_envelope("large").state == "miss"
    assert service.cache_layer.health_snapshot()["healthy"]
    assert service.cache_layer.health_snapshot()["memory"]["oversized_skips"] == 1


def test_unique_requests_do_not_retain_locks_or_exceed_cache_budget(settings_factory):
    service = make_service(settings_factory)
    for index in range(2000):
        payload, state, stale = service._with_cache_sync(str(index), 60, 120, lambda: {"value": "x" * 100})
        assert state == "miss" and not stale
    assert len(service._singleflight_locks) == 0
    memory = service.cache_layer.health_snapshot()["memory"]
    assert memory["entries"] <= 100
    assert memory["serialized_bytes"] <= 4096


def test_lock_registry_retains_active_and_waiting_keys_only(settings_factory):
    service = make_service(settings_factory)
    leader = service._get_singleflight_lock("shared")
    follower = service._get_singleflight_lock("shared")
    assert follower is leader
    ref = weakref.ref(leader)
    del leader
    assert service._get_singleflight_lock("shared") is follower
    del follower
    assert ref() is None
    assert len(service._singleflight_locks) == 0


def test_concurrent_singleflight_still_fetches_once_and_reclaims_lock(settings_factory):
    service = make_service(settings_factory)
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def fetch():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return "value"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(service._with_cache_sync, "shared", 60, 120, fetch) for _ in range(8)]
        try:
            assert entered.wait(2)
            assert len(service._singleflight_locks) == 1
        finally:
            release.set()
        assert [future.result(timeout=3)[0] for future in futures] == ["value"] * 8
    assert len(calls) == 1
    assert len(service._singleflight_locks) == 0


def test_cancelled_async_wait_does_not_abandon_lock_acquisition(settings_factory, monkeypatch):
    service = make_service(settings_factory)

    async def scenario():
        lock = service._get_singleflight_lock("cancel")
        lock.acquire()

        async def fetch():
            return "value"

        # Waiting must not enqueue executor jobs that can acquire after cancellation.
        def no_threads(*args, **kwargs):
            raise AssertionError("Async lock wait created a thread")

        monkeypatch.setattr("services.hot_endpoints.asyncio.to_thread", no_threads)
        waiter = asyncio.create_task(service._with_cache_async("cancel", 60, 120, fetch))
        await asyncio.sleep(0.03)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        lock.release()
        result = await asyncio.wait_for(service._with_cache_async("cancel", 60, 120, fetch), timeout=1)
        assert result == ("value", "miss", False)

    asyncio.run(scenario())
    gc.collect()
    assert len(service._singleflight_locks) == 0


def test_cancelled_async_fetch_releases_lock_for_next_request(settings_factory):
    service = make_service(settings_factory)

    async def scenario():
        started = asyncio.Event()

        async def fetch():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(service._with_cache_async("cancel", 60, 120, fetch))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async def healthy():
            return "recovered"

        assert await asyncio.wait_for(service._with_cache_async("cancel", 60, 120, healthy), timeout=1) == (
            "recovered", "miss", False,
        )

    asyncio.run(scenario())
    gc.collect()
    assert len(service._singleflight_locks) == 0


def test_sync_and_async_followers_share_one_active_lock(settings_factory):
    service = make_service(settings_factory)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fetch_sync():
        calls.append("sync")
        started.set()
        assert release.wait(3)
        return "value"

    async def fetch_async():
        calls.append("async")
        return "duplicate"

    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(service._with_cache_sync, "mixed", 60, 120, fetch_sync)
        try:
            assert started.wait(2)

            async def follower():
                task = asyncio.create_task(service._with_cache_async("mixed", 60, 120, fetch_async))
                await asyncio.sleep(0.03)
                assert not task.done()
                release.set()
                assert await asyncio.wait_for(task, timeout=1) == ("value", "hit", False)

            asyncio.run(follower())
            assert leader.result(timeout=2) == ("value", "miss", False)
        finally:
            release.set()
    assert calls == ["sync"]
    assert len(service._singleflight_locks) == 0


def test_fetch_failure_releases_registry_reference(settings_factory):
    service = make_service(settings_factory)

    def fail():
        raise RuntimeError("offline")

    for index in range(100):
        with pytest.raises(RuntimeError):
            service._with_cache_sync(str(index), 60, 120, fail)
    gc.collect()
    assert len(service._singleflight_locks) == 0
