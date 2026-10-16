"""Tests for the per-source title dedupe window (``dedupe_window_seconds``)."""
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
from apiwatch.models import Source, normalize_alert_title
from apiwatch.notify import Notifier
from apiwatch.store import Store

URL = "https://example.com/feed"
URL_B = "https://example.com/other"
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


class NormalizeTitleTests(unittest.TestCase):
    def test_trims_collapses_and_casefolds(self):
        self.assertEqual(
            normalize_alert_title("  V1   Deprecated\t\n"),
            "v1 deprecated",
        )

    def test_none_is_empty(self):
        self.assertEqual(normalize_alert_title(None), "")


class DedupeModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="s", url=URL, kind="rss", **kwargs)

    def test_defaults_to_none(self):
        source = self._source()
        self.assertIsNone(source.dedupe_window_seconds)
        self.assertIsNone(source.to_dict()["dedupe_window_seconds"])

    def test_accepts_zero_and_positive_integer(self):
        for value in (0, 300, 3600):
            source = self._source(dedupe_window_seconds=value)
            self.assertEqual(source.dedupe_window_seconds, value)
            self.assertEqual(source.to_dict()["dedupe_window_seconds"], value)

    def test_rejects_bool_negative_and_non_integer(self):
        for value in (True, False, -1, -300, 1.5, "300", [300]):
            with self.assertRaises(ValueError):
                self._source(dedupe_window_seconds=value)


class DedupeStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss", dedupe_window_seconds=300)
        )
        self.assertEqual(created.dedupe_window_seconds, 300)
        self.assertEqual(
            self.store.get_source(created.id).dedupe_window_seconds, 300
        )
        self.assertEqual(created.to_dict()["dedupe_window_seconds"], 300)

    def test_default_is_null(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertIsNone(created.dedupe_window_seconds)
        self.assertIsNone(self.store.get_source(created.id).dedupe_window_seconds)

    def test_title_dispatch_times_default_to_empty(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        self.assertEqual(self.store.title_dispatch_times(created.id), {})

    def test_record_and_read_title_dispatch_normalizes(self):
        created = self.store.add_source(Source(name="s", url=URL, kind="rss"))
        when = "2024-05-01T12:00:00+00:00"
        self.store.record_title_dispatch(created.id, "  V1  Deprecated ", when)
        times = self.store.title_dispatch_times(created.id)
        self.assertEqual(times, {"v1 deprecated": when})
        # A second spelling of the same title updates the same key.
        later = "2024-05-01T12:05:00+00:00"
        self.store.record_title_dispatch(created.id, "v1 deprecated", later)
        self.assertEqual(
            self.store.title_dispatch_times(created.id),
            {"v1 deprecated": later},
        )

    def test_success_and_failure_do_not_clobber_titles(self):
        created = self.store.add_source(
            Source(name="s", url=URL, kind="rss", dedupe_window_seconds=60)
        )
        when = "2024-05-01T12:00:00+00:00"
        self.store.record_title_dispatch(created.id, "v1 deprecated", when)
        self.store.record_source_failure(created.id, 3, None)
        self.store.record_source_success(created.id)
        self.store.record_source_truncation(created.id, True)
        self.assertEqual(
            self.store.title_dispatch_times(created.id),
            {"v1 deprecated": when},
        )

    def test_title_dispatch_survives_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = Store(path)
            created = store.add_source(
                Source(
                    name="s",
                    url=URL,
                    kind="rss",
                    dedupe_window_seconds=300,
                )
            )
            when = "2024-05-01T12:00:00+00:00"
            store.record_title_dispatch(created.id, "v1 deprecated", when)
            store.close()

            reopened = Store(path)
            try:
                self.assertEqual(
                    reopened.get_source(created.id).dedupe_window_seconds, 300
                )
                self.assertEqual(
                    reopened.title_dispatch_times(created.id),
                    {"v1 deprecated": when},
                )
            finally:
                reopened.close()
        finally:
            os.remove(path)

    def test_migrates_old_db_without_dedupe_columns(self):
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
            self.assertIn("dedupe_window_seconds", columns)
            self.assertIn("last_title_alerts", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.dedupe_window_seconds)
            self.assertEqual(store.title_dispatch_times(legacy.id), {})
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    dedupe_window_seconds=120,
                )
            )
            self.assertEqual(fresh.dedupe_window_seconds, 120)
            store.close()
        finally:
            os.remove(f"{path}")


class DedupeEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher({URL: feed([("v1 deprecated", "will be removed")])})
        self.source = self.store.add_source(
            Source(
                name="Noisy",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                dedupe_window_seconds=300,
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

    def test_same_title_inside_window_held_back(self):
        engine = self._engine()
        first = engine.scan_source(self.source)
        self.assertEqual(first.alerts_created, 1)
        self.assertEqual(first.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)

        # Same normalized title, different body: a new content hash, so a fresh
        # entry and alert, but the title was already dispatched.
        self.fetcher.responses[URL] = feed(
            [("V1   Deprecated", "will be removed soon")]
        )
        self.clock.advance(100)
        second = engine.scan_source(self.source)
        self.assertEqual(second.alerts_created, 1)
        self.assertEqual(second.alerts_suppressed, 1)
        self.assertEqual(second.to_dict()["alerts_suppressed"], 1)
        self.assertEqual(len(self.notifier.batches), 1)
        pending = self.store.list_alerts(notified=False)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].notified, False)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 1)

    def test_distinct_title_delivered_inside_another_titles_window(self):
        self.fetcher.responses[URL] = feed(
            [
                ("v1 deprecated", "will be removed"),
                ("v2 sunset", "removed next month"),
            ]
        )
        engine = self._engine()
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_created, 2)
        self.assertEqual(result.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(len(self.notifier.batches[0]), 2)

    def test_duplicate_titles_in_one_scan_single_fanout(self):
        self.fetcher.responses[URL] = feed(
            [
                ("v1 deprecated", "will be removed"),
                ("V1 Deprecated", "will be removed for good"),
            ]
        )
        engine = self._engine()
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_created, 2)
        self.assertEqual(result.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)
        self.assertEqual(len(self.notifier.batches[0]), 1)

    def test_same_title_after_window_delivered_again(self):
        engine = self._engine()
        engine.scan_source(self.source)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = feed(
            [("v1 deprecated", "will be removed soon")]
        )
        self.clock.advance(100)
        engine.scan_source(self.source)
        self.assertEqual(len(self.notifier.batches), 1)

        self.fetcher.responses[URL] = feed(
            [("v1 deprecated", "removal announced again")]
        )
        self.clock.advance(250)  # 350s since the last real dispatch
        third = engine.scan_source(self.source)
        self.assertEqual(third.alerts_suppressed, 0)
        self.assertEqual(len(self.notifier.batches), 2)

    def test_state_persisted_across_engine_restart(self):
        self._engine().scan_source(self.source)
        self.fetcher.responses[URL] = feed(
            [("v1 deprecated", "will be removed soon")]
        )
        self.clock.advance(100)
        # A brand new engine over the same store still sees the window.
        restarted = self._engine()
        result = restarted.scan_source(self.source)
        self.assertEqual(result.alerts_suppressed, 1)
        self.assertEqual(len(self.notifier.batches), 1)

    def test_zero_and_none_are_byte_for_byte_regression(self):
        for value in (0, None):
            store = Store(":memory:")
            try:
                clock = FakeClock()
                notifier = CapturingNotifier()
                fetcher = FakeFetcher({URL: feed([("v1 deprecated", "will be removed")])})
                source = store.add_source(
                    Source(
                        name="Plain",
                        url=URL,
                        kind="rss",
                        tracked_versions=["v1"],
                        dedupe_window_seconds=value,
                    )
                )
                engine = Engine(store, fetcher, notifiers=[notifier], clock=clock)
                first = engine.scan_source(source)
                fetcher.responses[URL] = feed(
                    [("v1 deprecated", "will be removed soon")]
                )
                clock.advance(1)
                second = engine.scan_source(source)
                self.assertEqual(first.alerts_suppressed, 0)
                self.assertEqual(second.alerts_suppressed, 0)
                self.assertEqual(len(notifier.batches), 2)
                self.assertEqual(store.title_dispatch_times(source.id), {})
                self.assertEqual(store.stats()["alerts_suppressed"], 0)
            finally:
                store.close()

    def test_holds_back_exactly_once_per_alert(self):
        self.fetcher.responses[URL] = feed(
            [
                ("v1 deprecated", "will be removed"),
                ("v1 deprecated", "will be removed again"),
                ("v1 deprecated", "will be removed once more"),
            ]
        )
        engine = self._engine()
        result = engine.scan_source(self.source)
        self.assertEqual(result.alerts_created, 3)
        self.assertEqual(result.alerts_suppressed, 2)
        self.assertEqual(self.store.stats()["alerts_suppressed"], 2)
        self.assertEqual(len(self.notifier.batches[0]), 1)


class DedupeValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": URL, "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(
            validate_source_payload(self._payload()).dedupe_window_seconds
        )
        self.assertIsNone(
            validate_source_payload(
                self._payload(dedupe_window_seconds=None)
            ).dedupe_window_seconds
        )

    def test_non_negative_integer_accepted(self):
        self.assertEqual(
            validate_source_payload(
                self._payload(dedupe_window_seconds=0)
            ).dedupe_window_seconds,
            0,
        )
        self.assertEqual(
            validate_source_payload(
                self._payload(dedupe_window_seconds=300)
            ).dedupe_window_seconds,
            300,
        )

    def test_invalid_values_rejected_naming_field(self):
        for value in (-1, 1.5, "300", True, False, [1]):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(
                    self._payload(dedupe_window_seconds=value)
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("dedupe_window_seconds", ctx.exception.message)


class DedupeHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.clock = FakeClock()
        self.notifier = CapturingNotifier()
        self.fetcher = FakeFetcher({URL: feed([("v1 deprecated", "will be removed")])})
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
        source = self.store.add_source(
            Source(
                name="Example",
                url=URL,
                kind="rss",
                tracked_versions=["v1"],
                **extra,
            )
        )
        return source

    def _create(self, **extra):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": URL, "kind": "rss", **extra},
        )
        self.assertEqual(status, 201)
        return json.loads(body)


class DedupeHttpTests(DedupeHttpBase):
    def test_create_and_read_dedupe(self):
        source = self._create(dedupe_window_seconds=300)
        self.assertEqual(source["dedupe_window_seconds"], 300)
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["sources"][0]["dedupe_window_seconds"], 300
        )
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["dedupe_window_seconds"], 300)

    def test_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["dedupe_window_seconds"])

    def test_zero_accepted_and_returned(self):
        source = self._create(dedupe_window_seconds=0)
        self.assertEqual(source["dedupe_window_seconds"], 0)

    def test_invalid_values_rejected_over_http(self):
        for value in (-1, 1.5, "300", True, False):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": URL,
                    "kind": "rss",
                    "dedupe_window_seconds": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("dedupe_window_seconds", json.loads(body)["error"])

    def test_held_back_alert_visible_and_counted(self):
        source = self._register(dedupe_window_seconds=300)
        self.engine.scan_source(source)
        self.fetcher.responses[URL] = feed(
            [("V1 Deprecated", "will be removed soon")]
        )
        self.clock.advance(100)
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

    def test_export_includes_held_back_alerts_and_field(self):
        source = self._register(dedupe_window_seconds=300)
        self.engine.scan_source(source)
        self.fetcher.responses[URL] = feed(
            [("V1 Deprecated", "will be removed soon")]
        )
        self.clock.advance(100)
        self.engine.scan_source(source)

        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stats"]["alerts_suppressed"], 1)
        self.assertEqual(payload["sources"][0]["dedupe_window_seconds"], 300)
        self.assertEqual(len(payload["alerts"]), 2)
        self.assertEqual(sum(1 for a in payload["alerts"] if not a["notified"]), 1)

    def test_stats_aggregates_suppressed_from_record(self):
        source = self._create(dedupe_window_seconds=300)
        self.store.record_suppressed_alerts({source["id"]: 2})
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alerts_suppressed"], 2)

    def test_dashboard_renders_dedupe_indicator(self):
        self._create(dedupe_window_seconds=60)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("dedupe 60s", body)

    def test_dashboard_renders_dash_when_unset(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("dedupe \u2013", body)


class SecuredDedupeHttpTests(DedupeHttpBase):
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
