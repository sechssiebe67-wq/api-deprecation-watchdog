"""Tests for the per-source failure circuit breaker."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import create_server
from apiwatch.config import Config
from apiwatch.dashboard import render_dashboard
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Source
from apiwatch.scheduler import Scheduler
from apiwatch.store import Store

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description></item>
</channel></rss>
"""

FEED_URL = "https://example.com/feed"


def _future_iso(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


class FakeClock:
    """A manually advanced wall clock so breaker windows are deterministic."""

    def __init__(self, now=None):
        self.now = now or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class ModelBreakerTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=FEED_URL, kind="rss", **kwargs)

    def test_defaults(self):
        source = self._source()
        self.assertEqual(source.failure_count, 0)
        self.assertIsNone(source.breaker_until)
        self.assertEqual(source.to_dict()["failure_count"], 0)
        self.assertIsNone(source.to_dict()["breaker_until"])

    def test_accepts_failure_count_and_breaker_until(self):
        source = self._source(
            failure_count=3, breaker_until="2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(source.failure_count, 3)
        self.assertEqual(source.breaker_until, "2026-01-01T00:00:00+00:00")

    def test_rejects_negative_failure_count(self):
        with self.assertRaises(ValueError):
            self._source(failure_count=-1)

    def test_rejects_bool_failure_count(self):
        with self.assertRaises(ValueError):
            self._source(failure_count=True)

    def test_rejects_non_integer_failure_count(self):
        with self.assertRaises(ValueError):
            self._source(failure_count="3")

    def test_rejects_unparseable_breaker_until(self):
        with self.assertRaises(ValueError):
            self._source(breaker_until="not-a-date")

    def test_accepts_z_suffix(self):
        source = self._source(breaker_until="2026-01-01T00:00:00Z")
        self.assertEqual(source.breaker_until, "2026-01-01T00:00:00Z")


class ConfigBreakerTests(unittest.TestCase):
    def test_defaults(self):
        config = Config.from_env({})
        self.assertEqual(config.breaker_threshold, 5)
        self.assertEqual(config.breaker_cooldown_seconds, 1800)

    def test_threshold_is_floored_at_one(self):
        self.assertEqual(
            Config.from_env({"BREAKER_THRESHOLD": "0"}).breaker_threshold, 1
        )
        self.assertEqual(
            Config.from_env({"BREAKER_THRESHOLD": "-4"}).breaker_threshold, 1
        )

    def test_cooldown_is_floored_at_sixty(self):
        self.assertEqual(
            Config.from_env({"BREAKER_COOLDOWN_SECONDS": "10"}).breaker_cooldown_seconds,
            60,
        )

    def test_custom_values(self):
        config = Config.from_env(
            {"BREAKER_THRESHOLD": "3", "BREAKER_COOLDOWN_SECONDS": "120"}
        )
        self.assertEqual(config.breaker_threshold, 3)
        self.assertEqual(config.breaker_cooldown_seconds, 120)


class StoreBreakerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_failure_and_success_round_trip(self):
        source = self.store.add_source(
            Source(name="s", url=FEED_URL, kind="rss")
        )
        self.store.record_source_failure(source.id, 4, "2030-01-01T00:00:00+00:00")
        fetched = self.store.get_source(source.id)
        self.assertEqual(fetched.failure_count, 4)
        self.assertEqual(fetched.breaker_until, "2030-01-01T00:00:00+00:00")
        data = fetched.to_dict()
        self.assertEqual(data["failure_count"], 4)
        self.assertEqual(data["breaker_until"], "2030-01-01T00:00:00+00:00")

        self.store.record_source_success(source.id)
        cleared = self.store.get_source(source.id)
        self.assertEqual(cleared.failure_count, 0)
        self.assertIsNone(cleared.breaker_until)

    def test_stats_counts_only_future_breakers(self):
        tripped = self.store.add_source(Source(name="a", url=FEED_URL, kind="rss"))
        expired = self.store.add_source(
            Source(name="b", url=FEED_URL + "2", kind="rss")
        )
        self.store.record_source_failure(tripped.id, 5, _future_iso())
        self.store.record_source_failure(
            expired.id, 5, "2000-01-01T00:00:00+00:00"
        )
        self.assertEqual(self.store.stats()["sources_tripped"], 1)

    def test_migrates_old_db_without_breaker_columns(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    api_name TEXT NOT NULL DEFAULT '',
                    tracked_versions TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selector TEXT,
                    min_severity TEXT NOT NULL DEFAULT 'info',
                    notify_channels TEXT NOT NULL DEFAULT '["all"]',
                    notify_webhook_url TEXT,
                    scan_interval_seconds INTEGER,
                    headers TEXT,
                    auth TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, notify_webhook_url,
                     scan_interval_seconds, headers, auth, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1,
                        NULL, 'info', '["all"]', NULL, NULL, NULL, NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("failure_count", columns)
            self.assertIn("breaker_until", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.failure_count, 0)
            self.assertIsNone(legacy.breaker_until)
            store.close()
        finally:
            os.remove(path)


class EngineBreakerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.source = self.store.add_source(
            Source(name="Example", url=FEED_URL, kind="rss")
        )
        self.clock = FakeClock()
        self.fetcher = FakeFetcher({})
        self.engine = Engine(
            self.store,
            self.fetcher,
            breaker_threshold=3,
            breaker_cooldown_seconds=1800,
            clock=self.clock,
        )

    def tearDown(self):
        self.store.close()

    def _source(self):
        return self.store.get_source(self.source.id)

    def test_threshold_trips_then_skips(self):
        for expected in (1, 2):
            result = self.engine.scan_source(self._source())
            self.assertEqual(result.sources_failed, 1)
            self.assertEqual(result.sources_skipped, 0)
            self.assertEqual(self._source().failure_count, expected)
            self.assertIsNone(self._source().breaker_until)

        result = self.engine.scan_source(self._source())
        self.assertEqual(result.sources_failed, 1)
        tripped = self._source()
        self.assertEqual(tripped.failure_count, 3)
        self.assertIsNotNone(tripped.breaker_until)
        self.assertTrue(self.engine.is_tripped(tripped))
        self.assertTrue(
            any("circuit breaker tripped" in error for error in result.errors)
        )

        calls_before = len(self.fetcher.calls)
        skipped = self.engine.scan_source(self._source())
        self.assertEqual(skipped.sources_skipped, 1)
        self.assertEqual(skipped.sources_failed, 0)
        self.assertEqual(skipped.sources_checked, 0)
        self.assertEqual(skipped.errors, [])
        self.assertEqual(len(self.fetcher.calls), calls_before)

    def test_success_after_cooldown_resets(self):
        for _ in range(3):
            self.engine.scan_source(self._source())
        self.assertTrue(self.engine.is_tripped(self._source()))

        self.clock.advance(1801)
        self.assertFalse(self.engine.is_tripped(self._source()))

        self.fetcher.responses[FEED_URL] = RSS
        result = self.engine.scan_source(self._source())
        self.assertEqual(result.sources_checked, 1)
        recovered = self._source()
        self.assertEqual(recovered.failure_count, 0)
        self.assertIsNone(recovered.breaker_until)

    def test_success_before_threshold_resets(self):
        self.engine.scan_source(self._source())
        self.assertEqual(self._source().failure_count, 1)
        self.fetcher.responses[FEED_URL] = RSS
        self.engine.scan_source(self._source())
        recovered = self._source()
        self.assertEqual(recovered.failure_count, 0)
        self.assertIsNone(recovered.breaker_until)

    def test_scan_all_skips_tripped_and_scans_healthy(self):
        healthy_url = "https://example.com/healthy"
        self.store.add_source(Source(name="Healthy", url=healthy_url, kind="rss"))
        self.fetcher.responses[healthy_url] = RSS
        self.store.record_source_failure(self.source.id, 3, _future_iso())
        result = self.engine.scan_all()
        self.assertEqual(result.sources_checked, 1)
        self.assertEqual(result.sources_skipped, 1)


class SchedulerBreakerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.healthy = self.store.add_source(
            Source(
                name="Healthy",
                url="https://example.com/healthy",
                kind="rss",
                scan_interval_seconds=3600,
            )
        )
        self.tripped = self.store.add_source(
            Source(
                name="Tripped",
                url="https://example.com/tripped",
                kind="rss",
                scan_interval_seconds=30,
            )
        )
        self.store.record_source_failure(self.tripped.id, 5, _future_iso(1800))
        self.fetcher = FakeFetcher(
            {
                "https://example.com/healthy": RSS,
                "https://example.com/tripped": RSS,
            }
        )
        self.engine = Engine(self.store, self.fetcher)
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()

    def test_tripped_source_not_due(self):
        scheduler = Scheduler(self.engine, interval_seconds=3600, clock=self.clock)
        due_ids = [source.id for source in scheduler.due_sources()]
        self.assertIn(self.healthy.id, due_ids)
        self.assertNotIn(self.tripped.id, due_ids)

    def test_run_pending_does_not_fetch_tripped(self):
        scheduler = Scheduler(self.engine, interval_seconds=3600, clock=self.clock)
        scheduler.run_pending(now=0)
        self.assertIn("https://example.com/healthy", self.fetcher.calls)
        self.assertNotIn("https://example.com/tripped", self.fetcher.calls)

    def test_tripped_cooldown_does_not_delay_wakeup(self):
        scheduler = Scheduler(self.engine, interval_seconds=3600, clock=self.clock)
        # The tripped source has a 30s interval but is quarantined, so it must not
        # drag the loop's wake-up down to 30s.
        self.assertEqual(scheduler._sleep_seconds(), 3600)


class DashboardBreakerTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="Example", url=FEED_URL, kind="rss", id=1, **kwargs)

    def test_health_column_ok(self):
        html = render_dashboard({}, [self._source()], [])
        self.assertIn("Health", html)
        self.assertIn(">ok<", html)

    def test_health_column_failures(self):
        html = render_dashboard({}, [self._source(failure_count=2)], [])
        self.assertIn("2 failures", html)

    def test_health_column_tripped_with_reset(self):
        html = render_dashboard(
            {}, [self._source(failure_count=5, breaker_until=_future_iso())], []
        )
        self.assertIn("tripped until", html)
        self.assertIn("/api/sources/1/reset", html)

    def test_expired_breaker_not_tripped(self):
        html = render_dashboard(
            {},
            [self._source(failure_count=5, breaker_until="2000-01-01T00:00:00+00:00")],
            [],
        )
        self.assertNotIn("tripped until", html)
        self.assertIn("5 failures", html)

    def test_never_renders_credentials(self):
        html = render_dashboard(
            {},
            [self._source(headers={"X-Api-Key": "topsecret"}, auth="u:hunter2")],
            [],
        )
        self.assertNotIn("topsecret", html)
        self.assertNotIn("hunter2", html)


class AppBreakerTestCase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(self.store, FakeFetcher({}))
        self.config = Config(
            host="127.0.0.1",
            port=0,
            disable_scheduler=True,
            seed_sources=False,
            api_token=self.token,
        )
        self.server = create_server(
            self.store, self.engine, self.config, host="127.0.0.1", port=0
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    def request(self, method, path, payload=None, token=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


class OpenAppBreakerTests(AppBreakerTestCase):
    def test_sources_expose_breaker_fields(self):
        source = self.store.add_source(
            Source(name="s", url=FEED_URL, kind="rss", failure_count=2)
        )
        status, body = self.request("GET", "/api/sources/%d" % source.id)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["failure_count"], 2)
        self.assertIsNone(payload["breaker_until"])

    def test_stats_reports_sources_tripped(self):
        tripped = self.store.add_source(Source(name="a", url=FEED_URL, kind="rss"))
        self.store.record_source_failure(tripped.id, 5, _future_iso())
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources_tripped"], 1)

    def test_reset_clears_state(self):
        source = self.store.add_source(Source(name="a", url=FEED_URL, kind="rss"))
        self.store.record_source_failure(source.id, 5, _future_iso())
        status, body = self.request("POST", "/api/sources/%d/reset" % source.id)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["failure_count"], 0)
        self.assertIsNone(payload["breaker_until"])
        self.assertEqual(self.store.get_source(source.id).failure_count, 0)

    def test_reset_unknown_source_404(self):
        status, body = self.request("POST", "/api/sources/999999/reset")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "source not found")

    def test_reset_invalid_id_400(self):
        status, body = self.request("POST", "/api/sources/abc/reset")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid id")


class SecuredAppBreakerTests(AppBreakerTestCase):
    token = "secret-token"

    def test_reset_requires_token(self):
        source = self.store.add_source(Source(name="a", url=FEED_URL, kind="rss"))
        self.store.record_source_failure(source.id, 5, _future_iso())
        status, _ = self.request("POST", "/api/sources/%d/reset" % source.id)
        self.assertEqual(status, 401)
        self.assertEqual(self.store.get_source(source.id).failure_count, 5)

        status, body = self.request(
            "POST", "/api/sources/%d/reset" % source.id, token="secret-token"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["failure_count"], 0)

    def test_reset_unknown_with_token_404(self):
        status, _ = self.request(
            "POST", "/api/sources/999999/reset", token="secret-token"
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
