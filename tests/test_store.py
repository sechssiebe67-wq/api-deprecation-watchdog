import unittest

from apiwatch.models import Alert, Entry, Source
from apiwatch.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _source(self, name="Example", url="https://example.com/feed"):
        return Source(name=name, url=url, kind="rss", api_name="Example", tracked_versions=["v1"])

    def test_add_and_list_sources(self):
        created = self.store.add_source(self._source())
        self.assertIsNotNone(created.id)
        self.assertEqual(created.tracked_versions, ["v1"])
        self.assertEqual(len(self.store.list_sources()), 1)

    def test_delete_source_removes_related(self):
        source = self.store.add_source(self._source())
        entry = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        self.store.create_alert(
            Alert(source_id=source.id, entry_id=entry.id, severity="warning", reason="r")
        )
        self.assertTrue(self.store.delete_source(source.id))
        self.assertEqual(self.store.list_sources(), [])
        self.assertEqual(self.store.list_entries(), [])
        self.assertEqual(self.store.list_alerts(), [])

    def test_entry_deduplication(self):
        source = self.store.add_source(self._source())
        first = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        second = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.store.list_entries()), 1)

    def test_alert_deduplication_per_entry(self):
        source = self.store.add_source(self._source())
        entry = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        first = self.store.create_alert(
            Alert(source_id=source.id, entry_id=entry.id, severity="warning", reason="r")
        )
        second = self.store.create_alert(
            Alert(source_id=source.id, entry_id=entry.id, severity="warning", reason="r")
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_mark_notified_and_filter(self):
        source = self.store.add_source(self._source())
        entry = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        alert = self.store.create_alert(
            Alert(source_id=source.id, entry_id=entry.id, severity="critical", reason="r")
        )
        self.assertEqual(len(self.store.list_alerts(notified=False)), 1)
        self.store.mark_notified([alert.id])
        self.assertEqual(len(self.store.list_alerts(notified=False)), 0)
        self.assertEqual(len(self.store.list_alerts(severity="critical")), 1)

    def test_stats_and_check_runs(self):
        source = self.store.add_source(self._source())
        run_id = self.store.start_check_run()
        self.store.finish_check_run(run_id, sources_checked=1, alerts_created=2, errors=0)
        stats = self.store.stats()
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["sources_enabled"], 1)
        self.assertEqual(stats["last_check"]["sources_checked"], 1)
        self.assertEqual(stats["last_check"]["alerts_created"], 2)


if __name__ == "__main__":
    unittest.main()
