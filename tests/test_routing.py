"""Tests for per-source notification routing (``notify_channels``)."""
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
from apiwatch.notify import NotificationError, Notifier
from apiwatch.store import Store

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description></item>
</channel></rss>
"""


class FakeNotifier(Notifier):
    """Records the batches and digests it is handed."""

    def __init__(self, name):
        self.name = name
        self.batches = []
        self.digests = []

    def send(self, alerts, source_names):
        self.batches.append(list(alerts))

    def send_digest(self, digest, source_names):
        self.digests.append(digest)


class FailingNotifier(Notifier):
    name = "failing"

    def send(self, alerts, source_names):
        raise NotificationError("boom")

    def send_digest(self, digest, source_names):
        raise NotificationError("boom")


class ModelRoutingTests(unittest.TestCase):
    def test_accepts_known_channels_and_all(self):
        for channels in (["slack"], ["all"], ["console", "email"]):
            source = Source(
                name="s", url="https://example.com/x", kind="rss", notify_channels=channels
            )
            self.assertEqual(source.notify_channels, channels)

    def test_empty_means_all(self):
        source = Source(
            name="s", url="https://example.com/x", kind="rss", notify_channels=[]
        )
        self.assertEqual(source.notify_channels, ["all"])

    def test_default_is_all(self):
        source = Source(name="s", url="https://example.com/x", kind="rss")
        self.assertEqual(source.notify_channels, ["all"])
        self.assertEqual(source.to_dict()["notify_channels"], ["all"])

    def test_rejects_unknown_channel(self):
        with self.assertRaises(ValueError):
            Source(
                name="s",
                url="https://example.com/x",
                kind="rss",
                notify_channels=["pagerduty"],
            )

    def test_to_dict_includes_channels(self):
        source = Source(
            name="s", url="https://example.com/x", kind="rss", notify_channels=["slack"]
        )
        self.assertEqual(source.to_dict()["notify_channels"], ["slack"])


class ConfigRoutingTests(unittest.TestCase):
    def test_default_is_all(self):
        self.assertEqual(Config.from_env({}).notify_channels, ["all"])

    def test_parses_comma_separated_channels(self):
        config = Config.from_env({"NOTIFY_CHANNELS": "slack, email"})
        self.assertEqual(config.notify_channels, ["slack", "email"])

    def test_invalid_falls_back_to_all(self):
        self.assertEqual(
            Config.from_env({"NOTIFY_CHANNELS": "pagerduty"}).notify_channels, ["all"]
        )


class StoreRoutingTests(unittest.TestCase):
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
                notify_channels=["slack"],
            )
        )
        self.assertEqual(created.notify_channels, ["slack"])
        self.assertEqual(self.store.get_source(created.id).notify_channels, ["slack"])
        self.assertEqual(self.store.list_sources()[0].notify_channels, ["slack"])

    def test_default_round_trip(self):
        created = self.store.add_source(
            Source(name="s", url="https://example.com/x", kind="rss")
        )
        self.assertEqual(created.notify_channels, ["all"])

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
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1, NULL,
                        'info', '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("notify_channels", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertEqual(legacy.notify_channels, ["all"])
            store.close()
        finally:
            os.remove(path)


class EngineRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"

    def tearDown(self):
        self.store.close()

    def _source(self, channels):
        return self.store.add_source(
            Source(
                name="Example",
                url=self.url,
                kind="rss",
                tracked_versions=["v1"],
                notify_channels=channels,
            )
        )

    def test_routes_only_to_matching_notifier(self):
        source = self._source(["slack"])
        console = FakeNotifier("console")
        slack = FakeNotifier("slack")
        email = FakeNotifier("email")
        engine = Engine(
            self.store,
            FakeFetcher({self.url: RSS}),
            notifiers=[console, slack, email],
        )
        result = engine.scan_source(source)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(len(slack.batches), 1)
        self.assertEqual(console.batches, [])
        self.assertEqual(email.batches, [])
        # Delivered to Slack, so it is no longer pending.
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_all_routes_to_every_notifier(self):
        source = self._source(["all"])
        notifiers = [FakeNotifier(name) for name in ("console", "slack", "email")]
        engine = Engine(
            self.store, FakeFetcher({self.url: RSS}), notifiers=notifiers
        )
        engine.scan_source(source)
        for notifier in notifiers:
            self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_omitted_field_defaults_to_all(self):
        source = self.store.add_source(
            Source(name="Example", url=self.url, kind="rss", tracked_versions=["v1"])
        )
        notifiers = [FakeNotifier(name) for name in ("console", "slack", "email")]
        engine = Engine(
            self.store, FakeFetcher({self.url: RSS}), notifiers=notifiers
        )
        engine.scan_source(source)
        for notifier in notifiers:
            self.assertEqual(len(notifier.batches), 1)

    def test_routing_to_unconfigured_channel_leaves_alert_pending(self):
        source = self._source(["email"])
        console = FakeNotifier("console")
        engine = Engine(
            self.store, FakeFetcher({self.url: RSS}), notifiers=[console]
        )
        engine.scan_source(source)
        self.assertEqual(console.batches, [])
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)

    def test_notifier_failure_keeps_alert_pending(self):
        source = self._source(["all"])
        engine = Engine(
            self.store, FakeFetcher({self.url: RSS}), notifiers=[FailingNotifier()]
        )
        engine.scan_source(source)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)


class DigestRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url_a = "https://example.com/a"
        self.url_b = "https://example.com/b"

    def tearDown(self):
        self.store.close()

    def _routed_source(self, name, url, channels):
        return self.store.add_source(
            Source(
                name=name,
                url=url,
                kind="rss",
                tracked_versions=["v1"],
                notify_channels=channels,
            )
        )

    def test_digest_only_delivered_to_matching_notifiers(self):
        source_a = self._routed_source("A", self.url_a, ["slack"])
        source_b = self._routed_source("B", self.url_b, ["email"])
        slack = FakeNotifier("slack")
        email = FakeNotifier("email")
        console = FakeNotifier("console")
        fetcher = FakeFetcher({self.url_a: RSS, self.url_b: RSS})
        engine = Engine(
            self.store, fetcher, notifiers=[console, slack, email], digest_mode="digest"
        )
        engine.scan_source(source_a)
        engine.scan_source(source_b)
        engine.flush_digest()
        self.assertEqual(len(slack.digests), 1)
        self.assertEqual(len(email.digests), 1)
        self.assertEqual(console.digests, [])
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_digest_failure_leaves_alert_pending(self):
        source = self._routed_source("A", self.url_a, ["all"])
        engine = Engine(
            self.store,
            FakeFetcher({self.url_a: RSS}),
            notifiers=[FailingNotifier()],
            digest_mode="digest",
        )
        engine.scan_source(source)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertFalse(digest.delivered)
        self.assertFalse(self.store.get_digest(digest.id).delivered)
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)


class AppRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(self.store, FakeFetcher({}))
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

    def test_rejects_unknown_channel(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "notify_channels": ["pagerduty"],
            },
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertIn("notify_channels", error)
        self.assertIn("slack", error)

    def test_stores_and_returns_valid_channels(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": "https://example.com/x",
                "kind": "rss",
                "notify_channels": ["slack"],
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["notify_channels"], ["slack"])

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["notify_channels"], ["slack"])

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources"][0]["notify_channels"], ["slack"])

    def test_defaults_to_all_when_omitted(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": "https://example.com/x", "kind": "rss"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["notify_channels"], ["all"])

    def test_validate_source_payload_rejects_non_list(self):
        with self.assertRaises(ApiError):
            validate_source_payload(
                {
                    "name": "Example",
                    "url": "https://example.com/x",
                    "kind": "rss",
                    "notify_channels": "slack",
                }
            )


if __name__ == "__main__":
    unittest.main()
