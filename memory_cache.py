import threading
import time
from collections import OrderedDict

from cachelib.serializers import SimpleSerializer
from flask_caching.backends.base import BaseCache


class BoundedMemoryCache(BaseCache):
    """Per-process LRU cache bounded by entry count and serialized key/value bytes."""

    serializer = SimpleSerializer()

    def __init__(self, threshold=5000, default_timeout=300, max_bytes=64 * 1024 * 1024,
                 max_entry_bytes=8 * 1024 * 1024, ignore_errors=False):
        super().__init__(default_timeout=default_timeout)
        self.ignore_errors = ignore_errors
        self._threshold = max(1, int(threshold))
        self._max_bytes = max(1, int(max_bytes))
        self._max_entry_bytes = min(self._max_bytes, max(1, int(max_entry_bytes)))
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._bytes = 0
        self._evictions = 0
        self._expired = 0
        self._oversized = 0

    @classmethod
    def factory(cls, app, config, args, kwargs):
        kwargs.update(threshold=config["CACHE_THRESHOLD"], ignore_errors=config["CACHE_IGNORE_ERRORS"])
        return cls(*args, **kwargs)

    def _remove(self, key):
        entry = self._cache.pop(key, None)
        if entry is None:
            return False
        self._bytes -= entry[2]
        return True

    def _lookup(self, key, touch=True):
        entry = self._cache.get(key)
        if entry is not None and entry[0] != 0 and entry[0] <= time.time():
            self._remove(key)
            self._expired += 1
            return None
        if entry is not None and touch:
            self._cache.move_to_end(key)
        return entry

    def _remove_expired(self, now):
        expired = [key for key, entry in self._cache.items() if entry[0] != 0 and entry[0] <= now]
        for key in expired:
            self._remove(key)
        self._expired += len(expired)

    def get(self, key):
        with self._lock:
            entry = self._lookup(key)
            serialized = entry[1] if entry is not None else None
        # Deserialize outside the cache lock, preserving isolated payload copies.
        return self.serializer.loads(serialized) if serialized is not None else None

    def has(self, key):
        with self._lock:
            return self._lookup(key, touch=False) is not None

    def _store(self, key, value, timeout, only_if_absent):
        serialized = self.serializer.dumps(value)
        weight = len(serialized) + len(str(key).encode("utf-8"))
        timeout = self.default_timeout if timeout is None else timeout
        with self._lock:
            if only_if_absent and self._lookup(key, touch=False) is not None:
                return False
            # An uncacheable replacement must not leave the previous value as a fresh hit.
            self._remove(key)
            if weight > self._max_entry_bytes:
                self._oversized += 1
                return False
            if timeout < 0:
                return True
            if len(self._cache) >= self._threshold or self._bytes + weight > self._max_bytes:
                self._remove_expired(time.time())
            while self._cache and (
                len(self._cache) >= self._threshold or self._bytes + weight > self._max_bytes
            ):
                oldest = next(iter(self._cache))
                self._remove(oldest)
                self._evictions += 1
            expires = time.time() + timeout if timeout else 0
            self._cache[key] = (expires, serialized, weight)
            self._bytes += weight
            return True

    def set(self, key, value, timeout=None):
        return self._store(key, value, timeout, only_if_absent=False)

    def add(self, key, value, timeout=None):
        return self._store(key, value, timeout, only_if_absent=True)

    def delete(self, key):
        with self._lock:
            return self._remove(key)

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._bytes = 0
            return True

    def inc(self, key, delta=1):
        with self._lock:
            value = (self.get(key) or 0) + delta
            return value if self.set(key, value) else None

    def dec(self, key, delta=1):
        return self.inc(key, -delta)

    def snapshot(self):
        with self._lock:
            self._remove_expired(time.time())
            return {
                "entries": len(self._cache), "max_entries": self._threshold,
                "serialized_bytes": self._bytes, "max_bytes": self._max_bytes,
                "max_entry_bytes": self._max_entry_bytes,
                "evictions": self._evictions, "expired_removed": self._expired,
                "oversized_skips": self._oversized,
            }
