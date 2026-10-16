"""Tests for the per-source notification floor (``notify_min_severity``)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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
URL_B = "https://example.com/other"
NOW = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)

INFO_ITEM = ("Release 2.0", "a new version announcement")
WARNING_ITEM = ("Legacy API", "deprecated")
CRITICAL_ITEM = ("API removal", "will be removed")


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


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


class CapturingNotifier(Notifier):
    name = "capture"

    def __init__(self):
        self.batches = []
        self.digests = []

    def send(self, alerts, source_names):
        self.batches.append(list(alerts))

    def send_digest(self, digest, source_names):
        self.digests.append(digest)


class NotifyFloorModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source(min_severity="info")
        self.assertIsNone(source.notify_min_severity)
        self.assertIsNone(source.to_dict()["notify_min_severity"])

    def test_accepts_severities_case_insensitively(self):
        for value, expected in (
            ("critical", "critical"),
            ("Warning", "warning"),
            ("  INFO ", "info"),
        ):
            source = self._source(notify_min_severity=value)
            self.assertEqual(source.notify_min_severity, expected)
            self.assertEqual(source.to_dict()["notify_min_severity"], expected)

    def test_rejects_bogus_and_non_string(self):
        for value in ("bogus", "", "  ", 5, True, ["critical"]):
            with self.assertRaises(ValueError):
                self._source(notify_min_severity=value)


class NotifyFloorStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(
                name="s",
                url=URL,
                kind="rss",
                min_severity="info",
                notify_min_severity="critical",
            )
        )
        self.assertEqual(created.notify_min_severity, "critical")
        self.assertEqual(
            self.store.get_source(created.id).notify_min_severity, "critical"
        )
        self.assertEqual(
            self.store.list_sources()[0].notify_min_severity, "critical"
        )
        self.assertEqual(created.to_dict()["notify_min_severity"], "critical")

    def test_default_is_null(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertIsNone(created.notify_min_severity)
        self.assertIsNone(self.store.get_source(created.id).notify_min_severity)

    def test_migrates_old_db_without_notify_floor_column(self):
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
                    scan_interval_seconds INTEGER,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, scan_interval_seconds, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1,
                        NULL, 'warning', '["all"]', NULL, '2024-01-01');
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
            self.assertIn("notify_min_severity", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.notify_min_severity)
            # The migrated source inherits its own min_severity.
            self.assertEqual(legacy.min_severity, "warning")
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    notify_min_severity="critical",
                )
            )
            self.assertEqual(fresh.notify_min_severity, "critical")
            store.close()
        finally:
            os.remove(path)


class NotifyFloorEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher(
            {URL: feed(INFO_ITEM, WARNING_ITEM, CRITICAL_ITEM)}
        )

    def tearDown(self):
        self.store.close()

    def _source(self, **kwargs):
        defaults = dict(
            name="Vendor",
            url=URL,
            kind="rss",
            min_severity="info",
            notify_min_severity="critical",
        )
        defaults.update(kwargs)
        return self.store.add_source(Source(**defaults))

    def test_below_floor_stored_but_only_floor_delivered(self):
        source = self._source()
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)

        self.assertEqual(result.alerts_created, 3)
        self.assertEqual(result.alerts_suppressed, 2)
        self.assertEqual(result.to_dict()["alerts_suppressed"], 2)
        # Only the critical alert is fanned out.
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(
            sorted(a.severity for a in self.notifier.batches[0]), ["critical"]
        )
        # Every alert is stored; below-floor ones keep notified = 0.
        stored = {a.severity: a for a in self.store.list_alerts()}
        self.assertEqual(set(stored), {"critical", "warning", "info"})
        self.assertTrue(stored["critical"].notified)
        self.assertFalse(stored["warning"].notified)
        self.assertFalse(stored["info"].notified)
        # The held-back count reaches the aggregate stats.
        self.assertEqual(self.store.stats()["alerts_suppressed"], 2)

    def test_below_floor_still_visible_in_entries(self):
        source = self._source()
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        engine.scan_source(source)
        severities = {a.severity for a in self.store.list_alerts()}
        self.assertEqual(severities, {"critical", "warning", "info"})
        self.assertEqual(len(self.store.list_entries()), 3)

    def test_floor_composes_with_cooldown(self):
        # notify floor = warning, cooldown = 300s.
        source = self._source(
            notify_min_severity="warning", alert_cooldown_seconds=300
        )
        clock = FakeClock()
        engine = Engine(
            self.store, self.fetcher, notifiers=[self.notifier], clock=clock
        )
        first = engine.scan_source(source)
        # info is below the warning floor; warning + critical dispatched.
        self.assertEqual(first.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = feed(
            ("Release 3.0", "a new version announcement"),
            ("Legacy API 2", "deprecated"),
            ("API removal 2", "will be removed"),
        )
        clock.advance(50)
        second = engine.scan_source(source)
        # info still below the floor, warning + critical inside the cooldown.
        self.assertEqual(second.alerts_suppressed, 3)
        self.assertEqual(len(self.notifier.batches), 1)

    def test_omitting_field_reproduces_prefeature_behaviour(self):
        source = self.store.add_source(
            Source(name="Plain", url=URL, kind="rss", min_severity="info")
        )
        engine = Engine(self.store, self.fetcher, notifiers=[self.notifier])
        result = engine.scan_source(source)
        self.assertEqual(result.alerts_created, 3)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(
            sorted(a.severity for a in self.notifier.batches[0]),
            ["critical", "info", "warning"],
        )
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_digest_mode_flush_does_not_claim_below_floor(self):
        source = self._source(min_severity="info", notify_min_severity="critical")
        engine = Engine(
            self.store,
            self.fetcher,
            notifiers=[self.notifier],
            digest_mode="digest",
        )
        result = engine.scan_source(source)
        # info + warning are held back by the floor and accounted once.
        self.assertEqual(result.alerts_suppressed, 2)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 2)

        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 1)
        stored = {a.severity: a for a in self.store.list_alerts()}
        self.assertTrue(stored["critical"].notified)
        self.assertIsNotNone(stored["critical"].digest_id)
        # Below-floor alerts are left pending and unclaimed.
        self.assertFalse(stored["warning"].notified)
        self.assertIsNone(stored["warning"].digest_id)
        self.assertFalse(stored["info"].notified)
        self.assertIsNone(stored["info"].digest_id)
        # A second flush has nothing eligible.
        self.assertIsNone(engine.flush_digest())
        # The held-back count is not re-counted by flushes.
        self.assertEqual(self.store.stats()["alerts_suppressed"], 2)

    def test_digest_mode_delivers_once_floor_lowered(self):
        source = self._source(min_severity="info", notify_min_severity="critical")
        engine = Engine(
            self.store,
            self.fetcher,
            notifiers=[self.notifier],
            digest_mode="digest",
        )
        engine.scan_source(source)
        first = engine.flush_digest()
        self.assertEqual(first.alert_count, 1)

        # Lower the floor: the previously held-back warning is now eligible.
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE sources SET notify_min_severity = 'warning' WHERE id = ?",
                (source.id,),
            )
            self.store._conn.commit()

        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 1)
        stored = {a.severity: a for a in self.store.list_alerts()}
        self.assertTrue(stored["warning"].notified)
        # info is still below the lowered floor.
        self.assertFalse(stored["info"].notified)


class NotifyFloorValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(
            validate_source_payload(self._payload()).notify_min_severity
        )
        self.assertIsNone(
            validate_source_payload(
                self._payload(notify_min_severity=None)
            ).notify_min_severity
        )

    def test_severities_accepted_case_insensitively(self):
        for value, expected in (
            ("critical", "critical"),
            ("Warning", "warning"),
            (" INFO ", "info"),
        ):
            self.assertEqual(
                validate_source_payload(
                    self._payload(notify_min_severity=value)
                ).notify_min_severity,
                expected,
            )

    def test_invalid_values_rejected_naming_field(self):
        for value in ("bogus", "", 5, True, ["critical"], {"a": 1}):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(
                    self._payload(notify_min_severity=value)
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("notify_min_severity", ctx.exception.message)


class NotifyFloorHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(self.store, FakeFetcher({URL: feed(CRITICAL_ITEM)}))
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


class NotifyFloorHttpTests(NotifyFloorHttpBase):
    def test_create_and_read_field(self):
        source = self._create(min_severity="info", notify_min_severity="critical")
        self.assertEqual(source["notify_min_severity"], "critical")

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["notify_min_severity"], "critical"
        )

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["notify_min_severity"], "critical")

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["notify_min_severity"])

    def test_case_insensitive_accepted(self):
        source = self._create(notify_min_severity="Warning")
        self.assertEqual(source["notify_min_severity"], "warning")

    def test_invalid_values_rejected_over_http(self):
        for value in ("bogus", 5, True, ["critical"]):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "notify_min_severity": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("notify_min_severity", json.loads(body)["error"])

    def test_export_includes_field(self):
        self._create(notify_min_severity="critical")
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        source = json.loads(body)["sources"][0]
        self.assertEqual(source["notify_min_severity"], "critical")

    def test_dashboard_renders_notify_indicator(self):
        self._create(notify_min_severity="critical")
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("notify: critical", body)

    def test_dashboard_shows_en_dash_when_inheriting(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("notify:", body)


class SecuredNotifyFloorHttpTests(NotifyFloorHttpBase):
    token = "secret-token"

    def test_create_requires_token(self):
        status, _ = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": URL,
                "kind": "rss",
                "notify_min_severity": "critical",
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
                "notify_min_severity": "critical",
            },
            token="secret-token",
        )
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
