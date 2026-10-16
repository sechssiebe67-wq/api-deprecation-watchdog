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
from apiwatch.dashboard import render_dashboard
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Source
from apiwatch.store import Store


STATUSPAGE = json.dumps(
    {
        "page": {"id": "abc", "name": "Example Status"},
        "incidents": [
            {
                "name": "API incident",
                "status": "resolved",
                "impact": "major",
                "created_at": "2024-01-01T00:00:00Z",
                "shortlink": "https://stspg.io/api",
                "components": [{"id": "abc123", "name": "API"}],
                "incident_updates": [{"body": "The v1 API is deprecated."}],
            },
            {
                "name": "Billing incident",
                "status": "resolved",
                "impact": "minor",
                "created_at": "2024-01-02T00:00:00Z",
                "shortlink": "https://stspg.io/billing",
                "components": [{"id": "def456", "name": "Billing"}],
                "incident_updates": [{"body": "Billing is degraded."}],
            },
            {
                "name": "No component incident",
                "status": "investigating",
                "created_at": "2024-01-03T00:00:00Z",
                "shortlink": "https://stspg.io/none",
                "incident_updates": [{"body": "Something is wrong."}],
            },
            {
                "name": "Empty components incident",
                "status": "resolved",
                "created_at": "2024-01-04T00:00:00Z",
                "shortlink": "https://stspg.io/empty",
                "components": [],
                "incident_updates": [{"body": "Resolved."}],
            },
        ],
        "scheduled_maintenances": [
            {
                "name": "API maintenance",
                "status": "scheduled",
                "impact": "maintenance",
                "created_at": "2024-02-01T00:00:00Z",
                "shortlink": "https://stspg.io/maint",
                "components": [{"id": "ghi789", "name": "api"}],
                "incident_updates": [{"body": "A maintenance window."}],
            }
        ],
    }
)


class StatuspageComponentParserTests(unittest.TestCase):
    def _titles(self, components=None):
        from apiwatch import parsers

        return [
            entry.title
            for entry in parsers.parse_statuspage(STATUSPAGE, components=components)
        ]

    def test_empty_filter_is_byte_for_byte_unchanged(self):
        from apiwatch import parsers

        legacy = parsers.parse_statuspage(STATUSPAGE)
        unfiltered = parsers.parse_statuspage(STATUSPAGE, components=None)
        empty = parsers.parse_statuspage(STATUSPAGE, components=[])
        self.assertEqual(
            [(e.title, e.body, e.link, e.published, e.categories) for e in legacy],
            [(e.title, e.body, e.link, e.published, e.categories) for e in unfiltered],
        )
        self.assertEqual(legacy, empty)
        self.assertEqual(len(legacy), 5)

    def test_name_matches_case_insensitively(self):
        from apiwatch import parsers

        self.assertEqual(
            self._titles(["API"]),
            ["API incident", "API maintenance"],
        )
        self.assertEqual(self._titles(["api"]), ["API incident", "API maintenance"])
        self.assertEqual(self._titles(["Api"]), ["API incident", "API maintenance"])

    def test_unrelated_component_is_omitted(self):
        self.assertEqual(self._titles(["Billing"]), ["Billing incident"])

    def test_id_matches_exactly(self):
        self.assertEqual(self._titles(["abc123"]), ["API incident"])
        self.assertEqual(self._titles(["ABC123"]), [])

    def test_missing_or_empty_components_kept_only_when_filter_empty(self):
        titles = self._titles(["API"])
        self.assertNotIn("No component incident", titles)
        self.assertNotIn("Empty components incident", titles)
        self.assertNotIn("Empty components incident", self._titles(["Billing"]))
        # With no filter they are kept.
        self.assertIn("No component incident", self._titles([]))

    def test_dispatch_passes_the_filter(self):
        from apiwatch import parsers

        entries = parsers.parse("statuspage", STATUSPAGE, components=["API"])
        self.assertEqual(
            [e.title for e in entries], ["API incident", "API maintenance"]
        )


class StatuspageComponentModelTests(unittest.TestCase):
    def test_defaults_to_empty(self):
        source = Source(name="Status", url="https://s.test", kind="statuspage")
        self.assertEqual(source.statuspage_components, [])
        self.assertEqual(source.to_dict()["statuspage_components"], [])

    def test_trim_dedupe_and_preserve_case(self):
        source = Source(
            name="Status",
            url="https://s.test",
            kind="statuspage",
            statuspage_components=[" API ", "abc123", "API", "abc123"],
        )
        self.assertEqual(source.statuspage_components, ["API", "abc123"])

    def test_non_string_entry_rejected(self):
        with self.assertRaises(ValueError):
            Source(
                name="Status",
                url="https://s.test",
                kind="statuspage",
                statuspage_components=["API", 5],
            )

    def test_blank_entry_rejected(self):
        with self.assertRaises(ValueError):
            Source(
                name="Status",
                url="https://s.test",
                kind="statuspage",
                statuspage_components=["  "],
            )


