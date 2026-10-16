"""Tests for the per-source severity escalation window.

``escalation_window_seconds`` is a notification-only filter that holds back an
alert whose severity was already dispatched from the same source within the
window, while a higher-severity alert still escalates through.
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
from apiwatch.models import Source, severity_rank
from apiwatch.notify import Notifier
from apiwatch.store import Store

URL = "https://example.com/feed"
NOW = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def feed(items):
    """Build an RSS document from ``(title, body)`` pairs."""
    parts = ['<?xml version="1.0"?>\n<rss version="2.0"><channel>']
    for title, body in items:
        parts.append(
            "<item><title>%s</title><description>%s</description>"
            "<link>https://example.com/%s</link></item>"
            % (title, body, title.replace(" ", "-"))
        )
    parts.append("</channel></rss>")
    return "".join(parts)


def critical_feed(title, body):
    """A feed whose single entry classifies as ``critical``."""
    return feed([(title, body)])


def warning_feed(title, body):
    """A feed whose single entry classifies as ``warning`` (no critical term)."""
    return feed([(title, body)])


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

    def send(self, alerts, source_names):
        self.batches.append(list(alerts))

    def send_digest(self, digest, source_names):
        pass


class SeverityRankTests(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(severity_rank("critical"), 3)
        self.assertEqual(severity_rank("warning"), 2)
        self.assertEqual(severity_rank("info"), 1)
        self.assertEqual(severity_rank("unknown"), 0)
        self.assertEqual(severity_rank(None), 0)


class EscalationModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none_and_empty_map(self):
        source = self._source()
        self.assertIsNone(source.escalation_window_seconds)
        self.assertEqual(source.last_severity_alerts, {})
        data = source.to_dict()
        self.assertIsNone(data["escalation_window_seconds"])
        self.assertEqual(data["last_severity_alerts"], {})

    def test_accepts_zero_and_positive_integer(self):
        for value in (0, 300, 3600):
            source = self._source(escalation_window_seconds=value)
            self.assertEqual(source.escalation_window_seconds, value)
            self.assertEqual(
                source.to_dict()["escalation_window_seconds"], value
            )

    def test_rejects_bool_negative_and_non_integer(self):
        for value in (True, False, -1, -300, 1.5, "300", [300]):
            with self.assertRaises(ValueError):
                self._source(escalation_window_seconds=value)

    def test_last_severity_alerts_in_to_dict(self):
        source = self._source(
            escalation_window_seconds=60,
            last_severity_alerts={"critical": "2024-05-01T12:00:00+00:00"},
        )
        self.assertEqual(
            source.to_dict()["last_severity_alerts"],
            {"critical": "2024-05-01T12:00:00+00:00"},
        )


class EscalationStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss", escalation_window_seconds=300)
        )
        self.assertEqual(created.escalation_window_seconds, 300)
        self.assertEqual(
            self.store.get_source(created.id).escalation_window_seconds, 300
        )
        self.assertEqual(created.to_dict()["escalation_window_seconds"], 300)

    def test_default_is_null(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertIsNone(created.escalation_window_seconds)
        self.assertIsNone(
            self.store.get_source(created.id).escalation_window_seconds
        )

    def test_severity_dispatch_times_default_to_empty(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertEqual(self.store.severity_dispatch_times(created.id), {})
        self.assertEqual(
            self.store.get_source(created.id).last_severity_alerts, {}
        )

    def test_record_merges_and_last_write_wins(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        first = "2024-05-01T12:00:00+00:00"
        self.store.record_severity_alerts(created.id, {"critical": first})
        self.store.record_severity_alerts(
            created.id, {"warning": "2024-05-01T12:01:00+00:00"}
        )
        later = "2024-05-01T12:05:00+00:00"
        self.store.record_severity_alerts(created.id, {"critical": later})
        self.assertEqual(
            self.store.severity_dispatch_times(created.id),
            {"critical": later, "warning": "2024-05-01T12:01:00+00:00"},
        )
        # The Source row reads the same map back.
        self.assertEqual(
            self.store.get_source(created.id).last_severity_alerts,
            {"critical": later, "warning": "2024-05-01T12:01:00+00:00"},
        )

    def test_record_empty_mapping_is_noop(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.store.record_severity_alerts(created.id, {})
        self.assertEqual(self.store.severity_dispatch_times(created.id), {})

    def test_malformed_json_reads_back_as_empty(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.store._conn.execute(
            "UPDATE sources SET last_severity_alerts = ? WHERE id = ?",
            ("{not json", created.id),
        )
        self.store._conn.commit()
        self.assertEqual(self.store.severity_dispatch_times(created.id), {})
        self.assertEqual(
            self.store.get_source(created.id).last_severity_alerts, {}
        )

    def test_survives_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = Store(path)
            created = store.add_source(
                Source(
                    name="s",
                    url=URL,
                    kind="rss",
                    escalation_window_seconds=3600,
                )
            )
            when = "2024-05-01T12:00:00+00:00"
            store.record_severity_alerts(created.id, {"critical": when})
            store.close()

            reopened = Store(path)
            try:
                self.assertEqual(
                    reopened.get_source(created.id).escalation_window_seconds,
                    3600,
                )
                self.assertEqual(
                    reopened.severity_dispatch_times(created.id),
                    {"critical": when},
                )
                self.assertEqual(
                    reopened.get_source(created.id).last_severity_alerts,
                    {"critical": when},
                )
            finally:
                reopened.close()
        finally:
            os.remove(path)

    def test_migrates_old_db_without_escalation_columns(self):
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
            self.assertIn("escalation_window_seconds", columns)
            self.assertIn("last_severity_alerts", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.escalation_window_seconds)
            self.assertEqual(store.severity_dispatch_times(legacy.id), {})
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    escalation_window_seconds=120,
                )
            )
            self.assertEqual(fresh.escalation_window_seconds, 120)
            store.close()
        finally:
            os.remove(path)


class EscalationEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher(
            {URL: critical_feed("v1 sunset", "removed now")}
        )
        self.source = self.store.add_source(
            Source(
                name="Noisy",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                escalation_window_seconds=3600,
            )
        )

    def tearDown(self):
        self.store.close()

    def _engine(self, notifiers=None):
        return Engine(
            self.store,
            self.fetcher,
            notifiers=notifiers if notifiers is not None else [self.notifier],
            clock=self.clock,
        )

    def test_repeat_critical_suppressed_but_warning_delivered(self):
        engine = self._engine()
        first = engine.scan_source(self.source)
        self.assertEqual(first.alerts_created, 1)
        self.assertEqual(first.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)

        # Same severity (critical), different body: a new content hash and a
        # fresh alert, but held back by the escalation window.
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed for good"
        )
        self.clock.advance(60)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_created, 1)
        self.assertEqual(second.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)
        pending = self.store.list_alerts(notified=False)
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].notified)

        # A lower severity is unaffected: no warning was dispatched in the
        # window, so it is still delivered.
        self.fetcher.responses[URL] = warning_feed(
            "v1 deprecated", "legacy migration"
        )
        self.clock.advance(60)
        third = engine.scan_source(self.source)
        self.assertEqual(third.alerts_created, 1)
        self.assertEqual(third.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_higher_severity_escalates_past_lower_severity(self):
        self.fetcher.responses[URL] = warning_feed(
            "v1 deprecated", "legacy migration"
        )
        engine = self._engine()
        first = engine.scan_source(self.source)
        self.assertEqual(first.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = critical_feed("v1 sunset", "removed")
        self.clock.advance(60)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_same_severity_after_window_delivered_again(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed soon"
        )
        self.clock.advance(60)
        engine.scan_source(self.source)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = critical_feed(
            "v1 sunsets once more", "removal announced again"
        )
        self.clock.advance(3600)  # > 3600s since the last real dispatch
        third = engine.scan_source(self.source)
        self.assertEqual(third.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_state_persisted_across_engine_restart(self):
        self._engine().scan_source(self.source)
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed soon"
        )
        self.clock.advance(60)
        # A brand new engine over the same store still sees the window.
        restarted = self._engine()
        result = restarted.scan_source(self.source)
        self.assertEqual(result.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(
            self.store.severity_dispatch_times(self.source.id),
            {"critical": NOW.isoformat(timespec="seconds")},
        )

    def test_different_severities_in_one_scan_both_delivered(self):
        self.fetcher.responses[URL] = feed(
            [
                ("v1 sunset", "removed now"),
                ("v1 deprecated", "legacy migration"),
            ]
        )
        engine = self._engine()
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_created, 2)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches[0]), 2)

    def test_duplicate_severity_in_one_scan_single_fanout(self):
        self.fetcher.responses[URL] = feed(
            [
                ("v1 sunset", "removed now"),
                ("v1 sunset again", "removed for good"),
            ]
        )
        engine = self._engine()
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_created, 2)
        self.assertEqual(result.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches[0]), 1)

    def test_zero_and_none_are_byte_for_byte_regression(self):
        for value in (0, None):
            store = Store(":memory:")
            try:
                clock = FakeClock()
                notifier = CapturingNotifier()
                fetcher = FakeFetcher(
                    {URL: critical_feed("v1 sunset", "removed now")}
                )
                source = store.add_source(
                    Source(
                        name="Plain",
                        url=URL,
                        kind="rss",
                        tracked_versions=["v1"],
                        escalation_window_seconds=value,
                    )
                )
                engine = Engine(store, fetcher, notifiers=[notifier], clock=clock)
                first = engine.scan_source(source)
                fetcher.responses[URL] = critical_feed(
                    "v1 sunset again", "removed soon"
                )
                clock.advance(1)
                second = engine.scan_source(source)
                self.assertEqual(first.alerts_suppressed, 0)
                self.assertEqual(second.alerts_suppressed, 0)
                self.assertEqual(len(notifier.batches), 2)
                self.assertEqual(store.severity_dispatch_times(source.id), {})
                self.assertEqual(store.stats()["alerts_suppressed"], 0)
            finally:
                store.close()

    def test_suppressed_alert_counted_in_stats(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed soon"
        )
        self.clock.advance(60)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_suppressed, 1)
        self.assertEqual(second.to_dict()["alerts_suppressed"], 1)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 1)


class EscalationValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(
            validate_source_payload(self._payload()).escalation_window_seconds
        )
        self.assertIsNone(
            validate_source_payload(
                self._payload(escalation_window_seconds=None)
            ).escalation_window_seconds
        )

    def test_non_negative_integer_accepted(self):
        for value in (0, 60, 3600):
            self.assertEqual(
                validate_source_payload(
                    self._payload(escalation_window_seconds=value)
                ).escalation_window_seconds,
                value,
            )

    def test_invalid_values_rejected_naming_field(self):
        for value in (-1, 1.5, "60", True, False, [1]):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(
                    self._payload(escalation_window_seconds=value)
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("escalation_window_seconds", ctx.exception.message)


class EscalationHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher(
            {URL: critical_feed("v1 sunset", "removed now")}
        )
        self.engine = Engine(
            self.store,
            self.fetcher,
            notifiers=[self.notifier],
            clock=self.clock,
        )
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

    def _register(self, **extra):
        return self.store.add_source(
            Source(
                name="Example",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                **extra,
            )
        )

    def _create(self, **extra):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": URL, "kind": "rss", **extra},
        )
        self.assertEqual(status, 201)
        return json.loads(body)


class EscalationHttpTests(EscalationHttpBase):
    def test_create_and_read_escalation(self):
        source = self._create(escalation_window_seconds=3600)
        self.assertEqual(source["escalation_window_seconds"], 3600)
        self.assertEqual(source["last_severity_alerts"], {})
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        listed = json.loads(body)["sources"][0]
        self.assertEqual(listed["escalation_window_seconds"], 3600)
        self.assertEqual(listed["last_severity_alerts"], {})
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        one = json.loads(body)
        self.assertEqual(one["escalation_window_seconds"], 3600)
        self.assertEqual(one["last_severity_alerts"], {})

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["escalation_window_seconds"])
        self.assertEqual(source["last_severity_alerts"], {})

    def test_zero_accepted_and_returned(self):
        source = self._create(escalation_window_seconds=0)
        self.assertEqual(source["escalation_window_seconds"], 0)

    def test_invalid_values_rejected_over_http(self):
        for value in (-1, 1.5, "60", True, False):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "escalation_window_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("escalation_window_seconds", json.loads(body)["error"])

    def test_held_back_alert_visible_and_counted(self):
        source = self._register(escalation_window_seconds=3600)
        self.engine.scan_source(source)
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed soon"
        )
        self.clock.advance(60)
        self.engine.scan_source(source)

        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alerts_suppressed"], 1)

        status, body = self.request("GET", "/api/alerts")
        self.assertEqual(status, 200)
        alerts = json.loads(body)["alerts"]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(sum(1 for a in alerts if a["notified"] is False), 1)

        status, body = self.request("GET", "/api/entries")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["entries"]), 2)

    def test_export_includes_field_and_map(self):
        source = self._register(escalation_window_seconds=3600)
        self.engine.scan_source(source)
        self.fetcher.responses[URL] = critical_feed(
            "v1 sunset again", "removed soon"
        )
        self.clock.advance(60)
        self.engine.scan_source(source)

        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stats"]["alerts_suppressed"], 1)
        self.assertEqual(payload["sources"][0]["escalation_window_seconds"], 3600)
        self.assertIn(
            "critical", payload["sources"][0]["last_severity_alerts"]
        )
        self.assertEqual(len(payload["alerts"]), 2)

    def test_dashboard_renders_escalate_indicator(self):
        self._create(escalation_window_seconds=60)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("escalate 60s", body)

    def test_dashboard_renders_dash_when_unset(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("escalate \u2013", body)

    def test_dashboard_never_renders_secrets(self):
        self._create(
            escalation_window_seconds=60,
            headers={"X-Api-Key": "super-secret"},
            auth="user:super-secret",
            notify_webhook_url="https://hooks.example/secret-token",
        )
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("super-secret", body)
        self.assertNotIn("secret-token", body)


class SecuredEscalationHttpTests(EscalationHttpBase):
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
