"""Tests for per-source scan jitter (``scan_jitter_seconds``)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import create_server, validate_source_payload
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


class ModelScanJitterTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url="https://example.com/x", kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source()
        self.assertIsNone(source.scan_jitter_seconds)
        self.assertIsNone(source.to_dict()["scan_jitter_seconds"])

    def test_accepts_non_negative_integer(self):
        source = self._source(scan_jitter_seconds=300)
        self.assertEqual(source.scan_jitter_seconds, 300)
        self.assertEqual(source.to_dict()["scan_jitter_seconds"], 300)

    def test_zero_normalizes_to_none(self):
        source = self._source(scan_jitter_seconds=0)
        self.assertIsNone(source.scan_jitter_seconds)
        self.assertIsNone(source.to_dict()["scan_jitter_seconds"])

    def test_rejects_negative_and_non_integers(self):
        for value in (-1, 1.5, "300", True, False, [300]):
            with self.assertRaises(ValueError, msg="value=%r" % (value,)):
                self._source(scan_jitter_seconds=value)


class StoreScanJitterTests(unittest.TestCase):
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
                scan_jitter_seconds=300,
            )
        )
        self.assertEqual(created.scan_jitter_seconds, 300)
        self.assertEqual(
            self.store.get_source(created.id).scan_jitter_seconds, 300
        )
        self.assertEqual(self.store.list_sources()[0].scan_jitter_seconds, 300)
        self.assertEqual(created.to_dict()["scan_jitter_seconds"], 300)

    def test_default_round_trip_is_none(self):
        created = self.store.add_source(
            Source(name="s", url="https://example.com/x", kind="rss")
        )
        self.assertIsNone(created.scan_jitter_seconds)
        self.assertIsNone(self.store.get_source(created.id).scan_jitter_seconds)

    def test_zero_is_stored_as_no_jitter(self):
        created = self.store.add_source(
            Source(
                name="s",
                url="https://example.com/x",
                kind="rss",
                scan_jitter_seconds=0,
            )
        )
        self.assertIsNone(created.scan_jitter_seconds)

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
            self.assertIn("scan_jitter_seconds", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.scan_jitter_seconds)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    scan_jitter_seconds=120,
                )
            )
            self.assertEqual(fresh.scan_jitter_seconds, 120)
            store.close()
        finally:
            os.remove(path)

    def test_old_row_with_zero_reads_back_as_no_jitter(self):
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
                    scan_jitter_seconds INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, scan_jitter_seconds, created_at)
                VALUES ('Zero', 'https://zero.example/feed', 'rss', '', '[]', 1,
                        NULL, 'info', '["all"]', 0, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            self.assertIsNone(store.list_sources()[0].scan_jitter_seconds)
            store.close()
        finally:
            os.remove(path)


class AppScanJitterTests(unittest.TestCase):
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

    def test_accepts_and_returns_jitter(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_jitter_seconds": 300,
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["scan_jitter_seconds"], 300)

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["scan_jitter_seconds"], 300)

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["scan_jitter_seconds"], 300
        )

    def test_omitted_and_null_and_zero_are_no_jitter(self):
        for payload in (
            {"name": "Omitted", "url": "https://example.com/x", "kind": "rss"},
            {
                "name": "Null",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_jitter_seconds": None,
            },
            {
                "name": "Zero",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_jitter_seconds": 0,
            },
        ):
            status, body = self.request("POST", "/api/sources", payload)
            self.assertEqual(status, 201, payload)
            self.assertIsNone(json.loads(body)["scan_jitter_seconds"], payload)

    def test_invalid_jitter_rejected(self):
        for value in (-1, -300, 300.5, "300", True, False, [300]):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Example",
                    "url": "https://example.com/x",
                    "kind": "rss",
                    "scan_jitter_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("scan_jitter_seconds", json.loads(body)["error"])

    def test_validate_source_payload_returns_value(self):
        source = validate_source_payload(
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_jitter_seconds": 15,
            }
        )
        self.assertEqual(source.scan_jitter_seconds, 15)

    def test_dashboard_shows_jitter_window(self):
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Jittered",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 900,
                "scan_jitter_seconds": 300,
            },
        )
        self.request(
            "POST",
            "/api/sources",
            {"name": "Plain", "url": "https://example.com/y", "kind": "rss"},
        )
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Interval", body)
        self.assertIn("900s +0-300s", body)
        self.assertIn("3600s (global)", body)

    def test_manual_check_scans_jittered_source_immediately(self):
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Jittered",
                "url": "https://example.com/x",
                "kind": "rss",
                "scan_interval_seconds": 86400,
                "scan_jitter_seconds": 3600,
            },
        )
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources_checked"], 1)


class EngineScanJitterTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_scan_all_ignores_jitter(self):
        url = "https://example.com/a"
        self.store.add_source(
            Source(
                name="Jittered",
                url=url,
                kind="rss",
                scan_interval_seconds=86400,
                scan_jitter_seconds=3600,
            )
        )
        fetcher = FakeFetcher({url: RSS})
        result = Engine(self.store, fetcher).scan_all()
        self.assertEqual(result.sources_checked, 1)
        self.assertEqual(fetcher.calls, [url])


class SchedulerJitterTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"
        self.source = self.store.add_source(
            Source(
                name="Example",
                url=self.url,
                kind="rss",
                scan_interval_seconds=3600,
                scan_jitter_seconds=100,
            )
        )
        self.fetcher = FakeFetcher({self.url: RSS})
        self.engine = Engine(self.store, self.fetcher)
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()

    def _scheduler(self, interval=3600):
        return Scheduler(self.engine, interval_seconds=interval, clock=self.clock)

    def _offset(self):
        return self.source.id % (100 + 1)

    def test_offset_is_deterministic(self):
        scheduler = self._scheduler()
        offset = scheduler.jitter_offset(self.source)
        self.assertEqual(offset, self._offset())
        self.assertEqual(scheduler.jitter_offset(self.source), offset)
        self.assertEqual(
            scheduler.jitter_offset(self.store.get_source(self.source.id)), offset
        )

    def test_offset_is_zero_without_jitter(self):
        plain = self.store.add_source(
            Source(name="Plain", url=self.url + "2", kind="rss")
        )
        scheduler = self._scheduler()
        self.assertEqual(scheduler.jitter_offset(plain), 0)

    def test_jitter_delays_due_time(self):
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        due_at = 3600 + self._offset()
        self.assertEqual(scheduler.due_sources(now=due_at - 1), [])
        self.assertEqual(
            [s.id for s in scheduler.due_sources(now=due_at)], [self.source.id]
        )

    def test_no_jitter_is_unchanged(self):
        plain = self.store.add_source(
            Source(
                name="Plain",
                url=self.url + "3",
                kind="rss",
                scan_interval_seconds=3600,
            )
        )
        self.fetcher.responses[plain.url] = RSS
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        self.assertNotIn(plain.id, [s.id for s in scheduler.due_sources(now=3599)])
        self.assertIn(plain.id, [s.id for s in scheduler.due_sources(now=3600)])

    def test_wakes_at_smallest_effective_due_time(self):
        fast = self.store.add_source(
            Source(
                name="Fast",
                url=self.url + "4",
                kind="rss",
                scan_interval_seconds=900,
                scan_jitter_seconds=300,
            )
        )
        self.fetcher.responses[fast.url] = RSS
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        # ``fast`` waits 900 + offset seconds; the 3600s source must not hold it
        # back and the jittered source must not be woken early.
        expected = 900 + scheduler.jitter_offset(fast)
        self.assertEqual(scheduler._sleep_seconds(now=0), expected)
        self.assertEqual(scheduler._sleep_seconds(now=100), expected - 100)

    def test_does_not_busy_loop_while_jittered_source_waits(self):
        fast = self.store.add_source(
            Source(
                name="Fast",
                url=self.url + "5",
                kind="rss",
                scan_interval_seconds=900,
                scan_jitter_seconds=100,
            )
        )
        self.fetcher.responses[fast.url] = RSS
        scheduler = self._scheduler(3600)
        scheduler.run_pending(now=0)
        wait = scheduler._sleep_seconds(now=0)
        self.assertGreater(wait, 0)
        self.assertEqual(wait, 900 + scheduler.jitter_offset(fast))
        # The loop sleeps until the offset elapses instead of spinning.
        self.assertNotIn(fast.id, [s.id for s in scheduler.due_sources(now=wait - 1)])
        self.assertIn(fast.id, [s.id for s in scheduler.due_sources(now=wait)])


if __name__ == "__main__":
    unittest.main()
