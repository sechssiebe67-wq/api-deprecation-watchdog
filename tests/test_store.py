import os
import sqlite3
import tempfile
import unittest

from apiwatch.models import Alert, Digest, Entry, Source
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
        self.assertEqual(stats["digests"], 0)
        self.assertEqual(stats["alerts_pending_digest"], 0)


class DigestStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.source = self.store.add_source(
            Source(name="Example", url="https://example.com/feed", kind="rss")
        )

    def tearDown(self):
        self.store.close()

    def _alert(self, severity="warning", fingerprint="fp"):
        entry = self.store.save_entry(
            Entry(source_id=self.source.id, title="t", body="b", fingerprint=fingerprint)
        )
        return self.store.create_alert(
            Alert(source_id=self.source.id, entry_id=entry.id, severity=severity, reason="r")
        )

    def test_digest_round_trip(self):
        digest = self.store.create_digest(
            Digest(
                alert_count=1,
                severity="warning",
                body="hello",
                subject="API Deprecation Watchdog digest: 1 alert",
                omitted=4,
                delivered=False,
            )
        )
        self.assertIsNotNone(digest.id)
        fetched = self.store.get_digest(digest.id)
        self.assertEqual(fetched.body, "hello")
        self.assertEqual(fetched.subject, "API Deprecation Watchdog digest: 1 alert")
        self.assertEqual(fetched.omitted, 4)
        self.assertFalse(fetched.delivered)
        self.assertEqual([d.id for d in self.store.list_digests()], [digest.id])
        self.store.mark_digest_delivered(digest.id)
        self.assertTrue(self.store.get_digest(digest.id).delivered)

    def test_list_undigested_alerts_semantics(self):
        pending = self._alert(fingerprint="fp1")
        notified = self._alert(fingerprint="fp2")
        self.store.mark_notified([notified.id])
        undigested = self.store.list_undigested_alerts()
        self.assertEqual([a.id for a in undigested], [pending.id])

        digest = self.store.create_digest(Digest(alert_count=1, severity="warning", body="x"))
        self.assertIsNotNone(digest.id)
        # Creating a digest does not consume alerts on its own.
        self.assertEqual([a.id for a in self.store.list_undigested_alerts()], [pending.id])
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 1)

        self.store.attach_alerts_to_digest(digest.id, [pending.id])
        self.assertEqual(self.store.list_undigested_alerts(), [])
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 0)

    def test_pending_digest_counter(self):
        self._alert()
        self.assertEqual(self.store.stats()["alerts_pending_digest"], 1)

    def test_migrates_digests_table_without_subject_and_omitted(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    alert_count INTEGER NOT NULL DEFAULT 0,
                    severity TEXT NOT NULL DEFAULT 'info',
                    delivered INTEGER NOT NULL DEFAULT 0,
                    body TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO digests (created_at, alert_count, severity, delivered, body)
                VALUES ('2024-01-01', 1, 'info', 0, 'legacy');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(digests)").fetchall()
            }
            self.assertIn("subject", columns)
            self.assertIn("omitted", columns)
            legacy = store.list_digests()[0]
            self.assertEqual(legacy.body, "legacy")
            self.assertEqual(legacy.subject, "")
            self.assertEqual(legacy.omitted, 0)
            store.close()
        finally:
            os.remove(path)

    def test_migrates_database_without_digest_id(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    entry_id INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    matched_terms TEXT NOT NULL DEFAULT '[]',
                    version_hits TEXT NOT NULL DEFAULT '[]',
                    title TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    notified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (entry_id)
                );
                INSERT INTO alerts
                    (source_id, entry_id, severity, reason, matched_terms,
                     version_hits, title, link, notified, created_at)
                VALUES (1, 1, 'info', '', '[]', '[]', 't', '', 0, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(alerts)").fetchall()
            }
            self.assertIn("digest_id", columns)
            alerts = store.list_alerts()
            self.assertEqual(len(alerts), 1)
            self.assertIsNone(alerts[0].digest_id)
            self.assertEqual(len(store.list_undigested_alerts()), 1)
            store.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
