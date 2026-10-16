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


def rss(titles):
    """Build an RSS feed with one item per title (unique links/fingerprints)."""
    items = "".join(
        '<item><title>%s</title><description>%s</description>'
        "<link>https://example.com/%d</link></item>" % (title, title, index)
        for index, title in enumerate(titles)
    )
    return '<?xml version="1.0"?><rss version="2.0"><channel>%s</channel></rss>' % items


class StoreCapTests(unittest.TestCase):
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
            Entry(
                source_id=source_id,
                title=fingerprint,
                body="b",
                fingerprint=fingerprint,
            )
        )
        if created_at is not None:
            self.store._conn.execute(
                "UPDATE entries SET created_at = ? WHERE id = ?",
                (created_at, entry.id),
            )
            self.store._conn.commit()
        return entry

    def test_no_cap_is_unbounded(self):
        source = self._source()
        for i in range(6):
            self._entry(source.id, "e%d" % i)
        # ``None`` and ``0`` both mean "no cap" and delete nothing.
        self.assertEqual(self.store.prune_entries_for_source(source.id, None), 0)
        self.assertEqual(self.store.prune_entries_for_source(source.id, 0), 0)
        self.assertEqual(len(self.store.list_entries_for_source(source.id)), 6)
        self.assertEqual(self.store.stats()["entries_pruned"], 0)

    def test_cap_keeps_newest_n(self):
        source = self._source(max_entries=3)
        for i in range(6):
            self._entry(source.id, "e%d" % i)
        removed = self.store.prune_entries_for_source(source.id, 3)
        self.assertEqual(removed, 3)
        titles = [e.title for e in self.store.list_entries_for_source(source.id)]
        self.assertEqual(titles, ["e5", "e4", "e3"])
        self.assertEqual(self.store.stats()["entries_pruned"], 3)

    def test_cap_is_per_source(self):
        capped = self._source("capped", max_entries=2)
        uncapped = self._source("uncapped")
        for i in range(4):
            self._entry(capped.id, "c%d" % i)
            self._entry(uncapped.id, "u%d" % i)
        self.store.prune_entries_for_source(capped.id, capped.max_entries)
        self.assertEqual(len(self.store.list_entries_for_source(capped.id)), 2)
        self.assertEqual(len(self.store.list_entries_for_source(uncapped.id)), 4)

    def test_cap_leaves_alerts_and_digests(self):
        source = self._source(max_entries=1)
        old = self._entry(source.id, "old")
        kept = self._entry(source.id, "kept")
        self.store.create_alert(
            Alert(
                source_id=source.id,
                entry_id=old.id,
                severity="warning",
                reason="r",
                title="stored title",
                link="https://example.com/stored",
            )
        )
        self.store.create_alert(
            Alert(source_id=source.id, entry_id=kept.id, severity="warning", reason="r")
        )
        self.store.create_digest(
            Digest(alert_count=1, severity="warning", subject="digest")
        )
        removed = self.store.prune_entries_for_source(source.id, 1)
        self.assertEqual(removed, 1)
        # The entry is gone but both alerts survive with their stored fields.
        self.assertEqual([e.id for e in self.store.list_entries_for_source(source.id)], [kept.id])
        alerts = {a.entry_id: a for a in self.store.list_alerts()}
        self.assertEqual(set(alerts), {old.id, kept.id})
        self.assertEqual(alerts[old.id].title, "stored title")
        self.assertEqual(alerts[old.id].link, "https://example.com/stored")
        # Digests are never pruned by the cap.
        self.assertEqual(len(self.store.list_digests()), 1)

    def test_cap_composes_with_retention(self):
        source = self._source(retention_days=1, max_entries=2)
        self._entry(source.id, "stale0", created_at=OLD)
        self._entry(source.id, "stale1", created_at=OLD)
        self._entry(source.id, "fresh2")
        self._entry(source.id, "fresh3")
        self._entry(source.id, "fresh4")
        # Retention (age) removes the two stale entries first.
        retention = self.store.prune_entries(0, {}, NOW)
        self.assertEqual(retention["entries_pruned"], 2)
        # The count cap then keeps only the two newest of the survivors.
        removed = self.store.prune_entries_for_source(source.id, source.max_entries)
        self.assertEqual(removed, 1)
        titles = [e.title for e in self.store.list_entries_for_source(source.id)]
        self.assertEqual(titles, ["fresh4", "fresh3"])
        self.assertEqual(self.store.stats()["entries_pruned"], 3)

    def test_max_entries_round_trip(self):
        created = self._source(max_entries=25)
        self.assertEqual(created.max_entries, 25)
        self.assertEqual(created.to_dict()["max_entries"], 25)
        self.assertEqual(self.store.get_source(created.id).max_entries, 25)
        self.assertEqual(self.store.list_sources()[0].max_entries, 25)

    def test_max_entries_defaults_to_none(self):
        created = self._source()
        self.assertIsNone(created.max_entries)
        self.assertIsNone(created.to_dict()["max_entries"])
        self.assertIsNone(self.store.get_source(created.id).max_entries)


