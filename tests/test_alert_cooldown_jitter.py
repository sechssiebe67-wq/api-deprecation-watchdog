"""Tests for the per-source alert cooldown jitter.

``alert_cooldown_jitter_seconds`` spreads the effective alert-cooldown window by
a deterministic, source-id-derived offset so many sources that share the same
``alert_cooldown_seconds`` no longer release their coalesced alerts on the same
tick. It mirrors ``scan_jitter_seconds`` (``id % (jitter + 1)``) but applies to
notification dispatch instead of polling.
"""
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


class AlertCooldownJitterModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source()
        self.assertIsNone(source.alert_cooldown_jitter_seconds)
        self.assertIsNone(source.to_dict()["alert_cooldown_jitter_seconds"])

    def test_accepts_non_negative_integer(self):
        for value in (60, 300, 3600):
            source = self._source(alert_cooldown_jitter_seconds=value)
            self.assertEqual(source.alert_cooldown_jitter_seconds, value)
            self.assertEqual(
                source.to_dict()["alert_cooldown_jitter_seconds"], value
            )

    def test_zero_normalizes_to_none(self):
        source = self._source(alert_cooldown_jitter_seconds=0)
        self.assertIsNone(source.alert_cooldown_jitter_seconds)

    def test_rejects_bool_negative_and_non_integer(self):
        for value in (True, False, -1, -60, 1.5, "60", [60]):
            with self.assertRaises(ValueError, msg="value=%r" % (value,)):
                self._source(alert_cooldown_jitter_seconds=value)


class AlertCooldownJitterStoreTests(unittest.TestCase):
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
                alert_cooldown_jitter_seconds=60,
            )
        )
        self.assertEqual(created.alert_cooldown_jitter_seconds, 60)
        self.assertEqual(
            self.store.get_source(created.id).alert_cooldown_jitter_seconds, 60
        )
        self.assertEqual(
            self.store.list_sources()[0].alert_cooldown_jitter_seconds, 60
        )
        self.assertEqual(created.to_dict()["alert_cooldown_jitter_seconds"], 60)

    def test_default_is_null(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertIsNone(created.alert_cooldown_jitter_seconds)
        self.assertIsNone(
            self.store.get_source(created.id).alert_cooldown_jitter_seconds
        )

    def test_zero_is_stored_as_no_jitter(self):
        created = self.store.add_source(
            Source(
                name="s",
                url=URL,
                kind="rss",
                alert_cooldown_jitter_seconds=0,
            )
        )
        self.assertIsNone(created.alert_cooldown_jitter_seconds)

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
                    scan_interval_seconds INTEGER,
                    alert_cooldown_seconds INTEGER,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, scan_interval_seconds,
                     alert_cooldown_seconds, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1,
                        NULL, 'info', '["all"]', NULL, 300, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("alert_cooldown_jitter_seconds", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertEqual(legacy.alert_cooldown_seconds, 300)
            self.assertIsNone(legacy.alert_cooldown_jitter_seconds)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    alert_cooldown_seconds=300,
                    alert_cooldown_jitter_seconds=60,
                )
            )
            self.assertEqual(fresh.alert_cooldown_jitter_seconds, 60)
            store.close()
        finally:
            os.remove(path)


class EffectiveCooldownTests(unittest.TestCase):
    def test_formula_is_deterministic(self):
        source = Source(
            id=7,
            name="s",
            url=URL,
            kind="rss",
            alert_cooldown_seconds=300,
            alert_cooldown_jitter_seconds=60,
        )
        self.assertEqual(Engine._effective_cooldown(source), 300 + (7 % 61))
        # The same (id, jitter) pair always yields the same offset.
        self.assertEqual(
            Engine._effective_cooldown(source), Engine._effective_cooldown(source)
        )

    def test_no_jitter_is_exactly_the_base_cooldown(self):
        for jitter in (None, 0):
            source = Source(
                id=7,
                name="s",
                url=URL,
                kind="rss",
                alert_cooldown_seconds=300,
                alert_cooldown_jitter_seconds=jitter,
            )
            self.assertEqual(Engine._effective_cooldown(source), 300)

    def test_no_cooldown_is_unaffected_by_jitter(self):
        for cooldown in (None, 0):
            source = Source(
                id=7,
                name="s",
                url=URL,
                kind="rss",
                alert_cooldown_seconds=cooldown,
                alert_cooldown_jitter_seconds=60,
            )
            self.assertEqual(Engine._effective_cooldown(source), 0)

    def test_none_source_is_zero(self):
        self.assertEqual(Engine._effective_cooldown(None), 0)


class AlertCooldownJitterEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher({URL: feed("v1 deprecated")})
        # First inserted source gets id 1, so the offset is ``1 % (60 + 1) = 1``.
        self.source = self.store.add_source(
            Source(
                name="Noisy",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                alert_cooldown_seconds=300,
                alert_cooldown_jitter_seconds=60,
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

    def test_offset_widens_the_window(self):
        self.assertEqual(self.source.id % 61, 1)
        self.assertEqual(Engine._effective_cooldown(self.source), 301)
        engine = self._engine()
        first = engine.scan_source(self.source)
        self.assertEqual(first.alerts_suppressed, 0)

        # 300s is below the jittered window of 301s, so the repeat is held back.
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(300)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)

        # Once the jittered window elapses the alert is delivered.
        self.fetcher.responses[URL] = feed("v1 sunset")
        self.clock.advance(1)  # 301s since the last real dispatch
        third = engine.scan_source(self.source)
        self.assertEqual(third.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_suppressed_alert_is_stored_and_visible(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_suppressed, 1)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 1)
        # The entry and alert are still stored, with ``notified = 0``.
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)
        self.assertEqual(len(self.store.list_entries()), 2)

    def test_no_cooldown_is_unaffected_by_jitter(self):
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
                    alert_cooldown_seconds=None,
                    alert_cooldown_jitter_seconds=60,
                )
            )
            engine = Engine(store, fetcher, notifiers=[notifier], clock=clock)
            fetcher.responses[URL] = feed("v1 deprecated")
            first = engine.scan_source(source)
            fetcher.responses[URL] = feed("v1 removal")
            clock.advance(1)
            second = engine.scan_source(source)
            self.assertEqual(first.alerts_suppressed, 0)
            self.assertEqual(second.alerts_suppressed, 0)
            self.assertEqual(len(notifier.batches), 2)
            self.assertEqual(store.stats()["alerts_suppressed"], 0)
        finally:
            store.close()

    def test_zero_and_none_jitter_are_byte_for_byte_unchanged(self):
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
                        alert_cooldown_seconds=300,
                        alert_cooldown_jitter_seconds=value,
                    )
                )
                engine = Engine(store, fetcher, notifiers=[notifier], clock=clock)
                first = engine.scan_source(source)
                fetcher.responses[URL] = feed("v1 removal")
                clock.advance(300)  # exactly the un-jittered window
                second = engine.scan_source(source)
                self.assertEqual(first.alerts_suppressed, 0)
                self.assertEqual(second.alerts_suppressed, 0)
                self.assertEqual(len(notifier.batches), 2)
            finally:
                store.close()

    def test_suppressed_alert_is_picked_up_by_next_digest(self):
        engine = self._engine(digest_mode="digest")
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = feed("v1 removal")
        self.clock.advance(100)
        engine.scan_source(self.source)
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 2)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 2)


class AlertCooldownJitterValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(
            validate_source_payload(
                self._payload()
            ).alert_cooldown_jitter_seconds
        )
        self.assertIsNone(
            validate_source_payload(
                self._payload(alert_cooldown_jitter_seconds=None)
            ).alert_cooldown_jitter_seconds
        )

    def test_non_negative_integer_accepted(self):
        self.assertEqual(
            validate_source_payload(
                self._payload(alert_cooldown_jitter_seconds=60)
            ).alert_cooldown_jitter_seconds,
            60,
        )

    def test_invalid_values_rejected_naming_field(self):
        for value in (-1, 1.5, "60", True, False, [60]):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(
                    self._payload(alert_cooldown_jitter_seconds=value)
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn(
                "alert_cooldown_jitter_seconds", ctx.exception.message
            )


class AlertCooldownJitterHttpBase(unittest.TestCase):
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


class AlertCooldownJitterHttpTests(AlertCooldownJitterHttpBase):
    def test_create_and_read_jitter(self):
        source = self._create(
            alert_cooldown_seconds=300,
            alert_cooldown_jitter_seconds=60,
        )
        self.assertEqual(source["alert_cooldown_jitter_seconds"], 60)
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["alert_cooldown_jitter_seconds"], 60
        )
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["alert_cooldown_jitter_seconds"], 60
        )

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["alert_cooldown_jitter_seconds"])

    def test_invalid_values_rejected_over_http(self):
        for value in (-1, 1.5, "60", True):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "alert_cooldown_jitter_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn(
                "alert_cooldown_jitter_seconds", json.loads(body)["error"]
            )

    def test_suppressed_alert_visible_via_api(self):
        source = self._create(
            alert_cooldown_seconds=300,
            alert_cooldown_jitter_seconds=60,
        )
        self.store.record_suppressed_alerts({source["id"]: 1})
        status, body = self.request("GET", "/api/alerts")
        self.assertEqual(status, 200)
        self.assertIn("alerts", json.loads(body))
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("alerts", json.loads(body))

    def test_dashboard_renders_jitter_indicator(self):
        self._create(alert_cooldown_seconds=300, alert_cooldown_jitter_seconds=60)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("cooldown 300s +0-60s", body)

    def test_dashboard_unchanged_when_jitter_unset(self):
        self._create(alert_cooldown_seconds=300)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("cooldown 300s", body)
        self.assertNotIn("+0-", body)


if __name__ == "__main__":
    unittest.main()
