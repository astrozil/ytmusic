import hashlib
import json
import random
import socket
import time
from dataclasses import dataclass

from flask import has_request_context, request
from flask_caching import Cache
from redis import from_url as redis_from_url
from redis.exceptions import RedisError


@dataclass
class CacheLookup:
    state: str
    payload: object = None
    envelope: dict | None = None


def _canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_sha256(data):
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def key_for_recommendations(song_ids):
    normalized = sorted([str(song_id).strip() for song_id in song_ids if str(song_id).strip()])
    digest = stable_sha256({"song_ids": normalized})
    return f"recommendations:{digest}"


def key_for_mix(artist_ids, limit):
    normalized_artists = sorted([str(artist_id).strip() for artist_id in artist_ids if str(artist_id).strip()])
    digest = stable_sha256({"artist_ids": normalized_artists, "limit": int(limit)})
    return f"mix:{digest}"


def key_for_trending(country, limit):
    normalized_country = str(country or "US").strip().upper() or "US"
    normalized_limit = int(limit)
    digest = stable_sha256({"country": normalized_country, "limit": normalized_limit})
    return f"trending:{digest}"


def key_for_billboard(week_key):
    return f"billboard:hot-100:{week_key}"


class CacheLayer:
    def __init__(self, flask_app, settings, logger):
        self.settings = settings
        self.logger = logger
        self.cache, self.status, self.cache_fail_open = self._initialize_cache(flask_app)
        self.metrics = {
            "hits": 0,
            "misses": 0,
            "stale_candidates": 0,
            "stale_served": 0,
            "get_errors": 0,
            "set_errors": 0,
        }

    def _cache_config(self, cache_type, redis_url=None):
        config = {
            "CACHE_TYPE": cache_type,
            "CACHE_DEFAULT_TIMEOUT": self.settings.cache_default_timeout,
            "CACHE_THRESHOLD": self.settings.cache_threshold,
            "CACHE_KEY_PREFIX": self.settings.cache_key_prefix,
        }
        if cache_type == "RedisCache" and redis_url:
            config["CACHE_REDIS_URL"] = redis_url
        return config

    def _initialize_cache(self, flask_app):
        cache_status = {
            "backend": self.settings.cache_backend,
            "mode": "fail_open" if self.settings.cache_fail_open else "fail_closed",
            "healthy": True,
            "last_error": None,
        }

        if self.settings.cache_backend == "simple":
            flask_app.config.update(self._cache_config("SimpleCache"))
            self.logger.info(
                "Cache initialized with backend=%s mode=%s",
                cache_status["backend"],
                cache_status["mode"],
            )
            return Cache(flask_app), cache_status, self.settings.cache_fail_open

        try:
            redis_client = redis_from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.cache_redis_connect_timeout,
                socket_timeout=self.settings.cache_redis_connect_timeout,
            )
            redis_client.ping()
            flask_app.config.update(
                self._cache_config("RedisCache", redis_url=self.settings.redis_url)
            )
            cache_instance = Cache(flask_app)
            self.logger.info(
                "Cache initialized with backend=%s mode=%s",
                cache_status["backend"],
                cache_status["mode"],
            )
            return cache_instance, cache_status, self.settings.cache_fail_open
        except (RedisError, OSError, socket.gaierror, ValueError) as exc:
            cache_status["healthy"] = False
            cache_status["last_error"] = f"Redis startup check failed: {exc}"

            if not self.settings.cache_fail_open:
                raise RuntimeError(
                    "Redis startup check failed and CACHE_FAIL_OPEN=false"
                ) from exc

            self.logger.warning(
                "Redis startup check failed (%s). Falling back to SimpleCache.", exc
            )
            cache_status["backend"] = "simple"
            cache_status["healthy"] = True
            flask_app.config.update(self._cache_config("SimpleCache"))
            cache_instance = Cache(flask_app)
            return cache_instance, cache_status, self.settings.cache_fail_open

    def _record_cache_success(self):
        self.status["healthy"] = True
        self.status["last_error"] = None

    def _record_cache_failure(self, operation, key, exc):
        self.status["healthy"] = False
        endpoint = request.path if has_request_context() else "unknown"
        self.status["last_error"] = (
            f"{operation} failed for key '{key}' on '{endpoint}': {exc}"
        )
        self.logger.warning(
            "Cache %s failed for key '%s' on '%s': %s", operation, key, endpoint, exc
        )
        if operation == "get":
            self.metrics["get_errors"] += 1
        elif operation == "set":
            self.metrics["set_errors"] += 1

    def cache_get_safe(self, key):
        try:
            value = self.cache.get(key)
            self._record_cache_success()
            return value
        except (RedisError, OSError, socket.gaierror, ConnectionError) as exc:
            self._record_cache_failure("get", key, exc)
            if not self.cache_fail_open:
                raise
            return None
        except Exception as exc:
            self._record_cache_failure("get", key, exc)
            if not self.cache_fail_open:
                raise
            return None

    def cache_set_safe(self, key, value, timeout=None):
        try:
            result = self.cache.set(key, value, timeout=timeout)
            self._record_cache_success()
            return result
        except (RedisError, OSError, socket.gaierror, ConnectionError) as exc:
            self._record_cache_failure("set", key, exc)
            if not self.cache_fail_open:
                raise
            return False
        except Exception as exc:
            self._record_cache_failure("set", key, exc)
            if not self.cache_fail_open:
                raise
            return False

    def _apply_jitter(self, ttl_seconds):
        ttl = int(ttl_seconds)
        if ttl <= 1 or self.settings.cache_jitter_pct <= 0:
            return max(1, ttl)
        spread = ttl * float(self.settings.cache_jitter_pct)
        jittered = int(ttl + random.uniform(-spread, spread))
        return max(1, jittered)

    def get_envelope(self, key):
        cached_value = self.cache_get_safe(key)
        if cached_value is None:
            self.metrics["misses"] += 1
            return CacheLookup(state="miss")

        if not isinstance(cached_value, dict) or "payload" not in cached_value:
            self.metrics["hits"] += 1
            return CacheLookup(state="hit", payload=cached_value, envelope=None)

        now = int(time.time())
        payload = cached_value.get("payload")
        fresh_until = int(cached_value.get("fresh_until", 0))
        stale_until = int(cached_value.get("stale_until", 0))

        if now <= fresh_until:
            self.metrics["hits"] += 1
            return CacheLookup(state="hit", payload=payload, envelope=cached_value)
        if now <= stale_until:
            self.metrics["stale_candidates"] += 1
            return CacheLookup(state="stale", payload=payload, envelope=cached_value)

        self.metrics["misses"] += 1
        return CacheLookup(state="miss")

    def set_envelope(self, key, payload, fresh_ttl, stale_ttl):
        now = int(time.time())
        effective_fresh = self._apply_jitter(fresh_ttl)
        effective_stale = max(effective_fresh + 1, self._apply_jitter(stale_ttl))
        envelope = {
            "payload": payload,
            "fetched_at": now,
            "fresh_until": now + effective_fresh,
            "stale_until": now + effective_stale,
        }
        self.cache_set_safe(key, envelope, timeout=effective_stale)
        return envelope

    def record_stale_served(self):
        self.metrics["stale_served"] += 1

    @staticmethod
    def headers_for_state(cache_state, stale_fallback=False):
        headers = {"X-Cache": cache_state}
        if stale_fallback:
            headers["X-Data-Stale"] = "1"
        return headers

    def health_snapshot(self):
        return {
            "backend": self.status["backend"],
            "mode": self.status["mode"],
            "healthy": self.status["healthy"],
            "last_error": self.status["last_error"],
            "metrics": self.metrics.copy(),
        }
