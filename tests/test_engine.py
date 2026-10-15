import unittest

from apiwatch.engine import Engine, seed_default_sources
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Source
from apiwatch.notify import NotificationError, Notifier
from apiwatch.store import Store


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description>
    <link>https://example.com/1</link></item>
  <item><title>Office closed</title><description>Happy holidays.</description></item>
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

    def test_seed_default_sources_is_idempotent(self):
        from apiwatch.sources import default_sources

        seed_default_sources(self.store, default_sources())
        count = len(self.store.list_sources())
        seed_default_sources(self.store, default_sources())
        self.assertEqual(len(self.store.list_sources()), count)


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


if __name__ == "__main__":
    unittest.main()
