import json
import unittest

from apiwatch.engine import Engine, seed_default_sources
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Source
from apiwatch.notify import NotificationError, Notifier, WebhookNotifier
from apiwatch.store import Store


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description>
    <link>https://example.com/1</link></item>
  <item><title>Office closed</title><description>Happy holidays.</description></item>
</channel></rss>
"""

FLOOR_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>New release notice</title><description>A minor announcement.</description>
    <link>https://example.com/info</link></item>
  <item><title>v1 breaking change</title><description>v1 will be removed.</description>
    <link>https://example.com/critical</link></item>
</channel></rss>
"""


class CapturingNotifier(Notifier):
    name = "capture"

    def __init__(self):
        self.batches = []

    def send(self, alerts, source_names):
        self.batches.append((list(alerts), dict(source_names)))


class DigestCapturingNotifier(Notifier):
    name = "digest-capture"

    def __init__(self):
        self.immediate = []
        self.digests = []

    def send(self, alerts, source_names):
        self.immediate.append(list(alerts))

    def send_digest(self, digest, source_names):
        self.digests.append(digest)


class FailingDigestNotifier(Notifier):
    name = "failing-digest"

    def send(self, alerts, source_names):
        pass

    def send_digest(self, digest, source_names):
        raise NotificationError("boom")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"
        self.source = self.store.add_source(
            Source(
                name="Example",
                url=self.url,
                kind="rss",
                api_name="Example",
                tracked_versions=["v1"],
            )
        )

    def tearDown(self):
        self.store.close()

    def test_scan_creates_alerts_only_for_relevant(self):
        fetcher = FakeFetcher({self.url: RSS})
        engine = Engine(self.store, fetcher)
        result = engine.scan_source(self.source)
        self.assertEqual(result.entries_seen, 2)
        self.assertEqual(result.entries_new, 2)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(result.alerts[0].title, "v1 deprecated")

    def test_rescan_is_idempotent(self):
        fetcher = FakeFetcher({self.url: RSS})
        engine = Engine(self.store, fetcher)
        engine.scan_source(self.source)
        second = engine.scan_source(self.source)
        self.assertEqual(second.entries_new, 0)
        self.assertEqual(second.alerts_created, 0)
        self.assertEqual(len(self.store.list_alerts()), 1)

    def test_notifier_receives_batch_and_marks_notified(self):
        fetcher = FakeFetcher({self.url: RSS})
        notifier = CapturingNotifier()
        engine = Engine(self.store, fetcher, notifiers=[notifier])
        engine.scan_source(self.source)
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(notifier.batches[0][1][self.source.id], "Example")
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_scan_all_aggregates_and_reports_errors(self):
        good = self.source
        bad = self.store.add_source(
            Source(name="Broken", url="https://broken.example/feed", kind="rss")
        )
        fetcher = FakeFetcher({good.url: RSS})
        engine = Engine(self.store, fetcher)
        result = engine.scan_all()
        self.assertEqual(result.sources_checked, 1)
        self.assertEqual(result.sources_failed, 1)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIsNotNone(bad.id)

    def test_min_severity_floor_suppresses_lower_alerts(self):
        url = "https://example.com/floor"
        source = self.store.add_source(
            Source(name="Floored", url=url, kind="rss", min_severity="warning")
        )
        engine = Engine(self.store, FakeFetcher({url: FLOOR_RSS}))
        result = engine.scan_source(source)
        # Both entries are stored, but only the warning-severity-relevant
        # critical one clears the source's floor.
        self.assertEqual(result.entries_new, 2)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual([a.severity for a in result.alerts], ["critical"])
        self.assertEqual(len(self.store.list_entries(source_id=source.id)), 2)
        self.assertEqual(len(self.store.list_alerts()), 1)

        # Re-scanning is still idempotent: the suppressed entry is not lost and
        # does not turn into a duplicate alert later.
        again = engine.scan_source(source)
        self.assertEqual(again.entries_new, 0)
        self.assertEqual(again.alerts_created, 0)
        self.assertEqual(len(self.store.list_entries(source_id=source.id)), 2)
        self.assertEqual(len(self.store.list_alerts()), 1)

    def test_info_floor_still_alerts_on_info(self):
        url = "https://example.com/info-floor"
        source = self.store.add_source(
            Source(name="Info floor", url=url, kind="rss", min_severity="info")
        )
        engine = Engine(self.store, FakeFetcher({url: FLOOR_RSS}))
        result = engine.scan_source(source)
        self.assertEqual(result.alerts_created, 2)

    def test_seed_default_sources_is_idempotent(self):
        from apiwatch.sources import default_sources

        seed_default_sources(self.store, default_sources())
        count = len(self.store.list_sources())
        seed_default_sources(self.store, default_sources())
        self.assertEqual(len(self.store.list_sources()), count)


