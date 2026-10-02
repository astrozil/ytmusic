import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

import app as app_module
from clients import UpstreamClients
from workers import batch_executor


def test_cold_requests_and_prewarm_share_one_client_and_service(settings_factory, monkeypatch):
    calls = Counter()
    started = threading.Event()
    release = threading.Event()
    caller_barrier = threading.Barrier(12)
    original_service = app_module.HotEndpointsService

    class FakeClients:
        def __init__(self, settings, logger):
            calls["clients"] += 1
            started.set()
            assert release.wait(5)

        def call_ytmusic(self, method, identifier, **kwargs):
            calls[method] += 1
            if method == "search":
                return []
            return {"videoDetails": {"videoId": identifier, "title": "Song"}}

    def build_service(**kwargs):
        calls["service"] += 1
        return original_service(**kwargs)

    monkeypatch.setattr(app_module, "UpstreamClients", FakeClients)
    monkeypatch.setattr(app_module, "HotEndpointsService", build_service)
    app = app_module.create_app(settings_obj=settings_factory())
    manager = app.extensions["ytmusic_prewarm_manager"]

    def request(index):
        caller_barrier.wait(timeout=5)
        if index % 3 == 0:
            return manager.hot_service_getter()
        with app.test_client() as client:
            path = "/search?query=song" if index % 3 == 1 else "/song/s1"
            return client.get(path)

    with ThreadPoolExecutor(max_workers=12) as callers:
        futures = [callers.submit(request, index) for index in range(12)]
        try:
            assert started.wait(5)
            # Health probes stay responsive while upstream initialization is blocked.
            with app.test_client() as client:
                assert client.get("/health").status_code == 200
                assert client.get("/").status_code == 200
            assert app.extensions["ytmusic_clients"] is None
            assert app.extensions["ytmusic_hot_service"] is None
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]

    assert calls["clients"] == calls["service"] == calls["get_song"] == 1
    assert calls["search"] == 4
    for index, result in enumerate(results):
        if index % 3 == 0:
            assert result is app.extensions["ytmusic_hot_service"]
        else:
            assert result.status_code == 200
    assert manager.hot_service_getter().clients is app.extensions["ytmusic_clients"]


@pytest.mark.parametrize("constructor", ["clients", "service"])
def test_failed_initialization_can_retry_without_recreating_ready_client(
    settings_factory, monkeypatch, constructor,
):
    calls = Counter()
    fake_clients = object()
    fake_service = object()

    def build_clients(*args):
        calls["clients"] += 1
        if constructor == "clients" and calls["clients"] == 1:
            raise RuntimeError("temporary client failure")
        return fake_clients

    def build_service(**kwargs):
        calls["service"] += 1
        assert kwargs["clients"] is fake_clients
        if constructor == "service" and calls["service"] == 1:
            raise RuntimeError("temporary service failure")
        return fake_service

    monkeypatch.setattr(app_module, "UpstreamClients", build_clients)
    monkeypatch.setattr(app_module, "HotEndpointsService", build_service)
    app = app_module.create_app(settings_obj=settings_factory())
    getter = app.extensions["ytmusic_prewarm_manager"].hot_service_getter
    with pytest.raises(RuntimeError, match="temporary"):
        getter()
    assert app.extensions["ytmusic_hot_service"] is None
    assert getter() is getter() is fake_service
    assert calls[constructor] == 2
    assert calls["clients"] == (2 if constructor == "clients" else 1)


def test_probes_and_injected_service_do_not_initialize_clients(settings_factory, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected client/service construction")

    monkeypatch.setattr(app_module, "UpstreamClients", unexpected)
    monkeypatch.setattr(app_module, "HotEndpointsService", unexpected)
    service = object()
    app = app_module.create_app(settings_obj=settings_factory(), hot_service_obj=service)
    with app.test_client() as client:
        assert client.get("/").status_code == client.get("/health").status_code == 200
    assert app.extensions["ytmusic_clients"] is None
    assert app.extensions["ytmusic_prewarm_manager"].hot_service_getter() is service


@pytest.mark.parametrize("item_count,max_workers", [(0, 8), (1, 8), (3, 1)])
def test_serial_batches_do_not_start_threads(monkeypatch, item_count, max_workers):
    monkeypatch.setattr("workers.ThreadPoolExecutor", lambda **kwargs: pytest.fail("Unused pool"))
    caller = threading.get_ident()
    with batch_executor(max_workers, item_count) as executor:
        futures = [executor.submit(threading.get_ident) for _ in range(item_count)]
        assert [future.result() for future in as_completed(futures)] == [caller] * item_count


def test_inline_failure_is_reported_by_future_and_other_items_continue():
    def fail():
        raise ValueError("bad item")

    with batch_executor(1, 2) as executor:
        failed = executor.submit(fail)
        success = executor.submit(lambda: "ok")
        with pytest.raises(ValueError, match="bad item"):
            failed.result()
        assert success.result() == "ok"


def test_parallel_batch_caps_workers_to_items_and_retains_parallelism():
    barrier = threading.Barrier(2)

    def item():
        barrier.wait(timeout=5)
        return threading.get_ident()

    with batch_executor(12, 2) as executor:
        assert executor._max_workers == 2
        futures = [executor.submit(item) for _ in range(2)]
        assert len({future.result(timeout=5) for future in futures}) == 2


@pytest.mark.parametrize("limit,expected", [(0, 16), (1, 1), (3, 3)])
def test_upstream_worker_limit_matches_both_http_pools_and_admission(
    settings_factory, monkeypatch, limit, expected,
):
    monkeypatch.setattr("clients.YTMusic", lambda *args, **kwargs: object())
    clients = UpstreamClients(settings_factory(UPSTREAM_MAX_WORKERS=limit), logging.getLogger(__name__))
    try:
        assert clients._ytmusic_executor._max_workers == expected
        for session in (clients.http, clients._ytmusic_http):
            for scheme in ("http://", "https://"):
                assert session.get_adapter(scheme)._pool_maxsize == expected
        for _ in range(expected):
            assert clients._upstream_slots.acquire(blocking=False)
        assert not clients._upstream_slots.acquire(blocking=False)
        for _ in range(expected):
            clients._upstream_slots.release()
    finally:
        clients._ytmusic_executor.shutdown(wait=True)
        clients.http.close()
        clients._ytmusic_http.close()


def test_failed_sdk_initialization_closes_sessions(settings_factory, monkeypatch):
    sessions = []
    original_session = UpstreamClients._pooled_session

    def session(*args, **kwargs):
        result = original_session(*args, **kwargs)
        monkeypatch.setattr(result, "close", lambda: sessions.append(result))
        return result

    def fail(*args, **kwargs):
        raise ValueError("SDK setup failed")

    monkeypatch.setattr(UpstreamClients, "_pooled_session", staticmethod(session))
    monkeypatch.setattr("clients.YTMusic", fail)
    with pytest.raises(ValueError, match="SDK setup failed"):
        UpstreamClients(settings_factory(), logging.getLogger(__name__))
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


@pytest.mark.parametrize("value,expected", [("garbage", 0), ("-3", 0), ("300", 256), ("5", 5)])
def test_upstream_worker_setting_is_validated(settings_factory, value, expected):
    assert settings_factory(UPSTREAM_MAX_WORKERS=value).upstream_max_workers == expected
