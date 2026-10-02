import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests
from urllib3.util import Timeout
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicGatedError, YTMusicServerError


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DeadlineSession(requests.Session):
    def __init__(self, deadline_state, raise_retryable_status=False):
        super().__init__()
        self._deadline_state = deadline_state
        self._raise_retryable_status = raise_retryable_status

    def request(self, *args, **kwargs):
        deadline = getattr(self._deadline_state, "deadline", None)
        if deadline is None:
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = 30
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Upstream deadline expired before HTTP request")
            explicit_timeout = kwargs.get("timeout")
            if isinstance(explicit_timeout, tuple):
                connect, read = explicit_timeout
            else:
                connect = read = explicit_timeout
            kwargs["timeout"] = Timeout(
                total=remaining,
                connect=min(connect, remaining) if connect is not None else remaining,
                read=min(read, remaining) if read is not None else remaining,
            )
        response = super().request(*args, **kwargs)
        if self._raise_retryable_status and (
            response.status_code == 429 or 500 <= response.status_code <= 599
        ):
            # Classify overload responses before the SDK tries to parse HTML as JSON.
            response.raise_for_status()
        return response


class UpstreamClients:
    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger
        executor_size = max(
            16,
            settings.max_workers_mix
            + settings.max_workers_recommendations
            + settings.max_workers_trending,
        )
        self._deadline_state = threading.local()
        self._upstream_slots = threading.BoundedSemaphore(executor_size)
        self.http = self._pooled_session(executor_size, self._deadline_state)
        self.http.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self._ytmusic_http = self._pooled_session(
            executor_size, self._deadline_state, raise_retryable_status=True,
        )
        self.ytmusic = YTMusic(
            settings.ytmusic_auth_file, requests_session=self._ytmusic_http,
        )
        self._ytmusic_executor = ThreadPoolExecutor(max_workers=executor_size)

    @staticmethod
    def _pooled_session(pool_size, deadline_state, raise_retryable_status=False):
        session = DeadlineSession(deadline_state, raise_retryable_status)
        for scheme in ("http://", "https://"):
            session.mount(scheme, requests.adapters.HTTPAdapter(
                pool_connections=pool_size,
                pool_maxsize=pool_size,
                pool_block=True,
            ))
        return session

    def _retry_sleep(self, attempt, deadline, retry_after=None):
        base_delay = self.settings.upstream_retry_backoff_ms / 1000.0
        exponential = base_delay * (2**attempt)
        jitter = random.uniform(0.0, base_delay)
        delay = exponential + jitter
        if retry_after:
            try:
                requested_delay = float(retry_after)
            except (TypeError, ValueError):
                try:
                    retry_date = parsedate_to_datetime(retry_after)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    requested_delay = retry_date.timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    requested_delay = 0
            delay = max(delay, requested_delay)
        if delay >= deadline - time.monotonic():
            return False
        time.sleep(delay)
        return True

    @staticmethod
    def _retryable_status(status):
        return status == 429 or 500 <= status <= 599

    @classmethod
    def _retryable_error(cls, exc):
        if isinstance(exc, (requests.exceptions.SSLError, YTMusicGatedError)):
            return False
        if isinstance(exc, (requests.ConnectionError, requests.Timeout,
                            requests.exceptions.ChunkedEncodingError, ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError):
            return exc.response is not None and cls._retryable_status(exc.response.status_code)
        if isinstance(exc, YTMusicServerError):
            status = re.search(r"HTTP (\d{3})\b", str(exc))
            return status is not None and cls._retryable_status(int(status.group(1)))
        return False

    def _submit_before_deadline(self, operation, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._upstream_slots.acquire(timeout=remaining):
            raise TimeoutError("Upstream capacity wait exhausted the deadline")

        def invoke():
            self._deadline_state.deadline = deadline
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Upstream deadline expired before worker started")
                result = operation()
                if time.monotonic() >= deadline:
                    if isinstance(result, requests.Response):
                        result.close()
                    raise TimeoutError("Upstream deadline expired while worker was running")
                return result
            finally:
                del self._deadline_state.deadline

        try:
            future = self._ytmusic_executor.submit(invoke)
        except BaseException:
            self._upstream_slots.release()
            raise
        # A timed-out caller cannot free capacity while its worker is still running.
        future.add_done_callback(lambda completed: self._upstream_slots.release())
        return future

    def _call_with_deadline(self, name, operation, timeout, retries, retry_response=False):
        timeout_sec = float(self.settings.upstream_timeout_sec if timeout is None else timeout)
        if timeout_sec <= 0:
            raise ValueError("Upstream timeout must be positive")
        retry_attempts = max(0, self.settings.upstream_retry_attempts if retries is None else int(retries))
        deadline = time.monotonic() + timeout_sec
        for attempt in range(retry_attempts + 1):
            future = None
            try:
                future = self._submit_before_deadline(operation, deadline)
                result = future.result(timeout=max(0, deadline - time.monotonic()))
            except FutureTimeoutError as exc:
                if future is not None:
                    future.cancel()
                self.logger.warning("%s exceeded its %.3fs total time budget", name, timeout_sec)
                raise TimeoutError(f"{name} timed out after {timeout_sec}s total") from exc
            except Exception as exc:
                response = getattr(exc, "response", None)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                if (attempt >= retry_attempts or not self._retryable_error(exc)
                        or not self._retry_sleep(attempt, deadline, retry_after)):
                    raise
                self.logger.warning("Retrying %s after transient failure: %s", name, exc)
                if response is not None:
                    response.close()
                continue

            if (retry_response and self._retryable_status(result.status_code)
                    and attempt < retry_attempts):
                # Release response resources before another attempt, but preserve the
                # existing contract of returning the final HTTP error response.
                if self._retry_sleep(attempt, deadline, result.headers.get("Retry-After")):
                    result.close()
                    self.logger.warning("Retrying %s after HTTP %s", name, result.status_code)
                    continue
            return result

    def call_ytmusic(self, method_name, *args, timeout=None, retries=None, **kwargs):
        method = getattr(self.ytmusic, method_name)
        return self._call_with_deadline(
            f"ytmusic.{method_name}", lambda: method(*args, **kwargs), timeout, retries,
        )

    def http_get(self, url, timeout=None, retries=None, **kwargs):
        return self._call_with_deadline(
            "HTTP GET", lambda: self.http.get(url, **kwargs), timeout, retries,
            retry_response=True,
        )