OPENAPI_URL = "https://api.acme.com/openapi.json"
_OPENAPI_V1 = {
    "openapi": "3.0.0",
    "info": {"title": "Acme", "version": "1.0.0", "contact": {"url": "/support"}},
    "paths": {
        "/users": {
            "get": {
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}, "404": {"description": "nope"}},
            }
        },
        "/legacy": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
}
_OPENAPI_V2 = {
    "openapi": "3.0.0",
    "info": {"title": "Acme", "version": "2.0.0", "contact": {"url": "/support"}},
    "paths": {
        "/users": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "org", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/teams": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
}
OPENAPI_DOC = json.dumps({"versions": [_OPENAPI_V1, _OPENAPI_V2]})


class OpenApiEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _engine(self, url):
        return Engine(self.store, FakeFetcher({url: OPENAPI_DOC}))

    def test_scan_reports_expected_severities(self):
        source = self.store.add_source(
            Source(name="Acme spec", url=OPENAPI_URL, kind="openapi", selector="versions")
        )
        result = self._engine(OPENAPI_URL).scan_source(source)
        sev = {alert.title: alert.severity for alert in result.alerts}
        self.assertEqual(result.alerts_created, 4)
        self.assertEqual(sev["/legacy: removed path"], "critical")
        self.assertEqual(sev["/teams: added path"], "info")
        self.assertEqual(sev["GET /users: added required parameter"], "warning")
        self.assertEqual(sev["GET /users: changed response status code set"], "critical")

    def test_rescan_is_idempotent(self):
        source = self.store.add_source(
            Source(name="Acme spec", url=OPENAPI_URL, kind="openapi", selector="versions")
        )
        engine = self._engine(OPENAPI_URL)
        engine.scan_source(source)
        second = engine.scan_source(source)
        self.assertEqual(second.entries_new, 0)
        self.assertEqual(second.alerts_created, 0)
        self.assertEqual(len(self.store.list_alerts()), 4)

    def test_severity_floor_suppresses_info_changes(self):
        source = self.store.add_source(
            Source(
                name="Acme spec",
                url=OPENAPI_URL,
                kind="openapi",
                selector="versions",
                min_severity="warning",
            )
        )
        result = self._engine(OPENAPI_URL).scan_source(source)
        self.assertEqual(result.entries_new, 4)
        self.assertEqual(result.alerts_created, 3)
        self.assertNotIn("info", [alert.severity for alert in result.alerts])
        self.assertEqual(len(self.store.list_entries(source_id=source.id)), 4)

    def test_malformed_openapi_records_error_without_aborting(self):
        good_url = "https://example.com/feed"
        self.store.add_source(Source(name="Good", url=good_url, kind="rss"))
        self.store.add_source(Source(name="Bad spec", url=OPENAPI_URL, kind="openapi"))
        fetcher = FakeFetcher({good_url: RSS, OPENAPI_URL: "definitely not json"})
        result = Engine(self.store, fetcher).scan_all()
        self.assertEqual(result.sources_checked, 2)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("malformed OpenAPI", result.errors[0])


class DigestEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"
        self.source = self.store.add_source(
            Source(
                name="Example",
                url=self.url,
                kind="rss",
                api_name="Example",
                tracked_versions=["v1"],
            )
        )

    def tearDown(self):
        self.store.close()

    def _engine(self, notifier, digest_mode):
        return Engine(
            self.store,
            FakeFetcher({self.url: RSS}),
            notifiers=[notifier],
            digest_mode=digest_mode,
        )

    def test_digest_mode_suppresses_immediate_dispatch(self):
        notifier = DigestCapturingNotifier()
        engine = self._engine(notifier, "digest")
        engine.scan_source(self.source)
        self.assertEqual(notifier.immediate, [])
        self.assertEqual(notifier.digests, [])
        self.assertEqual(len(self.store.list_undigested_alerts()), 1)
        self.assertEqual(self.store.stats()["digests"], 0)

    def test_flush_digest_delivers_once_and_marks_notified(self):
        notifier = DigestCapturingNotifier()
        engine = self._engine(notifier, "digest")
        engine.scan_source(self.source)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(digest.alert_count, 1)
        self.assertEqual(len(notifier.digests), 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)
        stored = self.store.get_digest(digest.id)
        self.assertTrue(stored.delivered)
        self.assertEqual(len(self.store.list_undigested_alerts()), 0)

    def test_second_flush_is_noop(self):
        notifier = DigestCapturingNotifier()
        engine = self._engine(notifier, "digest")
        engine.scan_source(self.source)
        engine.flush_digest()
        self.assertIsNone(engine.flush_digest())
        self.assertEqual(len(notifier.digests), 1)

    def test_both_mode_dispatches_immediately_and_stays_eligible(self):
        notifier = DigestCapturingNotifier()
        engine = self._engine(notifier, "both")
        engine.scan_source(self.source)
        self.assertEqual(len(notifier.immediate), 1)
        self.assertEqual(len(self.store.list_undigested_alerts()), 1)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(len(notifier.digests), 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_notifier_failure_does_not_raise_or_mark_delivered(self):
        engine = self._engine(FailingDigestNotifier(), "digest")
        engine.scan_source(self.source)
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertFalse(digest.delivered)
        self.assertFalse(self.store.get_digest(digest.id).delivered)
        # A failed flush must not consume the alerts: they stay pending so the
        # next flush can retry them.
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 1)
        self.assertEqual(len(self.store.list_undigested_alerts()), 1)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)

    def test_failed_flush_is_retried_on_next_flush(self):
        failing = FailingDigestNotifier()
        engine = self._engine(failing, "digest")
        engine.scan_source(self.source)
        self.assertFalse(engine.flush_digest().delivered)

        capture = DigestCapturingNotifier()
        engine.notifiers = [capture]
        retry = engine.flush_digest()
        self.assertIsNotNone(retry)
        self.assertTrue(retry.delivered)
        self.assertEqual(len(capture.digests), 1)
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 0)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_digest_subject_and_omitted_round_trip(self):
        notifier = DigestCapturingNotifier()
        engine = self._engine(notifier, "digest")
        engine.scan_source(self.source)
        digest = engine.flush_digest()
        self.assertEqual(digest.subject, "API Deprecation Watchdog digest: 1 alert")
        self.assertEqual(digest.omitted, 0)
        self.assertEqual(notifier.digests[0].subject, digest.subject)
        self.assertEqual(self.store.get_digest(digest.id).subject, digest.subject)


