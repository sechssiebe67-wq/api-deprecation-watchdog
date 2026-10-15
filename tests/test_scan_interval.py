"""Tests for per-source scan intervals (``scan_interval_seconds``)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import ApiError, create_server, validate_source_payload
from apiwatch.config import Config
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


class FakeClock:
    """A manually advanced clock so scheduler timing is deterministic."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ModelScanIntervalTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url="https://example.com/x", kind="rss", **kwargs)

    def test_defaults_to_none(self):
        self.assertIsNone(self._source().scan_interval_seconds)
        self.assertIsNone(self._source().to_dict()["scan_interval_seconds"])

    def test_accepts_positive_integer(self):
        source = self._source(scan_interval_seconds=900)
        self.assertEqual(source.scan_interval_seconds, 900)
        self.assertEqual(source.to_dict()["scan_interval_seconds"], 900)

    def test_rejects_zero_and_negative(self):
        for value in (0, -1, -900):
            with self.assertRaises(ValueError):
                self._source(scan_interval_seconds=value)

    def test_rejects_non_integers(self):
        for value in (900.5, "900", True, [900]):
            with self.assertRaises(ValueError):
                self._source(scan_interval_seconds=value)


class ConfigScanIntervalTests(unittest.TestCase):
    def test_poll_interval_is_global_default(self):
        self.assertEqual(Config.from_env({}).poll_interval_seconds, 3600)
        self.assertEqual(
            Config.from_env({"POLL_INTERVAL_SECONDS": "900"}).poll_interval_seconds,
            900,
        )


class StoreScanIntervalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(
                name="s",
                url="https://example.com/x",
                kind="rss",
                scan_interval_seconds=900,
            )
        )
        self.assertEqual(created.scan_interval_seconds, 900)
        self.assertEqual(self.store.get_source(created.id).scan_interval_seconds, 900)
        self.assertEqual(self.store.list_sources()[0].scan_interval_seconds, 900)
        self.assertEqual(created.to_dict()["scan_interval_seconds"], 900)

    def test_default_round_trip_is_none(self):
        created = self.store.add_source(
            Source(name="s", url="https://example.com/x", kind="rss")
        )
        self.assertIsNone(created.scan_interval_seconds)
        self.assertIsNone(self.store.get_source(created.id).scan_interval_seconds)

    def test_migrates_old_db_without_column(self):
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
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1,
                        NULL, 'info', '["all"]', '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("scan_interval_seconds", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.scan_interval_seconds)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    scan_interval_seconds=120,
                )
            )
            self.assertEqual(fresh.scan_interval_seconds, 120)
            store.close()
        finally:
            os.remove(path)


class AppScanIntervalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(
            self.store,
            FakeFetcher({"https://example.com/x": RSS, "https://example.com/y": RSS}),
        )
        self.config = Config(
            host="127.0.0.1",
            port=0,
            disable_scheduler=True,
            seed_sources=False,
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

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_accepts_and_returns_interval(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 900,
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["scan_interval_seconds"], 900)

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["scan_interval_seconds"], 900)

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["scan_interval_seconds"], 900
        )

    def test_missing_interval_is_null(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": "https://example.com/x", "kind": "rss"},
        )
        self.assertEqual(status, 201)
        self.assertIsNone(json.loads(body)["scan_interval_seconds"])

    def test_invalid_intervals_rejected(self):
        for value in (0, -1, 900.5, "900", True):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Example",
                    "url": "https://example.com/x",
                    "kind": "rss",
                    "scan_interval_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("scan_interval_seconds", json.loads(body)["error"])

    def test_validate_source_payload_returns_value(self):
        source = validate_source_payload(
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 15,
            }
        )
        self.assertEqual(source.scan_interval_seconds, 15)

    def test_dashboard_shows_interval_column(self):
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Fast",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 900,
            },
        )
        self.request(
            "POST",
            "/api/sources",
            {"name": "Default", "url": "https://example.com/y", "kind": "rss"},
        )
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Interval", body)
        self.assertIn("900s", body)
        self.assertIn("3600s (global)", body)

    def test_dashboard_renders_with_no_sources(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Interval", body)
        self.assertIn("No sources registered yet.", body)

    def test_manual_check_scans_all_regardless_of_interval(self):
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Fast",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 15,
            },
        )
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Slow",
                "url": "https://example.com/y",
                "kind": "rss",
                "scan_interval_seconds": 86400,
            },
        )
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources_checked"], 2)


class EngineScanAllTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_scan_all_ignores_intervals(self):
        url_a = "https://example.com/a"
        url_b = "https://example.com/b"
        self.store.add_source(
            Source(
                name="Fast",
                url=url_a,
                kind="rss",
                scan_interval_seconds=15,
            )
        )
        self.store.add_source(
            Source(name="Slow", url=url_b, kind="rss", scan_interval_seconds=86400)
        )
        fetcher = FakeFetcher({url_a: RSS, url_b: RSS})
        result = Engine(self.store, fetcher).scan_all()
        self.assertEqual(result.sources_checked, 2)
        self.assertCountEqual(fetcher.calls, [url_a, url_b])


class SchedulerIntervalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"
        self.source = self.store.add_source(
            Source(name="Example", url=self.url, kind="rss")
        )
        self.fetcher = FakeFetcher({self.url: RSS})
        self.engine = Engine(self.store, self.fetcher)
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()

    def _scheduler(self, interval=3600):
        return Scheduler(self.engine, interval_seconds=interval, clock=self.clock)

    def test_effective_interval_defaults_to_global(self):
        scheduler = self._scheduler(3600)
        self.assertEqual(scheduler.effective_interval(self.source), 3600)

    def test_effective_interval_override(self):
        source = self.store.add_source(
            Source(name="Fast", url=self.url + "2", kind="rss", scan_interval_seconds=900)
        )
        scheduler = self._scheduler(3600)
        self.assertEqual(scheduler.effective_interval(source), 900)

    def test_effective_interval_is_floored(self):
        source = self.store.add_source(
            Source(name="Tiny", url=self.url + "3", kind="rss", scan_interval_seconds=5)
        )
        scheduler = self._scheduler(3600)
        self.assertEqual(scheduler.effective_interval(source), 30)

    def test_first_tick_scans_everything(self):
        scheduler = self._scheduler()
        self.assertEqual(
            [s.id for s in scheduler.due_sources()], [self.source.id]
        )

    def test_not_due_before_interval_and_due_after(self):
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        self.assertEqual(scheduler.due_sources(now=3599), [])
        self.assertEqual(
            [s.id for s in scheduler.due_sources(now=3600)], [self.source.id]
        )

    def test_run_pending_scans_only_due_sources(self):
        slow_url = "https://example.com/slow"
        slow = self.store.add_source(
            Source(name="Slow", url=slow_url, kind="rss", scan_interval_seconds=7200)
        )
        self.fetcher.responses[slow_url] = RSS
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        self.fetcher.calls.clear()
        # At t=3600 only the global-interval source is due.
        scheduler.run_pending(now=3600)
        self.assertEqual(self.fetcher.calls, [self.url])
        # At t=7200 both are due.
        self.fetcher.calls.clear()
        scheduler.run_pending(now=7200)
        self.assertCountEqual(self.fetcher.calls, [self.url, slow_url])

    def test_disabled_source_is_never_due(self):
        disabled = self.store.add_source(
            Source(name="Off", url=self.url + "-off", kind="rss", enabled=False)
        )
        scheduler = self._scheduler()
        self.assertNotIn(disabled.id, [s.id for s in scheduler.due_sources()])

    def test_interval_is_validated_on_store(self):
        with self.assertRaises(ValueError):
            Source(name="Bad", url=self.url + "-bad", kind="rss", scan_interval_seconds=0)

    def test_short_interval_scanned_four_times_as_often(self):
        short_url = "https://example.com/short"
        long_url = "https://example.com/long"
        self.store.add_source(
            Source(
                name="Short",
                url=short_url,
                kind="rss",
                scan_interval_seconds=900,
            )
        )
        self.store.add_source(
            Source(name="Long", url=long_url, kind="rss")
        )
        self.fetcher.responses[short_url] = RSS
        self.fetcher.responses[long_url] = RSS
        scheduler = self._scheduler(3600)

        # Baseline: the first tick covers everything.
        scheduler.run_pending(now=0)
        short_before = self.fetcher.calls.count(short_url)
        long_before = self.fetcher.calls.count(long_url)

        # Simulate one global interval, stepping at the short cadence.
        for step in range(1, 5):
            scheduler.run_pending(now=step * 900)

        short_scans = self.fetcher.calls.count(short_url) - short_before
        long_scans = self.fetcher.calls.count(long_url) - long_before
        self.assertEqual(short_scans, 4)
        self.assertEqual(long_scans, 1)
        self.assertGreaterEqual(short_scans, 4 * long_scans)


if __name__ == "__main__":
    unittest.main()
