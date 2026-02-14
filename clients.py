import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import requests
from ytmusicapi import YTMusic


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class UpstreamClients:
    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger
        self.ytmusic = YTMusic(settings.ytmusic_auth_file)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": DEFAULT_USER_AGENT})

        executor_size = max(
            16,
            settings.max_workers_mix
            + settings.max_workers_recommendations
            + settings.max_workers_trending,
        )
        self._ytmusic_executor = ThreadPoolExecutor(max_workers=executor_size)

    def _retry_sleep(self, attempt):
        base_delay = self.settings.upstream_retry_backoff_ms / 1000.0
        exponential = base_delay * (2**attempt)
        jitter = random.uniform(0.0, base_delay)
        time.sleep(exponential + jitter)

    def call_ytmusic(self, method_name, *args, timeout=None, retries=None, **kwargs):
        method = getattr(self.ytmusic, method_name)
        timeout_sec = float(timeout or self.settings.upstream_timeout_sec)
        retry_attempts = (
            self.settings.upstream_retry_attempts if retries is None else int(retries)
        )
        total_attempts = retry_attempts + 1
        last_error = None

        for attempt in range(total_attempts):
            future = self._ytmusic_executor.submit(method, *args, **kwargs)
            try:
                return future.result(timeout=timeout_sec)
            except FutureTimeoutError as exc:
                future.cancel()
                last_error = TimeoutError(
                    f"ytmusic.{method_name} timed out after {timeout_sec}s"
                )
                self.logger.warning("%s (attempt %s/%s)", last_error, attempt + 1, total_attempts)
            except Exception as exc:
                last_error = exc
                self.logger.warning(
                    "ytmusic.%s failed on attempt %s/%s: %s",
                    method_name,
                    attempt + 1,
                    total_attempts,
                    exc,
                )

            if attempt < total_attempts - 1:
                self._retry_sleep(attempt)

        raise last_error

    def http_get(self, url, timeout=None, retries=None, **kwargs):
        timeout_sec = float(timeout or self.settings.upstream_timeout_sec)
        retry_attempts = (
            self.settings.upstream_retry_attempts if retries is None else int(retries)
        )
        total_attempts = retry_attempts + 1
        last_error = None

        for attempt in range(total_attempts):
            try:
                response = self.http.get(url, timeout=timeout_sec, **kwargs)
                if response.status_code >= 500 and attempt < total_attempts - 1:
                    self.logger.warning(
                        "HTTP GET %s returned %s (attempt %s/%s)",
                        url,
                        response.status_code,
                        attempt + 1,
                        total_attempts,
                    )
                    self._retry_sleep(attempt)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                self.logger.warning(
                    "HTTP GET %s failed on attempt %s/%s: %s",
                    url,
                    attempt + 1,
                    total_attempts,
                    exc,
                )
                if attempt < total_attempts - 1:
                    self._retry_sleep(attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"HTTP GET failed for {url}")
