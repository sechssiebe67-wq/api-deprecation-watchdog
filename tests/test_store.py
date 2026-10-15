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

    def _source(self, name="Example", url="https://example.com/feed", **kwargs):
        return Source(
            name=name,
            url=url,
            kind="rss",
            api_name="Example",
            tracked_versions=["v1"],
            **kwargs,
        )

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

    def test_list_entries_offset_skips_newest(self):
        source = self.store.add_source(self._source())
        for title in ("first", "second", "third", "fourth"):
            self.store.save_entry(
                Entry(source_id=source.id, title=title, body="", fingerprint=title)
            )
        all_titles = [e.title for e in self.store.list_entries()]
        self.assertEqual(all_titles, ["fourth", "third", "second", "first"])
        page = self.store.list_entries(limit=2, offset=2)
        self.assertEqual([e.title for e in page], ["second", "first"])
        self.assertEqual(
            [e.title for e in self.store.list_entries(limit=2, offset=0)],
            ["fourth", "third"],
        )
        self.assertEqual(self.store.list_entries(limit=2, offset=100), [])

    def test_list_entries_for_source_offset(self):
        source = self.store.add_source(self._source())
        other = self.store.add_source(self._source(name="Other", url="https://other/feed"))
        for title in ("first", "second", "third"):
            self.store.save_entry(
                Entry(source_id=source.id, title=title, body="", fingerprint=title)
            )
        self.store.save_entry(
            Entry(source_id=other.id, title="noise", body="", fingerprint="noise")
        )
        page = self.store.list_entries_for_source(source.id, limit=1, offset=1)
        self.assertEqual([e.title for e in page], ["second"])
        self.assertEqual(self.store.list_entries_for_source(source.id, offset=100), [])

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

    def test_source_min_severity_defaults_to_info(self):
        created = self.store.add_source(self._source())
        self.assertEqual(created.min_severity, "info")
        self.assertEqual(self.store.get_source(created.id).min_severity, "info")

    def test_source_min_severity_round_trip(self):
        created = self.store.add_source(self._source(min_severity="warning"))
        self.assertEqual(created.min_severity, "warning")
        self.assertEqual(created.to_dict()["min_severity"], "warning")
        fetched = self.store.get_source(created.id)
        self.assertEqual(fetched.min_severity, "warning")
        self.assertEqual(self.store.list_sources()[0].min_severity, "warning")

    def test_source_notify_webhook_url_round_trip(self):
        created = self.store.add_source(
            self._source(notify_webhook_url="https://example.test/critical")
        )
        self.assertEqual(created.notify_webhook_url, "https://example.test/critical")
        self.assertEqual(
            created.to_dict()["notify_webhook_url"], "https://example.test/critical"
        )
        self.assertEqual(
            self.store.get_source(created.id).notify_webhook_url,
            "https://example.test/critical",
        )
        self.assertEqual(
            self.store.list_sources()[0].notify_webhook_url,
            "https://example.test/critical",
        )

    def test_source_notify_webhook_url_defaults_to_none(self):
        created = self.store.add_source(self._source())
        self.assertIsNone(created.notify_webhook_url)
        self.assertIsNone(self.store.get_source(created.id).notify_webhook_url)
        self.assertIsNone(created.to_dict()["notify_webhook_url"])

    def test_source_keywords_default_to_empty(self):
        created = self.store.add_source(self._source())
        self.assertEqual(created.extra_keywords, [])
        self.assertEqual(created.suppress_keywords, [])
        self.assertEqual(created.to_dict()["extra_keywords"], [])
        fetched = self.store.get_source(created.id)
        self.assertEqual(fetched.extra_keywords, [])
        self.assertEqual(fetched.suppress_keywords, [])

    def test_source_keywords_round_trip(self):
        created = self.store.add_source(
            self._source(
                extra_keywords=["retired", "eol"],
                suppress_keywords=["notice"],
            )
        )
        self.assertEqual(created.extra_keywords, ["retired", "eol"])
        self.assertEqual(created.suppress_keywords, ["notice"])
        self.assertEqual(created.to_dict()["extra_keywords"], ["retired", "eol"])
        fetched = self.store.get_source(created.id)
        self.assertEqual(fetched.extra_keywords, ["retired", "eol"])
        self.assertEqual(fetched.suppress_keywords, ["notice"])
        listed = self.store.list_sources()[0]
        self.assertEqual(listed.extra_keywords, ["retired", "eol"])
        self.assertEqual(listed.suppress_keywords, ["notice"])

    def test_migrates_sources_table_without_keyword_columns(self):
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
                    notify_webhook_url TEXT,
                    scan_interval_seconds INTEGER,
                    headers TEXT,
                    auth TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    breaker_until TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, failure_count, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1, NULL,
                        'info', '["all"]', 0, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("extra_keywords", columns)
            self.assertIn("suppress_keywords", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertEqual(legacy.extra_keywords, [])
            self.assertEqual(legacy.suppress_keywords, [])
            fresh = store.add_source(
                self._source(
                    name="New",
                    extra_keywords=["retired"],
                    suppress_keywords=["notice"],
                )
            )
            self.assertEqual(fresh.extra_keywords, ["retired"])
            self.assertEqual(fresh.suppress_keywords, ["notice"])
            store.close()
        finally:
            os.remove(path)

    def test_migrates_sources_table_without_notify_webhook_url(self):
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
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1, NULL,
                        'info', '["all"]', NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("notify_webhook_url", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.notify_webhook_url)
            fresh = store.add_source(
                self._source(name="New", notify_webhook_url="https://new.example/hook")
            )
            self.assertEqual(fresh.notify_webhook_url, "https://new.example/hook")
            store.close()
        finally:
            os.remove(path)

    def test_migrates_sources_table_without_min_severity(self):
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
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1, NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("min_severity", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertEqual(legacy.min_severity, "info")
            fresh = store.add_source(
                Source(name="New", url="https://new.example/feed", kind="rss", min_severity="critical")
            )
            self.assertEqual(fresh.min_severity, "critical")
            store.close()
        finally:
            os.remove(path)


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
