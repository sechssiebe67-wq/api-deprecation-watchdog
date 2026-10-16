"""Tests for the per-source per-scan alert fan-out cap (``max_alerts_per_scan``)."""
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
from apiwatch.notify import Notifier
from apiwatch.store import Store

URL = "https://example.com/feed"


def feed(*items):
    """Build an RSS feed from ``(title, body)`` pairs."""
    body = "".join(
        "<item><title>%s</title><description>%s</description>"
        "<link>https://example.com/%s</link></item>"
        % (title, description, title.replace(" ", "-"))
        for title, description in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>%s</channel></rss>' % body
    )


def critical_item(n):
    return ("Removal %d" % n, "this endpoint will be removed")


def warning_item(n):
    return ("Deprecation %d" % n, "this endpoint is deprecated")


def info_item(n):
    return ("Release %d" % n, "a new release announcement")


def ten_items():
    """Ten relevant items: 2 critical, 3 warning and 5 info."""
    return [
        critical_item(1),
        warning_item(1),
        warning_item(2),
        info_item(1),
        info_item(2),
        info_item(3),
        info_item(4),
        info_item(5),
        critical_item(2),
        warning_item(3),
    ]


class CapturingNotifier(Notifier):
    name = "capture"

    def __init__(self):
        self.batches = []
        self.digests = []

    def send(self, alerts, source_names):
        self.batches.append(list(alerts))

    def send_digest(self, digest, source_names):
        self.digests.append(digest)


class FanoutCapModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source()
        self.assertIsNone(source.max_alerts_per_scan)
        self.assertIsNone(source.to_dict()["max_alerts_per_scan"])

    def test_accepts_positive_integer(self):
        source = self._source(max_alerts_per_scan=3)
        self.assertEqual(source.max_alerts_per_scan, 3)
        self.assertEqual(source.to_dict()["max_alerts_per_scan"], 3)

    def test_rejects_bool_and_non_positive_and_non_integer(self):
        for value in (0, -1, True, False, "5", 2.5, [3]):
            with self.assertRaises(ValueError):
                self._source(max_alerts_per_scan=value)


class FanoutCapStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss", max_alerts_per_scan=4)
        )
        self.assertEqual(created.max_alerts_per_scan, 4)
        self.assertEqual(self.store.get_source(created.id).max_alerts_per_scan, 4)
        self.assertEqual(self.store.list_sources()[0].max_alerts_per_scan, 4)
        self.assertEqual(created.to_dict()["max_alerts_per_scan"], 4)

    def test_default_is_null(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertIsNone(created.max_alerts_per_scan)
        self.assertIsNone(self.store.get_source(created.id).max_alerts_per_scan)

    def test_migrates_old_db_without_fanout_column(self):
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
                for row in store._conn.execute(
                    "PRAGMA table_info(sources)"
                ).fetchall()
            }
            self.assertIn("max_alerts_per_scan", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.max_alerts_per_scan)
            fresh = store.add_source(
                Source(name="New", url=URL, kind="rss", max_alerts_per_scan=2)
            )
            self.assertEqual(fresh.max_alerts_per_scan, 2)
            store.close()
        finally:
            os.remove(path)


class FanoutCapEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.notifier = CapturingNotifier()

    def tearDown(self):
        self.store.close()

    def _source(self, items, **kwargs):
        self.fetcher = FakeFetcher({URL: feed(*items)})
        defaults = dict(name="Vendor", url=URL, kind="rss", min_severity="info")
        defaults.update(kwargs)
        return self.store.add_source(Source(**defaults))

    def test_no_cap_dispatches_every_alert(self):
        source = self._source([info_item(1), warning_item(1), critical_item(1)])
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        self.assertEqual(result.alerts_created, 3)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(
            sorted(a.severity for a in self.notifier.batches[0]),
            ["critical", "info", "warning"],
        )
        self.assertEqual(self.store.stats()["alerts_suppressed"], 0)
        self.assertEqual(self.store.list_alerts(notified=False), [])

    def test_cap_dispatches_top_severity_and_suppresses_rest(self):
        source = self._source(ten_items(), max_alerts_per_scan=3)
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        # All ten alerts are created and stored; only the top three fan out.
        self.assertEqual(result.alerts_created, 10)
        self.assertEqual(result.alerts_suppressed, 7)
        self.assertEqual(result.to_dict()["alerts_suppressed"], 7)
        self.assertEqual(len(self.notifier.batches), 1)
        delivered = self.notifier.batches[0]
        self.assertEqual(len(delivered), 3)
        # Highest severity first, stable on creation order within a severity.
        self.assertEqual(
            [a.title for a in delivered],
            ["Removal 1", "Removal 2", "Deprecation 1"],
        )

    def test_suppressed_alerts_stored_notified_zero(self):
        source = self._source(ten_items(), max_alerts_per_scan=3)
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        engine.scan_source(source)

        all_alerts = self.store.list_alerts(limit=100)
        self.assertEqual(len(all_alerts), 10)
        suppressed = self.store.list_alerts(limit=100, notified=False)
        self.assertEqual(len(suppressed), 7)
        delivered = self.store.list_alerts(limit=100, notified=True)
        self.assertEqual(len(delivered), 3)

    def test_cap_counts_into_stats(self):
        source = self._source(ten_items(), max_alerts_per_scan=3)
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        self.assertEqual(result.alerts_suppressed, 7)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 7)

    def test_cap_larger_than_alert_count_is_inert(self):
        source = self._source(ten_items(), max_alerts_per_scan=50)
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        self.assertEqual(result.alerts_created, 10)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches[0]), 10)

    def test_cap_composes_with_notification_floor(self):
        # Floor holds back the info alerts; the cap then limits above-floor alerts.
        source = self._source(
            ten_items(),
            notify_min_severity="warning",
            max_alerts_per_scan=2,
        )
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        # 5 info alerts are below the floor; of the 5 above-floor alerts the cap
        # only lets the 2 critical ones through.
        self.assertEqual(result.alerts_created, 10)
        self.assertEqual(result.alerts_suppressed, 3 + 5)
        delivered = self.notifier.batches[0]
        self.assertEqual([a.severity for a in delivered], ["critical", "critical"])


class FanoutCapDigestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.notifier = CapturingNotifier()

    def tearDown(self):
        self.store.close()

    def test_cap_defers_to_dispatch_in_digest_mode(self):
        fetcher = FakeFetcher({URL: feed(*ten_items())})
        source = self.store.add_source(
            Source(
                name="Vendor",
                url=URL,
                kind="rss",
                min_severity="info",
                notify_min_severity="warning",
                max_alerts_per_scan=2,
            )
        )
        engine = Engine(
            self.store,
            fetcher,
            notifiers=[self.notifier],
            digest_mode="digest",
        )
        result = engine.scan_source(source)

        # Nothing is dispatched immediately; the cap is deferred, so only the
        # five below-floor alerts are counted, exactly once (no double count).
        self.assertEqual(len(self.notifier.batches), 0)
        self.assertEqual(result.alerts_suppressed, 5)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 5)

        # All ten alerts remain digest-eligible (notified = 0).
        self.assertEqual(len(self.store.list_undigested_alerts()), 10)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        # Only the five above-floor alerts are claimed by the flush.
        self.assertEqual(digest.alert_count, 5)
        # The flush does not re-count the held-back alerts.
        self.assertEqual(self.store.stats()["alerts_suppressed"], 5)

    def test_cap_applies_to_both_mode_dispatch(self):
        fetcher = FakeFetcher({URL: feed(*ten_items())})
        source = self.store.add_source(
            Source(
                name="Vendor",
                url=URL,
                kind="rss",
                min_severity="info",
                max_alerts_per_scan=2,
            )
        )
        engine = Engine(
            self.store,
            fetcher,
            notifiers=[self.notifier],
            digest_mode="both",
        )
        result = engine.scan_source(source)

        # The immediate fan-out is capped at two, and the held-back alerts are
        # still eligible for the digest.
        self.assertEqual(len(self.notifier.batches[0]), 2)
        self.assertEqual(result.alerts_suppressed, 8)
        self.assertEqual(len(self.store.list_undigested_alerts()), 10)


class FanoutCapValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(validate_source_payload(self._payload()).max_alerts_per_scan)
        self.assertIsNone(
            validate_source_payload(
                self._payload(max_alerts_per_scan=None)
            ).max_alerts_per_scan
        )

    def test_positive_integer_accepted(self):
        self.assertEqual(
            validate_source_payload(
                self._payload(max_alerts_per_scan=7)
            ).max_alerts_per_scan,
            7,
        )

    def test_invalid_values_rejected_naming_field(self):
        for value in (0, -1, True, "5", 2.5, [3]):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(self._payload(max_alerts_per_scan=value))
            self.assertEqual(ctx.exception.status, 400, "value=%r" % (value,))
            self.assertIn("max_alerts_per_scan", ctx.exception.message)


class FanoutCapHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher({URL: feed(*ten_items())})
        self.engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
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

    def _create(self, **extra):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": URL, "kind": "rss", **extra},
        )
        self.assertEqual(status, 201)
        return json.loads(body)


class FanoutCapHttpTests(FanoutCapHttpBase):
    def test_create_and_read_field(self):
        source = self._create(max_alerts_per_scan=3)
        self.assertEqual(source["max_alerts_per_scan"], 3)

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["max_alerts_per_scan"], 3
        )

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["max_alerts_per_scan"], 3)

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["max_alerts_per_scan"])

    def test_invalid_values_rejected_over_http(self):
        for value in (0, -1, True, "5"):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "max_alerts_per_scan": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("max_alerts_per_scan", json.loads(body)["error"])

    def test_check_reports_suppressed_and_alerts_visible(self):
        self._create(max_alerts_per_scan=3)
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["alerts_created"], 10)
        self.assertEqual(result["alerts_suppressed"], 7)

        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alerts_suppressed"], 7)

        status, body = self.request("GET", "/api/alerts?limit=100")
        self.assertEqual(status, 200)
        alerts = json.loads(body)["alerts"]
        self.assertEqual(len(alerts), 10)
        self.assertEqual(sum(1 for a in alerts if not a["notified"]), 7)

    def test_regression_no_cap_dispatches_all(self):
        self._create()
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["alerts_created"], 10)
        self.assertEqual(result["alerts_suppressed"], 0)

    def test_export_includes_field(self):
        self._create(max_alerts_per_scan=3)
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        source = json.loads(body)["sources"][0]
        self.assertEqual(source["max_alerts_per_scan"], 3)

    def test_dashboard_renders_fanout_indicator(self):
        self._create(max_alerts_per_scan=3)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("fanout 3", body)

    def test_dashboard_shows_en_dash_when_uncapped(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("fanout \u2013", body)
        self.assertNotIn("fanout 3", body)


class SecuredFanoutCapHttpTests(FanoutCapHttpBase):
    token = "secret-token"

    def test_create_requires_token(self):
        status, _ = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": URL,
                "kind": "rss",
                "max_alerts_per_scan": 3,
            },
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": URL,
                "kind": "rss",
                "max_alerts_per_scan": 3,
            },
            token="secret-token",
        )
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