class MaxEntriesMigrationTests(unittest.TestCase):
    def test_migrates_sources_table_without_max_entries(self):
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
                    retention_days INTEGER,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, scan_interval_seconds,
                     retention_days, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1, NULL,
                        'info', '["all"]', NULL, NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("max_entries", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertIsNone(legacy.max_entries)
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    max_entries=42,
                )
            )
            self.assertEqual(fresh.max_entries, 42)
            store.close()
        finally:
            os.remove(path)


class EngineCapTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"
        self.fetcher = FakeFetcher({self.url: rss(["initial"])})
        self.engine = Engine(self.store, self.fetcher)

    def tearDown(self):
        self.store.close()

    def test_default_behaviour_unchanged_without_cap(self):
        source = self.store.add_source(
            Source(name="s", url=self.url, kind="rss")
        )
        self.fetcher.responses[self.url] = rss(["e%d" % i for i in range(5)])
        result = self.engine.scan_source(source)
        self.assertEqual(result.entries_new, 5)
        self.assertEqual(result.entries_capped, 0)
        self.assertEqual(len(self.store.list_entries_for_source(source.id)), 5)
        self.assertEqual(self.store.stats()["entries_pruned"], 0)

    def test_cap_trims_to_newest_n_on_every_scan(self):
        source = self.store.add_source(
            Source(name="s", url=self.url, kind="rss", max_entries=3)
        )
        self.fetcher.responses[self.url] = rss(["a%d" % i for i in range(5)])
        first = self.engine.scan_source(source)
        self.assertEqual(first.entries_new, 5)
        self.assertEqual(first.entries_capped, 2)
        self.assertEqual(
            [e.title for e in self.store.list_entries_for_source(source.id)],
            ["a4", "a3", "a2"],
        )

        # A later scan adds two more; the cap trims again on that scan. The feed
        # is a sliding window so previously-seen entries are not re-added.
        self.fetcher.responses[self.url] = rss(["a%d" % i for i in range(2, 7)])
        second = self.engine.scan_source(source)
        self.assertEqual(second.entries_new, 2)
        self.assertEqual(second.entries_capped, 2)
        self.assertEqual(
            [e.title for e in self.store.list_entries_for_source(source.id)],
            ["a6", "a5", "a4"],
        )

        self.fetcher.responses[self.url] = rss(["a%d" % i for i in range(4, 8)])
        third = self.engine.scan_source(source)
        self.assertEqual(third.entries_new, 1)
        self.assertEqual(third.entries_capped, 1)
        self.assertEqual(
            [e.title for e in self.store.list_entries_for_source(source.id)],
            ["a7", "a6", "a5"],
        )
        self.assertEqual(self.store.stats()["entries_pruned"], 5)

    def test_scan_sources_aggregates_entries_capped(self):
        source = self.store.add_source(
            Source(name="s", url=self.url, kind="rss", max_entries=2)
        )
        self.fetcher.responses[self.url] = rss(["e%d" % i for i in range(4)])
        result = self.engine.scan_sources([source])
        self.assertEqual(result.entries_capped, 2)
        self.assertEqual(result.to_dict()["entries_capped"], 2)

    def test_alert_for_pruned_entry_survives(self):
        source = self.store.add_source(
            Source(name="s", url=self.url, kind="rss", max_entries=1)
        )
        self.fetcher.responses[self.url] = rss(["v1 deprecated", "second"])
        result = self.engine.scan_source(source)
        self.assertEqual(result.alerts_created, 1)
        self.assertEqual(result.entries_capped, 1)
        # The alert's entry was capped away, but the alert (with its own stored
        # title and link) is still listed.
        titles = [e.title for e in self.store.list_entries_for_source(source.id)]
        self.assertEqual(titles, ["second"])
        alerts = self.store.list_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].title, "v1 deprecated")
        self.assertTrue(alerts[0].link)


class MaxEntriesValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {"name": "Example", "url": "https://example.com/feed", "kind": "rss"}
        payload.update(extra)
        return payload

    def test_omitted_and_null_are_none(self):
        self.assertIsNone(validate_source_payload(self._payload()).max_entries)
        self.assertIsNone(
            validate_source_payload(self._payload(max_entries=None)).max_entries
        )

    def test_positive_integer_accepted(self):
        source = validate_source_payload(self._payload(max_entries=100))
        self.assertEqual(source.max_entries, 100)

    def test_invalid_values_rejected_naming_field(self):
        for value in (0, -1, 1.5, "5", True, False):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(self._payload(max_entries=value))
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("max_entries", ctx.exception.message)

    def test_model_rejects_invalid_values(self):
        for value in (0, -1, True, 2.5):
            with self.assertRaises(ValueError):
                Source(
                    name="x",
                    url="https://example.com/feed",
                    kind="rss",
                    max_entries=value,
                )


class MaxEntriesHttpBase(unittest.TestCase):
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
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": "https://example.com/feed",
                "kind": "rss",
                **extra,
            },
        )
        self.assertEqual(status, 201)
        return json.loads(body)


class MaxEntriesHttpTests(MaxEntriesHttpBase):
    def test_create_and_read_max_entries(self):
        source = self._create(max_entries=50)
        self.assertEqual(source["max_entries"], 50)
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources"][0]["max_entries"], 50)
        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["max_entries"], 50)

    def test_max_entries_defaults_to_null(self):
        source = self._create()
        self.assertIsNone(source["max_entries"])

    def test_invalid_max_entries_rejected_over_http(self):
        for value in (0, -5, "5", True, 2.5):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Bad",
                    "url": "https://example.com/feed",
                    "kind": "rss",
                    "max_entries": value,
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("max_entries", json.loads(body)["error"])

    def test_alerts_survive_cap_over_http(self):
        source = self._create(max_entries=1)
        old = self.store.save_entry(
            Entry(
                source_id=source["id"],
                title="old deprecation",
                body="b",
                link="https://example.com/old",
                fingerprint="old",
            )
        )
        kept = self.store.save_entry(
            Entry(source_id=source["id"], title="kept", body="b", fingerprint="kept")
        )
        self.store.create_alert(
            Alert(
                source_id=source["id"],
                entry_id=old.id,
                severity="warning",
                reason="r",
                title="old deprecation",
                link="https://example.com/old",
            )
        )
        self.store.create_digest(Digest(alert_count=1, severity="warning", subject="d"))
        removed = self.store.prune_entries_for_source(source["id"], 1)
        self.assertEqual(removed, 1)
        status, body = self.request("GET", "/api/alerts")
        self.assertEqual(status, 200)
        alerts = json.loads(body)["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "old deprecation")
        self.assertEqual(alerts[0]["link"], "https://example.com/old")
        self.assertEqual([e.id for e in self.store.list_entries_for_source(source["id"])], [kept.id])
        status, body = self.request("GET", "/api/digests")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["digests"]), 1)

    def test_stats_reports_entries_pruned(self):
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(json.loads(body)["entries_pruned"], 0)
        source = self._create(max_entries=2)
        for i in range(5):
            self.store.save_entry(
                Entry(source_id=source["id"], title="t%d" % i, body="b", fingerprint="t%d" % i)
            )
        self.store.prune_entries_for_source(source["id"], 2)
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(json.loads(body)["entries_pruned"], 3)

    def test_dashboard_renders_cap_indicator(self):
        self._create(max_entries=7)
        self._create(name="Unbounded", url="https://example.com/other")
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("cap 7", body)
        self.assertIn("cap \u2013", body)


class SecuredMaxEntriesHttpTests(MaxEntriesHttpBase):
    token = "secret-token"

    def test_create_requires_token(self):
        status, _ = self.request(
            "POST",
            "/api/sources",
            {"name": "x", "url": "https://example.com/feed", "kind": "rss", "max_entries": 1},
        )
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