class StatuspageComponentValidationTests(unittest.TestCase):
    def test_valid_list_is_trimmed_and_deduplicated(self):
        source = validate_source_payload(
            {
                "name": "Status",
                "url": "https://s.test",
                "kind": "statuspage",
                "statuspage_components": [" API ", "abc123", "API"],
            }
        )
        self.assertEqual(source.statuspage_components, ["API", "abc123"])

    def test_omitted_defaults_to_empty(self):
        source = validate_source_payload(
            {"name": "Status", "url": "https://s.test", "kind": "statuspage"}
        )
        self.assertEqual(source.statuspage_components, [])

    def test_non_list_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(
                {
                    "name": "Status",
                    "url": "https://s.test",
                    "kind": "statuspage",
                    "statuspage_components": "API",
                }
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("statuspage_components", ctx.exception.message)

    def test_non_string_entry_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(
                {
                    "name": "Status",
                    "url": "https://s.test",
                    "kind": "statuspage",
                    "statuspage_components": ["API", 5],
                }
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("statuspage_components", ctx.exception.message)

    def test_blank_entry_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(
                {
                    "name": "Status",
                    "url": "https://s.test",
                    "kind": "statuspage",
                    "statuspage_components": ["API", "  "],
                }
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("statuspage_components", ctx.exception.message)


class StatuspageComponentStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip_defaults(self):
        source = self.store.add_source(
            Source(name="Status", url="https://s.test", kind="statuspage")
        )
        fetched = self.store.get_source(source.id)
        self.assertEqual(fetched.statuspage_components, [])
        self.assertEqual(fetched.to_dict()["statuspage_components"], [])

    def test_round_trip_values(self):
        source = self.store.add_source(
            Source(
                name="Status",
                url="https://s.test",
                kind="statuspage",
                statuspage_components=["API", "abc123"],
            )
        )
        fetched = self.store.get_source(source.id)
        self.assertEqual(fetched.statuspage_components, ["API", "abc123"])
        self.assertEqual(
            self.store.list_sources()[0].statuspage_components, ["API", "abc123"]
        )

    def test_migration_adds_column_to_a_legacy_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE sources (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       name TEXT NOT NULL,
                       url TEXT NOT NULL,
                       kind TEXT NOT NULL,
                       api_name TEXT NOT NULL DEFAULT '',
                       tracked_versions TEXT NOT NULL DEFAULT '[]',
                       enabled INTEGER NOT NULL DEFAULT 1,
                       selector TEXT,
                       min_severity TEXT NOT NULL DEFAULT 'info',
                       created_at TEXT NOT NULL)"""
            )
            conn.execute(
                "INSERT INTO sources (name, url, kind, created_at) "
                "VALUES ('legacy', 'https://s.test/feed', 'statuspage', "
                "'2020-01-01T00:00:00+00:00')"
            )
            conn.commit()
            conn.close()

            store = Store(path)
            try:
                source = store.get_source(1)
                self.assertEqual(source.statuspage_components, [])
            finally:
                store.close()


class _AppTestCase(unittest.TestCase):
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


class StatuspageComponentAppTests(_AppTestCase):
    def test_post_and_get_round_trip(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Status",
                "url": "https://s.test/api/v2/incidents.json",
                "kind": "statuspage",
                "statuspage_components": [" API ", "abc123", "API"],
            },
        )
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertEqual(created["statuspage_components"], ["API", "abc123"])

        status, body = self.request("GET", "/api/sources/%d" % created["id"])
        self.assertEqual(status, 200)
        fetched = json.loads(body)
        self.assertEqual(fetched["statuspage_components"], ["API", "abc123"])

    def test_default_field_present_on_a_plain_source(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Status",
                "url": "https://s.test/api/v2/incidents.json",
                "kind": "statuspage",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["statuspage_components"], [])

    def test_non_list_rejected_with_400_naming_the_field(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Status",
                "url": "https://s.test/api/v2/incidents.json",
                "kind": "statuspage",
                "statuspage_components": "API",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("statuspage_components", json.loads(body)["error"])

    def test_blank_entry_rejected_with_400_naming_the_field(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Status",
                "url": "https://s.test/api/v2/incidents.json",
                "kind": "statuspage",
                "statuspage_components": ["API", " "],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("statuspage_components", json.loads(body)["error"])


class StatuspageComponentEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _scan(self, body, **kwargs):
        url = "https://s.test/api/v2/incidents.json"
        source = self.store.add_source(
            Source(name="Status", url=url, kind="statuspage", **kwargs)
        )
        engine = Engine(self.store, FakeFetcher({url: body}))
        return source, engine.scan_source(source)

    def test_scan_without_filter_keeps_everything(self):
        _, result = self._scan(STATUSPAGE)
        self.assertEqual(result.entries_seen, 5)

    def test_scan_with_filter_narrows_entries(self):
        _, result = self._scan(STATUSPAGE, statuspage_components=["API"])
        self.assertEqual(result.entries_seen, 2)
        titles = {entry.title for entry in self.store.list_entries()}
        self.assertEqual(titles, {"API incident", "API maintenance"})

    def test_scan_with_id_filter_is_exact(self):
        _, result = self._scan(STATUSPAGE, statuspage_components=["abc123"])
        self.assertEqual(result.entries_seen, 1)


class StatuspageComponentDashboardTests(unittest.TestCase):
    def test_components_indicator(self):
        source = Source(
            name="Status",
            url="https://s.test/api/v2/incidents.json",
            kind="statuspage",
            statuspage_components=["API", "abc123"],
        )
        markup = render_dashboard({}, [source], [])
        self.assertIn("components 2", markup)

    def test_components_dash_when_unset(self):
        source = Source(
            name="Status",
            url="https://s.test/api/v2/incidents.json",
            kind="statuspage",
        )
        markup = render_dashboard({}, [source], [])
        self.assertNotIn("components 0", markup)
        self.assertIn("\u2013", markup)


if __name__ == "__main__":
    unittest.main()
