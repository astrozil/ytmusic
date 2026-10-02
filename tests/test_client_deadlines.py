import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from ytmusicapi.exceptions import YTMusicGatedError, YTMusicServerError, YTMusicUserError

from clients import UpstreamClients


@pytest.fixture
def upstream(settings_factory, monkeypatch, request):
    class ControlledYTMusic:
        def __init__(self, auth, requests_session):
            self.session = requests_session
            self.calls = 0
            self.actions = []

        def search(self, query):
            self.calls += 1
            action = self.actions.pop(0) if self.actions else "ok"
            if isinstance(action, Exception):
                raise action
            return action() if callable(action) else action

    monkeypatch.setattr("clients.YTMusic", ControlledYTMusic)
    monkeypatch.setattr("clients.random.uniform", lambda low, high: 0)
    clients = UpstreamClients(settings_factory(
        UPSTREAM_RETRY_ATTEMPTS="3", UPSTREAM_MAX_WORKERS=getattr(request, "param", 0),
    ), logging.getLogger(__name__))
    try:
        yield clients
    finally:
        clients._ytmusic_executor.shutdown(wait=True, cancel_futures=True)
        clients.http.close()
        clients._ytmusic_http.close()


@pytest.mark.parametrize("error", [
    KeyError("renderer"), ValueError("parse failure"), TypeError("schema"),
    YTMusicUserError("invalid request"), YTMusicGatedError("sign in required"),
    YTMusicServerError("Server returned HTTP 403: Forbidden"),
    requests.exceptions.SSLError("invalid certificate"),
])
def test_permanent_errors_are_not_retried(upstream, error):
    upstream.ytmusic.actions = [error, "unexpected retry"]
    with pytest.raises(type(error)):
        upstream.call_ytmusic("search", "q", timeout=1)
    assert upstream.ytmusic.calls == 1


@pytest.mark.parametrize("error", [
    requests.ConnectionError("reset"), requests.Timeout("connect timeout"),
    requests.exceptions.ChunkedEncodingError("truncated response"),
    YTMusicServerError("Server returned HTTP 503: Unavailable"),
    YTMusicServerError("Server returned HTTP 429: Too many requests"),
])
def test_transient_errors_retry_and_recover(upstream, error):
    upstream.ytmusic.actions = [error, "recovered"]
    assert upstream.call_ytmusic("search", "q", timeout=1) == "recovered"
    assert upstream.ytmusic.calls == 2


def test_zero_retry_override_is_preserved(upstream):
    upstream.ytmusic.actions = [requests.ConnectionError("reset"), "unexpected retry"]
    with pytest.raises(requests.ConnectionError):
        upstream.call_ytmusic("search", "q", timeout=1, retries=0)
    assert upstream.ytmusic.calls == 1


def test_running_timeout_does_not_start_overlapping_retries(upstream):
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        assert release.wait(5)
        return "late result"

    upstream.ytmusic.actions = [blocked, "healthy"]
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="total"):
            upstream.call_ytmusic("search", "q", timeout=0.08, retries=5)
        assert time.monotonic() - started_at < 0.5
        assert started.is_set()
        assert upstream.ytmusic.calls == 1
    finally:
        release.set()
    assert upstream.call_ytmusic("search", "next", timeout=1) == "healthy"
    assert upstream.ytmusic.calls == 2


def test_retry_backoff_and_attempts_share_one_budget(upstream):
    upstream.ytmusic.actions = [requests.ConnectionError("reset")] * 6
    started = time.monotonic()
    with pytest.raises(requests.ConnectionError):
        upstream.call_ytmusic("search", "q", timeout=0.12, retries=5)
    assert time.monotonic() - started < 0.35
    assert upstream.ytmusic.calls <= 3


