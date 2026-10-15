import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import ApiError, create_server, validate_source_payload
from apiwatch.config import Config
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Alert, Digest, Entry, Source
from apiwatch.store import Store

OLD = "2024-01-01T00:00:00+00:00"
NOW = datetime(2024, 1, 10, tzinfo=timezone.utc)


class PruneStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _source(self, name="Example", **kwargs):
        return self.store.add_source(
            Source(name=name, url="https://example.com/%s" % name, kind="rss", **kwargs)
        )

    def _entry(self, source_id, fingerprint, created_at=None):
        entry = self.store.save_entry(
            Entry(source_id=source_id, title=fingerprint, body="b", fingerprint=fingerprint)
        )
        if created_at is not None:
            self.store._conn.execute(
                "UPDATE entries SET created_at = ? WHERE id = ?",
                (created_at, entry.id),
            )
            self.store._conn.commit()
        return entry

    def test_global_default_prunes_old_entries(self):
        source = self._source()
        old = self._entry(source.id, "old", created_at=OLD)
        fresh = self._entry(source.id, "fresh")
        counts = self.store.prune_entries(1, {}, NOW)
        self.assertEqual(counts, {"entries_pruned": 1, "alerts_pruned": 0})
        self.assertEqual([e.id for e in self.store.list_entries()], [fresh.id])

    def test_source_override_precedence_over_global(self):
        plain = self._source("plain")
        tuned = self._source("tuned", retention_days=1)
        plain_old = self._entry(plain.id, "plain-old", created_at=OLD)
        tuned_old = self._entry(tuned.id, "tuned-old", created_at=OLD)
        # The global default is disabled, so only the overriding source is pruned.
        counts = self.store.prune_entries(0, {}, NOW)
        self.assertEqual(counts["entries_pruned"], 1)
        remaining = [e.id for e in self.store.list_entries()]
        self.assertIn(plain_old.id, remaining)
        self.assertNotIn(tuned_old.id, remaining)

    def test_explicit_override_wins_over_global(self):
        source = self._source("s")
        self._entry(source.id, "old", created_at=OLD)
        counts = self.store.prune_entries(365, {source.id: 1}, NOW)
        self.assertEqual(counts["entries_pruned"], 1)

    def test_alerts_removed_with_their_entries_sources_and_digests_kept(self):
        source = self._source("s", retention_days=1)
        old = self._entry(source.id, "old", created_at=OLD)
        kept = self._entry(source.id, "kept")
        self.store.create_alert(
            Alert(source_id=source.id, entry_id=old.id, severity="warning", reason="r")
        )
        self.store.create_alert(
            Alert(source_id=source.id, entry_id=kept.id, severity="warning", reason="r")
        )
        self.store.create_digest(
            Digest(alert_count=1, severity="warning", subject="digest")
        )
        counts = self.store.prune_entries(0, {}, NOW)
        self.assertEqual(counts, {"entries_pruned": 1, "alerts_pruned": 1})
        remaining_alerts = self.store.list_alerts()
        self.assertEqual(len(remaining_alerts), 1)
        self.assertEqual(remaining_alerts[0].entry_id, kept.id)
        # Sources and digests are never pruned.
        self.assertEqual(len(self.store.list_sources()), 1)
        self.assertEqual(len(self.store.list_digests()), 1)

    def test_disabled_prune_deletes_nothing(self):
        source = self._source("s")
        self._entry(source.id, "old", created_at=OLD)
        counts = self.store.prune_entries(0, {}, datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(counts, {"entries_pruned": 0, "alerts_pruned": 0})
        self.assertEqual(len(self.store.list_entries()), 1)

    def test_retention_days_round_trip(self):
        created = self._source("s", retention_days=7)
        self.assertEqual(created.retention_days, 7)
        self.assertEqual(created.to_dict()["retention_days"], 7)
        self.assertEqual(self.store.get_source(created.id).retention_days, 7)
        self.assertEqual(self.store.list_sources()[0].retention_days, 7)

    def test_retention_days_defaults_to_none(self):
        created = self._source("s")
        self.assertIsNone(created.retention_days)
        self.assertIsNone(created.to_dict()["retention_days"])
        self.assertIsNone(self.store.get_source(created.id).retention_days)


class RetentionMigrationTests(unittest.TestCase):
    def test_migrates_sources_table_without_retention_days(self):
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
            self.assertIn("retention_days", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.retention_days)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    retention_days=7,
                )
            )
            self.assertEqual(fresh.retention_days, 7)
            store.close()
        finally:
            os.remove(path)


class EnginePruneTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _engine(self, retention_days=0):
        return Engine(
            self.store,
            FakeFetcher({}),
            retention_days=retention_days,
            clock=lambda: NOW,
        )

    def test_engine_prune_uses_source_override(self):
        source = self.store.add_source(
            Source(name="s", url="https://e.example/feed", kind="rss", retention_days=1)
        )
        entry = self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        self.store._conn.execute(
            "UPDATE entries SET created_at = ? WHERE id = ?", (OLD, entry.id)
        )
        self.store._conn.commit()
        counts = self._engine(retention_days=0).prune()
        self.assertEqual(counts, {"entries_pruned": 1, "alerts_pruned": 0})

    def test_engine_prune_disabled_returns_zero(self):
        source = self.store.add_source(
            Source(name="s", url="https://e.example/feed", kind="rss")
        )
        self.store.save_entry(
            Entry(source_id=source.id, title="t", body="b", fingerprint="fp")
        )
        self.assertEqual(
            self._engine(retention_days=0).prune(),
            {"entries_pruned": 0, "alerts_pruned": 0},
        )


class RetentionValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": "https://example.com/feed", "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(validate_source_payload(self._payload()).retention_days)
        self.assertIsNone(
            validate_source_payload(self._payload(retention_days=None)).retention_days
        )

    def test_positive_integer_accepted(self):
        source = validate_source_payload(self._payload(retention_days=30))
        self.assertEqual(source.retention_days, 30)

    def test_invalid_values_rejected_naming_field(self):
        for value in (0, -1, 1.5, "7", True, False):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(self._payload(retention_days=value))
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("retention_days", ctx.exception.message)

    def test_model_rejects_invalid_values(self):
        for value in (0, -1, True, 2.5):
            with self.assertRaises(ValueError):
                Source(
                    name="x",
                    url="https://example.com/feed",
                    kind="rss",
                    retention_days=value,
                )


class RetentionHttpBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(
            self.store,
            FakeFetcher({}),
            retention_days=0,
            clock=lambda: NOW,
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

    def _create(self, **extra):
        status, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": "https://example.com/feed", "kind": "rss", **extra}
        )
        self.assertEqual(status, 201)
        return json.loads(body)


class RetentionHttpTests(RetentionHttpBase):
    def test_create_and_read_retention_days(self):
        source = self._create(retention_days=14)
        self.assertEqual(source["retention_days"], 14)
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources"][0]["retention_days"], 14)
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["retention_days"], 14)

    def test_retention_days_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["retention_days"])

    def test_invalid_retention_days_rejected_over_http(self):
        for value in (0, -5, "soon", True):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": "https://example.com/feed",
                    "kind": "rss",
                    "retention_days": value,
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("retention_days", json.loads(body)["error"])

    def test_prune_endpoint_response_shape(self):
        source = self._create(retention_days=1)
        entry = self.store.save_entry(
            Entry(source_id=source["id"], title="old", body="b", fingerprint="old")
        )
        self.store._conn.execute(
            "UPDATE entries SET created_at = ? WHERE id = ?", (OLD, entry.id)
        )
        self.store.create_alert(
            Alert(source_id=source["id"], entry_id=entry.id, severity="warning", reason="r")
        )
        self.store.create_digest(Digest(alert_count=1, severity="warning", subject="d"))
        self.store._conn.commit()
        status, body = self.request("POST", "/api/prune")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"entries_pruned": 1, "alerts_pruned": 1}
        )
        self.assertEqual(len(self.store.list_sources()), 1)
        self.assertEqual(len(self.store.list_digests()), 1)

    def test_dashboard_renders_retention_label(self):
        self._create(retention_days=3)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Retention", body)
        self.assertIn(">3d<", body)

    def test_dashboard_renders_dash_when_unset(self):
        self._create()
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("\u2013", body)


class SecuredRetentionHttpTests(RetentionHttpBase):
    token = "secret-token"

    def test_prune_requires_token(self):
        status, _ = self.request("POST", "/api/prune")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/api/prune", token="wrong")
        self.assertEqual(status, 401)
        status, body = self.request("POST", "/api/prune", token="secret-token")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"entries_pruned": 0, "alerts_pruned": 0}
        )


if __name__ == "__main__":
    unittest.main()