class RecordingWebhook(WebhookNotifier):
    """A ``WebhookNotifier`` that records the resolved URL instead of sending."""

    def __init__(self, global_url="https://global.test/hook"):
        super().__init__(global_url)
        self.calls = []

    def _post(self, url, payload):
        self.calls.append((url, payload))


class FailingWebhook(WebhookNotifier):
    def __init__(self):
        super().__init__("https://global.test/hook")

    def _post(self, url, payload):
        raise NotificationError("webhook returned 500")


class NamedNotifier(Notifier):
    def __init__(self, name):
        self.name = name
        self.batches = []

    def send(self, alerts, source_names):
        self.batches.append(list(alerts))


class WebhookEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url_a = "https://example.com/a"
        self.url_b = "https://example.com/b"

    def tearDown(self):
        self.store.close()

    def _source(self, name, url, webhook_url=None):
        return self.store.add_source(
            Source(
                name=name,
                url=url,
                kind="rss",
                tracked_versions=["v1"],
                notify_channels=["webhook"],
                notify_webhook_url=webhook_url,
            )
        )

    def test_webhook_channel_routes_only_to_webhook(self):
        source = self._source("A", self.url_a)
        webhook = RecordingWebhook()
        console = NamedNotifier("console")
        slack = NamedNotifier("slack")
        engine = Engine(
            self.store,
            FakeFetcher({self.url_a: RSS}),
            notifiers=[console, slack, webhook],
        )
        result = engine.scan_source(source)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(console.batches, [])
        self.assertEqual(slack.batches, [])
        self.assertEqual(len(webhook.calls), 1)
        self.assertEqual(webhook.calls[0][0], "https://global.test/hook")
        # Delivered by the webhook, so it is no longer pending.
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)

    def test_per_source_url_overrides_global(self):
        critical = self._source("Critical", self.url_a, webhook_url="https://example.test/critical")
        normal = self._source("Normal", self.url_b)
        webhook = RecordingWebhook()
        engine = Engine(
            self.store,
            FakeFetcher({self.url_a: RSS, self.url_b: RSS}),
            notifiers=[webhook],
        )
        engine.scan_source(critical)
        engine.scan_source(normal)
        urls = [url for url, _ in webhook.calls]
        self.assertIn("https://example.test/critical", urls)
        self.assertIn("https://global.test/hook", urls)

    def test_failed_webhook_leaves_alert_pending(self):
        source = self._source("A", self.url_a)
        engine = Engine(
            self.store, FakeFetcher({self.url_a: RSS}), notifiers=[FailingWebhook()]
        )
        engine.scan_source(source)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)

    def test_digest_uses_per_source_url(self):
        source = self._source(
            "Critical", self.url_a, webhook_url="https://example.test/critical"
        )
        webhook = RecordingWebhook()
        engine = Engine(
            self.store,
            FakeFetcher({self.url_a: RSS}),
            notifiers=[webhook],
            digest_mode="digest",
        )
        engine.scan_source(source)
        self.assertEqual(webhook.calls, [])
        digest = engine.flush_digest()
        self.assertIsNotNone(digest)
        self.assertEqual(len(webhook.calls), 1)
        url, payload = webhook.calls[0]
        self.assertEqual(url, "https://example.test/critical")
        self.assertIn("digest", payload)
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)


if __name__ == "__main__":
    unittest.main()
