import asyncio
import threading
import time
from datetime import datetime, timezone


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _iso_utc(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


class PrewarmManager:
    def __init__(self, hot_service_getter, settings, logger):
        self.hot_service_getter = hot_service_getter
        self.settings = settings
        self.logger = logger

        self.enabled = settings.enable_prewarm
        self.loop_tick_sec = settings.prewarm_loop_tick_sec
        self.trending_countries = settings.prewarm_trending_countries
        self.trending_limits = settings.prewarm_trending_limits
        self.trending_interval_sec = _clamp(
            int(settings.cache_ttl_trending_sec * 0.5),
            minimum=300,
            maximum=3600,
        )
        self.billboard_interval_sec = _clamp(
            int(settings.cache_ttl_billboard_sec * 0.5),
            minimum=3600,
            maximum=86400,
        )
        # Short TTLs and negative jitter must not outlive the refresh cadence.
        self.trending_interval_sec = min(
            self.trending_interval_sec,
            max(1, int(settings.cache_ttl_trending_sec * (1 - settings.cache_jitter_pct) * 0.5)),
        )
        self.billboard_interval_sec = min(
            self.billboard_interval_sec,
            max(1, int(settings.cache_ttl_billboard_sec * (1 - settings.cache_jitter_pct) * 0.5)),
        )

        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._started_at = None
        self._iteration_count = 0

        self._endpoint_state = {
            "trending": self._new_endpoint_state(next_due_at=None),
            "billboard": self._new_endpoint_state(next_due_at=None),
        }

    @staticmethod
    def _new_endpoint_state(next_due_at):
        return {
            "last_run_at": None,
            "last_success_at": None,
            "last_duration_ms": None,
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "refresh_count": 0,
            "last_error": None,
            "next_due_at": next_due_at,
        }

    def start(self):
        if not self.enabled:
            self.logger.info("Prewarm worker disabled (ENABLE_PREWARM=false)")
            return False

        with self._lock:
            if self._thread and self._thread.is_alive():
                return True

            self._stop_event.clear()
            self._started_at = _iso_utc(time.time())
            self._thread = threading.Thread(
                target=self._run_loop,
                name="ytmusic-prewarm",
                daemon=True,
            )
            self._thread.start()

        self.logger.info(
            "Prewarm worker started countries=%s limits=%s tick_sec=%s",
            list(self.trending_countries),
            list(self.trending_limits),
            self.loop_tick_sec,
        )
        return True

    def stop(self, timeout=2.0):
        self._stop_event.set()
        thread = None
        with self._lock:
            thread = self._thread

        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            self.logger.info("Prewarm worker stop requested")
        return True

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self.run_cycle_once()
            except Exception as exc:
                self.logger.exception("Prewarm loop iteration failed: %s", exc)
            self._stop_event.wait(self.loop_tick_sec)

    def _run_billboard(self):
        hot_service = self.hot_service_getter()
        loop = asyncio.new_event_loop()
        try:
            payload, cache_state, stale_fallback = loop.run_until_complete(
                hot_service.billboard(refresh_after_sec=self.billboard_interval_sec)
            )
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if stale_fallback or not rows or any(
                not row.get("ytmusic_result", {}).get("videoId") for row in rows
            ):
                raise RuntimeError("Billboard prewarm returned stale or incomplete data")
            return cache_state == "miss"
        finally:
            loop.close()

    def _run_trending(self):
        hot_service = self.hot_service_getter()
        refreshed = False
        failures = []
        # The largest result warms every smaller slice of the shared country cache.
        limit = max(self.trending_limits)
        for country in self.trending_countries:
            if self._stop_event.is_set():
                break
            try:
                payload, cache_state, stale_fallback = hot_service.trending(
                    country, str(limit), refresh_after_sec=self.trending_interval_sec,
                )
                if stale_fallback or not payload:
                    raise RuntimeError("Trending prewarm returned stale or empty data")
                refreshed = refreshed or cache_state == "miss"
                item_count = len(payload) if isinstance(payload, list) else None
                if cache_state != "hit":
                    self.logger.info(
                        "Prewarm trending refreshed country=%s limit=%s items=%s",
                        country, limit, item_count,
                    )
            except Exception as exc:
                failures.append(f"{country}: {exc}")
        if failures:
            raise RuntimeError("; ".join(failures))
        return refreshed

    def _is_due(self, endpoint_name, now_ts):
        with self._lock:
            next_due_at = self._endpoint_state[endpoint_name]["next_due_at"]
        return next_due_at is None or now_ts >= next_due_at

    def _record_endpoint_success(self, endpoint_name, now_ts, duration_ms, next_due_at, refreshed):
        with self._lock:
            state = self._endpoint_state[endpoint_name]
            state["last_run_at"] = _iso_utc(now_ts)
            state["last_success_at"] = _iso_utc(now_ts)
            state["last_duration_ms"] = duration_ms
            state["success_count"] += 1
            state["consecutive_failures"] = 0
            state["refresh_count"] += int(refreshed)
            state["last_error"] = None
            state["next_due_at"] = next_due_at

    def _record_endpoint_failure(self, endpoint_name, now_ts, duration_ms, next_due_at, exc):
        with self._lock:
            state = self._endpoint_state[endpoint_name]
            state["last_run_at"] = _iso_utc(now_ts)
            state["last_duration_ms"] = duration_ms
            state["failure_count"] += 1
            state["consecutive_failures"] += 1
            state["last_error"] = str(exc)
            state["next_due_at"] = next_due_at

    def _run_endpoint(self, endpoint_name, now_ts):
        started = time.perf_counter()
        if endpoint_name == "trending":
            interval_sec = self.trending_interval_sec
            runner = self._run_trending
        else:
            interval_sec = self.billboard_interval_sec
            runner = self._run_billboard

        try:
            refreshed = runner()
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._record_endpoint_success(
                endpoint_name=endpoint_name,
                now_ts=now_ts,
                duration_ms=duration_ms,
                next_due_at=now_ts + duration_ms / 1000.0 + self.loop_tick_sec,
                refreshed=refreshed,
            )
            if refreshed:
                self.logger.info("Prewarm %s refreshed duration_ms=%s", endpoint_name, duration_ms)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            with self._lock:
                failures = self._endpoint_state[endpoint_name]["consecutive_failures"]
            retry_sec = min(interval_sec, 3600, max(60, self.loop_tick_sec) * 2**min(failures, 6))
            self._record_endpoint_failure(
                endpoint_name=endpoint_name,
                now_ts=now_ts,
                duration_ms=duration_ms,
                next_due_at=now_ts + duration_ms / 1000.0 + retry_sec,
                exc=exc,
            )
            self.logger.warning(
                "Prewarm %s failed duration_ms=%s next_due_in_sec=%s error=%s",
                endpoint_name,
                duration_ms,
                retry_sec,
                exc,
            )

    def run_cycle_once(self, now_ts=None):
        if not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            ran_any = False
            with self._lock:
                self._iteration_count += 1
            for endpoint_name in ("trending", "billboard"):
                if self._stop_event.is_set():
                    break
                # Poll fresh keys cheaply, so eviction, short-lived partial results,
                # and Billboard's new week are noticed before the normal refresh age.
                endpoint_now = time.time() if now_ts is None else float(now_ts)
                if self._is_due(endpoint_name, endpoint_now):
                    ran_any = True
                    self._run_endpoint(endpoint_name, endpoint_now)
            return ran_any
        finally:
            self._cycle_lock.release()

    def snapshot(self):
        with self._lock:
            endpoint_copy = {
                "trending": dict(self._endpoint_state["trending"]),
                "billboard": dict(self._endpoint_state["billboard"]),
            }
            iteration_count = self._iteration_count
            started_at = self._started_at
            thread = self._thread

        thread_alive = bool(thread and thread.is_alive())
        running = bool(self.enabled and thread_alive and not self._stop_event.is_set())
        return {
            "enabled": self.enabled,
            "running": running,
            "worker": {
                "thread_alive": thread_alive,
                "started_at": started_at,
                "loop_tick_sec": self.loop_tick_sec,
                "iteration_count": iteration_count,
                "trending_interval_sec": self.trending_interval_sec,
                "billboard_interval_sec": self.billboard_interval_sec,
            },
            "endpoints": endpoint_copy,
        }