@pytest.mark.parametrize("upstream", [0, 2], indirect=True)
def test_saturation_keeps_timed_out_workers_charged_and_does_not_queue(upstream, monkeypatch):
    workers = upstream._ytmusic_executor._max_workers
    release = threading.Event()
    full = threading.Event()
    lock = threading.Lock()
    calls = [0]
    submissions = [0]
    original_submit = upstream._ytmusic_executor.submit

    def counted_submit(*args, **kwargs):
        with lock:
            submissions[0] += 1
        return original_submit(*args, **kwargs)

    def blocked(query):
        with lock:
            calls[0] += 1
            if calls[0] == workers:
                full.set()
        assert release.wait(5)
        return "late"

    monkeypatch.setattr(upstream._ytmusic_executor, "submit", counted_submit)
    monkeypatch.setattr(upstream.ytmusic, "search", blocked)

    def call():
        with pytest.raises(TimeoutError):
            upstream.call_ytmusic("search", "slow", timeout=0.2, retries=5)

    try:
        with ThreadPoolExecutor(max_workers=workers) as callers:
            futures = [callers.submit(call) for _ in range(workers)]
            assert full.wait(2)
            for future in futures:
                future.result(timeout=2)
        # Every caller has timed out, but every original worker is still occupied.
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            upstream.call_ytmusic("search", "overflow", timeout=0.05, retries=5)
        assert time.monotonic() - started < 0.3
        assert submissions[0] == calls[0] == workers
    finally:
        release.set()
    monkeypatch.setattr(upstream.ytmusic, "search", lambda query: "healthy")
    assert upstream.call_ytmusic("search", "recovered", timeout=1) == "healthy"


def test_queued_timeout_cancels_unstarted_work(upstream, monkeypatch):
    queued = Future()
    original_submit = upstream._ytmusic_executor.submit
    monkeypatch.setattr(upstream._ytmusic_executor, "submit", lambda *args, **kwargs: queued)
    with pytest.raises(TimeoutError):
        upstream.call_ytmusic("search", "expired", timeout=0.02)
    assert queued.cancelled()
    assert upstream.ytmusic.calls == 0
    monkeypatch.setattr(upstream._ytmusic_executor, "submit", original_submit)
    assert upstream.call_ytmusic("search", "healthy", timeout=1) == "ok"


def response(status):
    result = requests.Response()
    result.status_code = status
    result._content = b"response body"
    result._content_consumed = True
    result.closed_by_retry = False

    def close():
        result.closed_by_retry = True

    result.close = close
    return result


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_retries_close_previous_response_and_keep_final_response(upstream, monkeypatch, status):
    first, final = response(status), response(200)
    results = iter([first, final])
    monkeypatch.setattr(upstream.http, "get", lambda *args, **kwargs: next(results))
    assert upstream.http_get("https://example.test", timeout=1) is final
    assert first.closed_by_retry
    assert not final.closed_by_retry
    assert final.text == "response body"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_http_client_errors_are_returned_without_retry(upstream, monkeypatch, status):
    final = response(status)
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return final

    monkeypatch.setattr(upstream.http, "get", get)
    assert upstream.http_get("https://example.test", timeout=1) is final
    assert calls == [1]
    assert not final.closed_by_retry


def test_http_retry_exhaustion_returns_final_error_response(upstream, monkeypatch):
    first, final = response(503), response(503)
    results = iter([first, final])
    monkeypatch.setattr(upstream.http, "get", lambda *args, **kwargs: next(results))
    assert upstream.http_get("https://example.test", timeout=1, retries=1) is final
    assert first.closed_by_retry and not final.closed_by_retry


def test_http_backoff_that_cannot_fit_returns_open_response(upstream, monkeypatch):
    final = response(503)
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return final

    monkeypatch.setattr(upstream.http, "get", get)
    assert upstream.http_get("https://example.test", timeout=0.01) is final
    assert calls == [1]
    assert not final.closed_by_retry


@pytest.mark.parametrize("retry_after", ["120", "Thu, 01 Jan 2099 00:00:00 GMT"])
def test_http_retry_after_outside_budget_does_not_retry(upstream, monkeypatch, retry_after):
    final = response(429)
    final.headers["Retry-After"] = retry_after
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return final

    monkeypatch.setattr(upstream.http, "get", get)
    assert upstream.http_get("https://example.test", timeout=0.1) is final
    assert calls == [1]
    assert not final.closed_by_retry


def test_http_invalid_retry_after_still_uses_normal_backoff(upstream, monkeypatch):
    first, final = response(503), response(200)
    first.headers["Retry-After"] = "invalid"
    results = iter([first, final])
    monkeypatch.setattr(upstream.http, "get", lambda *args, **kwargs: next(results))
    assert upstream.http_get("https://example.test", timeout=1) is final


