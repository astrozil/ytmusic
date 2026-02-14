import logging
import time

from services.prewarm import PrewarmManager


class FakeHotService:
    def __init__(self):
        self.trending_calls = 0
        self.billboard_calls = 0
        self.fail_trending = False
        self.fail_billboard = False

    def trending(self, country, limit_value):
        self.trending_calls += 1
        if self.fail_trending:
            raise RuntimeError("trending failed")
        return [{"country": country, "limit": limit_value}], "miss", False

    async def billboard(self):
        self.billboard_calls += 1
        if self.fail_billboard:
            raise RuntimeError("billboard failed")
        return {"data": [], "metadata": {}}, "miss", False


def wait_until(predicate, timeout_sec=2.0):
    started = time.time()
    while time.time() - started < timeout_sec:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_settings_prewarm_defaults(settings_factory):
    settings = settings_factory()
    assert settings.enable_prewarm is False
    assert settings.prewarm_loop_tick_sec == 30
    assert settings.prewarm_trending_countries == ("US",)
    assert settings.prewarm_trending_limits == (50,)


def test_settings_prewarm_invalid_values_fallback_safely(settings_factory):
    settings = settings_factory(
        ENABLE_PREWARM="true",
        PREWARM_LOOP_TICK_SEC="999",
        PREWARM_TRENDING_COUNTRIES=" , , ",
        PREWARM_TRENDING_LIMITS="bad,none",
    )
    assert settings.enable_prewarm is True
    assert settings.prewarm_loop_tick_sec == 300
    assert settings.prewarm_trending_countries == ("US",)
    assert settings.prewarm_trending_limits == (50,)


def test_prewarm_interval_derivation_clamps(settings_factory):
    settings = settings_factory(
        ENABLE_PREWARM="true",
        CACHE_TTL_TRENDING_SEC="1200",
        CACHE_TTL_BILLBOARD_SEC="604800",
    )
    fake_hot = FakeHotService()
    manager = PrewarmManager(lambda: fake_hot, settings, logging.getLogger(__name__))
    assert manager.trending_interval_sec == 600
    assert manager.billboard_interval_sec == 86400


def test_prewarm_start_disabled_does_not_run(settings_factory):
    settings = settings_factory(ENABLE_PREWARM="false")
    fake_hot = FakeHotService()
    manager = PrewarmManager(lambda: fake_hot, settings, logging.getLogger(__name__))
    started = manager.start()
    snapshot = manager.snapshot()
    manager.stop()

    assert started is False
    assert snapshot["enabled"] is False
    assert snapshot["running"] is False
    assert snapshot["worker"]["thread_alive"] is False
    assert fake_hot.trending_calls == 0
    assert fake_hot.billboard_calls == 0


def test_prewarm_start_runs_immediate_cycle(settings_factory):
    settings = settings_factory(
        ENABLE_PREWARM="true",
        PREWARM_LOOP_TICK_SEC="60",
        CACHE_TTL_TRENDING_SEC="60",
        CACHE_TTL_BILLBOARD_SEC="60",
    )
    fake_hot = FakeHotService()
    manager = PrewarmManager(lambda: fake_hot, settings, logging.getLogger(__name__))
    manager.start()

    assert wait_until(lambda: fake_hot.trending_calls >= 1 and fake_hot.billboard_calls >= 1)
    snapshot = manager.snapshot()
    manager.stop()

    assert snapshot["enabled"] is True
    assert snapshot["worker"]["iteration_count"] >= 1
    assert snapshot["endpoints"]["trending"]["success_count"] >= 1
    assert snapshot["endpoints"]["billboard"]["success_count"] >= 1


def test_prewarm_failure_updates_state_and_continues(settings_factory):
    settings = settings_factory(
        ENABLE_PREWARM="true",
        CACHE_TTL_TRENDING_SEC="60",
        CACHE_TTL_BILLBOARD_SEC="60",
    )
    fake_hot = FakeHotService()
    fake_hot.fail_trending = True
    manager = PrewarmManager(lambda: fake_hot, settings, logging.getLogger(__name__))

    manager.run_cycle_once(now_ts=100)
    first_snapshot = manager.snapshot()
    first_next_due = first_snapshot["endpoints"]["trending"]["next_due_at"]

    fake_hot.fail_trending = False
    manager.run_cycle_once(now_ts=first_next_due + 1)
    second_snapshot = manager.snapshot()

    assert first_snapshot["endpoints"]["trending"]["failure_count"] == 1
    assert first_snapshot["endpoints"]["trending"]["success_count"] == 0
    assert first_snapshot["endpoints"]["trending"]["last_error"] == "trending failed"
    assert second_snapshot["endpoints"]["trending"]["failure_count"] == 1
    assert second_snapshot["endpoints"]["trending"]["success_count"] == 1


def test_create_app_registers_prewarm_manager_and_health_adds_block(settings_factory):
    from app import create_app

    class StubClients:
        def call_ytmusic(self, method_name, *args, **kwargs):
            if method_name == "search":
                return []
            if method_name == "get_search_suggestions":
                return []
            return {}

        def http_get(self, *args, **kwargs):
            raise RuntimeError("unused")

    class StubHotService:
        def trending(self, country, limit_value):
            return [], "miss", False

        def recommendations(self, song_ids):
            return [], "miss", False

        def mix(self, artist_ids, limit_value):
            return [], "miss", False

        async def billboard(self):
            return {"data": [], "metadata": {}}, "miss", False

    settings = settings_factory(ENABLE_PREWARM="false")
    app = create_app(
        settings_obj=settings,
        clients_obj=StubClients(),
        hot_service_obj=StubHotService(),
    )
    assert "ytmusic_prewarm_manager" in app.extensions

    client = app.test_client()
    response = client.get("/health")
    body = response.get_json()

    assert response.status_code == 200
    assert "prewarm" in body
    assert "worker" in body["prewarm"]
    assert "endpoints" in body["prewarm"]
    assert "trending" in body["prewarm"]["endpoints"]
    assert "billboard" in body["prewarm"]["endpoints"]
