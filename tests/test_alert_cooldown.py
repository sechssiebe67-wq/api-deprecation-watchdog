"""Tests for the per-source alert cooldown (``alert_cooldown_seconds``)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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


def feed(title, body="will be removed"):
    return (
        '<?xml version="1.0"?>\n<rss version="2.0"><channel>'
        "<item><title>%s</title><description>%s</description>"
        "<link>https://example.com/%s</link></item>"
        "</channel></rss>" % (title, body, title.replace(" ", "-"))
    )


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
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


class AlertCooldownModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source()
        self.assertIsNone(source.alert_cooldown_seconds)
        self.assertIsNone(source.to_dict()["alert_cooldown_seconds"])

    def test_accepts_zero_and_positive_integer(self):
        for value in (0, 300, 3600):
            source = self._source(alert_cooldown_seconds=value)
            self.assertEqual(source.alert_cooldown_seconds, value)
            self.assertEqual(source.to_dict()["alert_cooldown_seconds"], value)

    def test_rejects_bool_negative_and_non_integer(self):
        for value in (True, False, -1, -300, 1.5, "300", [300]):
            with self.assertRaises(ValueError):
                self._source(alert_cooldown_seconds=value)


class AlertCooldownStoreTests(unittest.TestCase):
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
                alert_cooldown_seconds=300,
            )
        )
        self.assertEqual(created.alert_cooldown_seconds, 300)
        self.assertEqual(
            self.store.get_source(created.id).alert_cooldown_seconds, 300
        )
        self.assertEqual(
            self.store.list_sources()[0].alert_cooldown_seconds, 300
        )
        self.assertEqual(created.to_dict()["alert_cooldown_seconds"], 300)

    def test_default_is_null(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss")
        )
        self.assertIsNone(created.alert_cooldown_seconds)
        self.assertIsNone(self.store.get_source(created.id).alert_cooldown_seconds)

    def test_suppressed_counter_aggregates(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss", alert_cooldown_seconds=60)
        )
        self.store.record_suppressed_alerts({created.id: 2})
        self.store.record_suppressed_alerts({created.id: 1})
        self.assertEqual(self.store.stats()["alerts_suppressed"], 3)

    def test_migrates_old_db_without_cooldown_columns(self):
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
                        NULL, 'info', '["all"]', NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("alert_cooldown_seconds", columns)
            self.assertIn("last_alert_at", columns)
            self.assertIn("alerts_suppressed", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.alert_cooldown_seconds)
            self.assertEqual(legacy.alerts_suppressed, 0)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    alert_cooldown_seconds=120,
                )
            )
            self.assertEqual(fresh.alert_cooldown_seconds, 120)
            store.close()
        finally:
            os.remove(path)


class AlertCooldownEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher({URL: feed("v1 deprecated")})
        self.source = self.store.add_source(
            Source(
                name="Noisy",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                alert_cooldown_seconds=300,
            )
        )

    def tearDown(self):
        self.store.close()

    def _engine(self, notifiers=None, digest_mode="off"):
        return Engine(
            self.store,
            self.fetcher,
            notifiers=notifiers if notifiers is not None else [self.notifier],
            digest_mode=digest_mode,
            clock=self.clock,
        )

    def test_second_alert_inside_window_is_suppressed(self):
        engine = self._engine()
        first = engine.scan_source(self.source)
        self.assertEqual(first.alerts_created, 1)
        self.assertEqual(first.alerts_suppressed, 0)
        self.assertEqual(first.to_dict()["alerts_suppressed"], 0)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_created, 1)
        self.assertEqual(second.alerts_suppressed, 1)
        # No new fan-out, but the alert is still stored and un-notified.
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 1)

    def test_alert_dispatched_after_window_elapses(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        engine.scan_source(self.source)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = feed("v1 sunset")
        self.clock.advance(250)  # 350s since the last real dispatch
        third = engine.scan_source(self.source)
        self.assertEqual(third.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_different_source_is_not_suppressed(self):
        other = self.store.add_source(
            Source(
                name="Other",
                url=URL_B,
                kind="rss",
                tracked_versions=["v1"],
                alert_cooldown_seconds=300,
            )
        )
        self.fetcher.responses[URL_B] = feed("v1 deprecated")
        engine = self._engine()
        engine.scan_source(self.source)
        result = engine.scan_source(other)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_suppressed_alert_is_picked_up_by_next_digest(self):
        engine = self._engine(digest_mode="digest")
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        engine.scan_source(self.source)
        # Both the immediate alert and the suppressed one are still pending.
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 2)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 2)
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 0)

    def test_suppressed_alert_reaches_digest_in_immediate_mode(self):
        engine = self._engine()  # default ``off``: first alert dispatched now
        engine.scan_source(self.source)
        self.assertEqual(len(self.notifier.batches), 1)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        engine.scan_source(self.source)
        pending = self.store.list_alerts(notified=False)
        self.assertEqual(len(pending), 1)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 1)
        self.assertEqual(len(self.notifier.digests), 1)
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 0)

    def test_scan_sources_aggregates_suppressed(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        result = engine.scan_sources(self.store.list_sources())
        self.assertEqual(result.alerts_suppressed, 1)

    def test_zero_and_none_are_byte_for_byte_regression(self):
        for value in (0, None):
            store = Store(":memory:")
            try:
                clock = FakeClock()
                notifier = CapturingNotifier()
                fetcher = FakeFetcher({URL: feed("v1 deprecated")})
                source = store.add_source(
                    Source(
                        name="Plain",
                        url=URL,
                        kind="rss",
                        tracked_versions=["v1"],
                        alert_cooldown_seconds=value,
                    )
                )
                engine = Engine(
                    store, fetcher, notifiers=[notifier], clock=clock
                )
                first = engine.scan_source(source)
                fetcher.responses[URL] = feed("v1 removal")
                clock.advance(1)
                second = engine.scan_source(source)
                self.assertEqual(first.alerts_suppressed, 0)
                self.assertEqual(second.alerts_suppressed, 0)
                self.assertEqual(len(notifier.batches), 2)
                self.assertEqual(len(store.list_alerts(notified=False)), 0)
                self.assertEqual(store.stats()["alerts_suppressed"], 0)
            finally:
                store.close()


class AlertCooldownValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(validate_source_payload(self._payload()).alert_cooldown_seconds)
        self.assertIsNone(
            validate_source_payload(
                self._payload(alert_cooldown_seconds=None)
            ).alert_cooldown_seconds
        )

    def test_non_negative_integer_accepted(self):
        self.assertEqual(
            validate_source_payload(
                self._payload(alert_cooldown_seconds=0)
            ).alert_cooldown_seconds,
            0,
        )
        self.assertEqual(
            validate_source_payload(
                self._payload(alert_cooldown_seconds=300)
            ).alert_cooldown_seconds,
            300,
        )

    def test_invalid_values_rejected_naming_field(self):
        for value in (-1, 1.5, "300", True, False, [1]):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(
                    self._payload(alert_cooldown_seconds=value)
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("alert_cooldown_seconds", ctx.exception.message)


class AlertCooldownHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(self.store, FakeFetcher({URL: feed("v1 deprecated")}))
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


class AlertCooldownHttpTests(AlertCooldownHttpBase):
    def test_create_and_read_cooldown(self):
        source = self._create(alert_cooldown_seconds=300)
        self.assertEqual(source["alert_cooldown_seconds"], 300)
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["alert_cooldown_seconds"], 300
        )
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alert_cooldown_seconds"], 300)

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["alert_cooldown_seconds"])

    def test_zero_accepted_and_returned(self):
        source = self._create(alert_cooldown_seconds=0)
        self.assertEqual(source["alert_cooldown_seconds"], 0)

    def test_invalid_values_rejected_over_http(self):
        for value in (-1, 1.5, "300", True):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "alert_cooldown_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("alert_cooldown_seconds", json.loads(body)["error"])

    def test_stats_reports_suppressed(self):
        source = self._create(alert_cooldown_seconds=300)
        self.store.record_suppressed_alerts({source["id"]: 2})
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alerts_suppressed"], 2)

    def test_dashboard_renders_cooldown_indicator(self):
        self._create(alert_cooldown_seconds=300)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("cooldown 300s", body)

    def test_dashboard_omits_indicator_when_unset(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("cooldown", body)

    def test_dashboard_shows_suppressed_card(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("suppressed", body)


class SecuredAlertCooldownHttpTests(AlertCooldownHttpBase):
    token = "secret-token"

    def test_create_requires_token(self):
        status, _ = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": URL, "kind": "rss"},
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": URL, "kind": "rss"},
            token="secret-token",
        )
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