def test_sdk_retries_html_server_error_before_json_parser(upstream, monkeypatch):
    first, final = response(503), response(200)
    first._content = b"<html>Temporary outage</html>"
    final._content = b'{"ok": true}'
    results = iter([first, final])
    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: next(results))

    def search(query):
        return upstream.ytmusic.session.get("https://example.test").json()

    monkeypatch.setattr(upstream.ytmusic, "search", search)
    assert upstream.call_ytmusic("search", "q", timeout=1) == {"ok": True}


def test_sdk_honors_retry_after_without_waiting_beyond_budget(upstream, monkeypatch):
    final = response(429)
    final.headers["Retry-After"] = "120"
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return final

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(upstream.ytmusic, "search", lambda query: upstream.ytmusic.session.get("https://example.test"))
    with pytest.raises(requests.HTTPError) as error:
        upstream.call_ytmusic("search", "q", timeout=0.1)
    assert error.value.response is final
    assert calls == [1]


def test_sdk_requests_share_remaining_deadline_and_clear_thread_state(upstream, monkeypatch):
    upstream._ytmusic_executor.shutdown(wait=True)
    upstream._ytmusic_executor = ThreadPoolExecutor(max_workers=1)
    now = [1000.0]
    captured = []
    monkeypatch.setattr("clients.time.monotonic", lambda: now[0])

    def request(session, *args, **kwargs):
        captured.append(kwargs["timeout"])
        now[0] += 0.25
        return response(200)

    monkeypatch.setattr(requests.Session, "request", request)

    def search(query):
        upstream.ytmusic.session.get("https://example.test/first")
        upstream.ytmusic.session.get("https://example.test/second")
        return "ok"

    monkeypatch.setattr(upstream.ytmusic, "search", search)
    assert upstream.call_ytmusic("search", "q", timeout=1) == "ok"
    assert [timeout.total for timeout in captured] == [1, 0.75]
    upstream._ytmusic_executor.submit(
        upstream._ytmusic_http.get, "https://example.test/outside-task"
    ).result(timeout=1)
    assert captured[-1] == 30


def test_sdk_does_not_send_second_request_after_deadline(upstream, monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr("clients.time.monotonic", lambda: now[0])

    def request(session, *args, **kwargs):
        calls.append(1)
        now[0] += 2
        return response(200)

    monkeypatch.setattr(requests.Session, "request", request)

    def search(query):
        upstream.ytmusic.session.get("https://example.test/first")
        upstream.ytmusic.session.get("https://example.test/expired")

    monkeypatch.setattr(upstream.ytmusic, "search", search)
    with pytest.raises(TimeoutError):
        upstream.call_ytmusic("search", "q", timeout=1, retries=5)
    assert calls == [1]


def test_concurrent_sdk_calls_have_independent_socket_budgets(upstream, monkeypatch):
    gate = threading.Barrier(2)
    budgets = {}

    def request(session, method, url, **kwargs):
        budgets[url] = kwargs["timeout"].total
        return response(200)

    monkeypatch.setattr(requests.Session, "request", request)

    def search(query):
        gate.wait(timeout=2)
        upstream.ytmusic.session.get(query)
        return "ok"

    monkeypatch.setattr(upstream.ytmusic, "search", search)
    with ThreadPoolExecutor(max_workers=2) as callers:
        short = callers.submit(upstream.call_ytmusic, "search", "short", timeout=1)
        long = callers.submit(upstream.call_ytmusic, "search", "long", timeout=2)
        assert short.result(timeout=3) == long.result(timeout=3) == "ok"
    assert 0 < budgets["short"] <= 1
    assert 1 < budgets["long"] <= 2


def test_real_http_delay_uses_socket_deadline_and_recovers(upstream):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == "/slow":
                time.sleep(0.2)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        started = time.monotonic()
        with pytest.raises((TimeoutError, requests.Timeout)):
            upstream.http_get(url + "/slow", timeout=0.06, retries=5)
        assert time.monotonic() - started < 0.3
        assert upstream.http_get(url + "/fast", timeout=1).text == "ok"
        assert paths == ["/slow", "/fast"]
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
