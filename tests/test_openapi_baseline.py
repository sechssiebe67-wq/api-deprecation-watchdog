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
from apiwatch.parsers import parse_openapi
from apiwatch.store import Store


def _spec(paths):
    return {
        "openapi": "3.0.0",
        "info": {"version": "1.0.0", "contact": {"url": "https://api.acme.com/support"}},
        "paths": paths,
    }


def _op(schema_type="object"):
    return {
        "responses": {
            "200": {
                "description": "ok",
                "content": {"application/json": {"schema": {"type": schema_type}}},
            }
        }
    }


LIVE = _spec({"/users": {"get": _op()}})
CHANGED = _spec({"/users": {"get": _op("string")}})


class OpenApiBaselineParserTests(unittest.TestCase):
    def test_no_baseline_still_surfaces_the_empty_spec_diff(self):
        entries = parse_openapi(json.dumps(LIVE))
        self.assertEqual(
            [e.title for e in entries],
            ["/users: added path"],
        )
        self.assertTrue(all(e.severity == "info" for e in entries))

    def test_baseline_matching_live_produces_no_entries(self):
        self.assertEqual(
            parse_openapi(json.dumps(LIVE), baseline=LIVE),
            [],
        )

    def test_only_changes_relative_to_baseline_are_emitted(self):
        entries = parse_openapi(json.dumps(CHANGED), baseline=LIVE)
        self.assertEqual(
            [e.title for e in entries],
            ["GET /users: changed response schema type"],
        )
        self.assertEqual(entries[0].severity, "warning")
        self.assertIn("/users", entries[0].categories)

    def test_ignore_exact_path_drops_the_change(self):
        live = _spec({})
        baseline = _spec({"/v2/legacy": {"get": _op()}})
        entries = parse_openapi(
            json.dumps(live),
            baseline=baseline,
            ignore_paths=["paths./v2/legacy"],
        )
        self.assertEqual(entries, [])

    def test_ignore_prefix_covers_nested_paths(self):
        baseline = _spec(
            {
                "/v2/legacy": {"get": _op()},
                "/v2/legacy/sub": {"get": _op()},
                "/users": {"get": _op()},
            }
        )
        live = _spec({"/users": {"get": _op("string")}})
        entries = parse_openapi(
            json.dumps(live),
            baseline=baseline,
            ignore_paths=["paths./v2/legacy"],
        )
        self.assertEqual(
            [e.title for e in entries],
            ["GET /users: changed response schema type"],
        )

    def test_ignore_prefix_respects_the_slash_boundary(self):
        baseline = _spec({"/v2/legacyv2": {"get": _op()}})
        live = _spec({})
        entries = parse_openapi(
            json.dumps(live),
            baseline=baseline,
            ignore_paths=["paths./v2/legacy"],
        )
        self.assertEqual([e.title for e in entries], ["/v2/legacyv2: removed path"])

    def test_ignored_change_does_not_hide_an_unrelated_one(self):
        baseline = _spec(
            {
                "/v2/legacy": {"get": _op()},
                "/users": {"get": _op()},
            }
        )
        live = _spec({"/users": {"get": _op("string")}})
        entries = parse_openapi(
            json.dumps(live),
            baseline=baseline,
            ignore_paths=["paths./v2/legacy"],
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "GET /users: changed response schema type")


class OpenApiSourceModelTests(unittest.TestCase):
    def test_defaults_are_none_and_empty(self):
        source = Source(name="x", url="https://x.test", kind="openapi")
        self.assertIsNone(source.openapi_baseline)
        self.assertEqual(source.openapi_ignore_paths, [])
        data = source.to_dict()
        self.assertIsNone(data["openapi_baseline"])
        self.assertEqual(data["openapi_ignore_paths"], [])

    def test_to_dict_round_trips_both_fields(self):
        source = Source(
            name="x",
            url="https://x.test",
            kind="openapi",
            openapi_baseline=LIVE,
            openapi_ignore_paths=["paths./a", "paths./b"],
        )
        data = source.to_dict()
        self.assertEqual(data["openapi_baseline"], LIVE)
        self.assertEqual(data["openapi_ignore_paths"], ["paths./a", "paths./b"])

    def test_baseline_must_be_an_object(self):
        for bad in (["nope"], "nope", 3):
            with self.assertRaises(ValueError) as ctx:
                Source(
                    name="x",
                    url="https://x.test",
                    kind="openapi",
                    openapi_baseline=bad,
                )
            self.assertIn("openapi_baseline", str(ctx.exception))

    def test_ignore_paths_are_trimmed_and_deduplicated(self):
        source = Source(
            name="x",
            url="https://x.test",
            kind="openapi",
            openapi_ignore_paths=[" paths./a ", "paths./a", "paths./b"],
        )
        self.assertEqual(source.openapi_ignore_paths, ["paths./a", "paths./b"])

    def test_ignore_paths_must_be_strings(self):
        for bad in ("nope", [1], ["  "]):
            with self.assertRaises(ValueError) as ctx:
                Source(
                    name="x",
                    url="https://x.test",
                    kind="openapi",
                    openapi_ignore_paths=bad,
                )
            self.assertIn("openapi_ignore_paths", str(ctx.exception))


class OpenApiValidationTests(unittest.TestCase):
    def _payload(self, **kwargs):
        payload = {"name": "Spec", "url": "https://acme.test/openapi.json", "kind": "openapi"}
        payload.update(kwargs)
        return payload

    def test_valid_baseline_object_is_accepted(self):
        source = validate_source_payload(self._payload(openapi_baseline=LIVE))
        self.assertEqual(source.openapi_baseline, LIVE)

    def test_non_object_baseline_rejected(self):
        for bad in (["nope"], "nope", 3):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(self._payload(openapi_baseline=bad))
            self.assertIn("openapi_baseline", ctx.exception.message)

    def test_ignore_paths_trimmed_and_deduplicated(self):
        source = validate_source_payload(
            self._payload(
                openapi_ignore_paths=["paths./v2/legacy", " paths./v2/legacy "]
            )
        )
        self.assertEqual(source.openapi_ignore_paths, ["paths./v2/legacy"])

    def test_non_list_ignore_paths_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(self._payload(openapi_ignore_paths="nope"))
        self.assertIn("openapi_ignore_paths", ctx.exception.message)

    def test_blank_ignore_path_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(self._payload(openapi_ignore_paths=["paths./a", "  "]))
        self.assertIn("openapi_ignore_paths", ctx.exception.message)


class OpenApiStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip_defaults(self):
        source = self.store.add_source(
            Source(name="Spec", url="https://acme.test/openapi.json", kind="openapi")
        )
        fetched = self.store.get_source(source.id)
        self.assertIsNone(fetched.openapi_baseline)
        self.assertEqual(fetched.openapi_ignore_paths, [])
        self.assertIsNone(fetched.to_dict()["openapi_baseline"])
        self.assertEqual(fetched.to_dict()["openapi_ignore_paths"], [])

    def test_round_trip_values(self):
        source = self.store.add_source(
            Source(
                name="Spec",
                url="https://acme.test/openapi.json",
                kind="openapi",
                openapi_baseline=LIVE,
                openapi_ignore_paths=["paths./v2/legacy"],
            )
        )
        fetched = self.store.get_source(source.id)
        self.assertEqual(fetched.openapi_baseline, LIVE)
        self.assertEqual(fetched.openapi_ignore_paths, ["paths./v2/legacy"])
        listed = self.store.list_sources()
        self.assertEqual(listed[0].openapi_baseline, LIVE)
        self.assertEqual(listed[0].openapi_ignore_paths, ["paths./v2/legacy"])

    def test_migration_adds_columns_to_a_legacy_database(self):
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
                "VALUES ('legacy', 'https://acme.test/feed', 'rss', "
                "'2020-01-01T00:00:00+00:00')"
            )
            conn.commit()
            conn.close()

            store = Store(path)
            try:
                source = store.get_source(1)
                self.assertIsNone(source.openapi_baseline)
                self.assertEqual(source.openapi_ignore_paths, [])
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


class OpenApiAppTests(_AppTestCase):
    def test_post_and_get_round_trip(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Spec",
                "url": "https://acme.test/openapi.json",
                "kind": "openapi",
                "openapi_baseline": LIVE,
                "openapi_ignore_paths": ["paths./v2/legacy", " paths./v2/legacy "],
            },
        )
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertEqual(created["openapi_baseline"], LIVE)
        self.assertEqual(created["openapi_ignore_paths"], ["paths./v2/legacy"])

        source_id = created["id"]
        status, body = self.request("GET", "/api/sources/%d" % source_id)
        self.assertEqual(status, 200)
        fetched = json.loads(body)
        self.assertEqual(fetched["openapi_baseline"], LIVE)
        self.assertEqual(fetched["openapi_ignore_paths"], ["paths./v2/legacy"])

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        listed = json.loads(body)["sources"][0]
        self.assertEqual(listed["openapi_baseline"], LIVE)
        self.assertEqual(listed["openapi_ignore_paths"], ["paths./v2/legacy"])

    def test_default_fields_on_a_plain_source(self):
        self.request(
            "POST",
            "/api/sources",
            {"name": "Spec", "url": "https://acme.test/openapi.json", "kind": "openapi"},
        )
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        listed = json.loads(body)["sources"][0]
        self.assertIsNone(listed["openapi_baseline"])
        self.assertEqual(listed["openapi_ignore_paths"], [])

    def test_invalid_baseline_rejected(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Spec", "url": "https://acme.test/openapi.json", "kind": "openapi",
             "openapi_baseline": ["nope"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("openapi_baseline", json.loads(body)["error"])

    def test_invalid_ignore_paths_rejected(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Spec", "url": "https://acme.test/openapi.json", "kind": "openapi",
             "openapi_ignore_paths": "nope"},
        )
        self.assertEqual(status, 400)
        self.assertIn("openapi_ignore_paths", json.loads(body)["error"])


class OpenApiEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _scan(self, url, body, **kwargs):
        source = self.store.add_source(
            Source(name="Spec", url=url, kind="openapi", **kwargs)
        )
        engine = Engine(self.store, FakeFetcher({url: body}))
        return source, engine.scan_source(source)

    def test_baseline_matching_live_scan_produces_no_entries(self):
        _, result = self._scan(
            "https://acme.test/openapi.json",
            json.dumps(LIVE),
            openapi_baseline=LIVE,
        )
        self.assertEqual(result.entries_seen, 0)
        self.assertEqual(result.entries_new, 0)
        self.assertEqual(result.alerts_created, 0)

    def test_baseline_scan_emits_only_the_relative_change(self):
        _, result = self._scan(
            "https://acme.test/openapi.json",
            json.dumps(CHANGED),
            openapi_baseline=LIVE,
        )
        self.assertEqual(result.entries_seen, 1)
        self.assertEqual(result.entries_new, 1)
        self.assertEqual(result.alerts_created, 1)
        alert = result.alerts[0]
        self.assertIn("/users", alert.title)
        self.assertEqual(alert.severity, "warning")

    def test_ignore_paths_mute_the_change_during_a_scan(self):
        baseline = _spec(
            {"/v2/legacy": {"get": _op()}, "/users": {"get": _op()}}
        )
        live = _spec({"/users": {"get": _op("string")}})
        _, result = self._scan(
            "https://acme.test/openapi.json",
            json.dumps(live),
            openapi_baseline=baseline,
            openapi_ignore_paths=["paths./v2/legacy"],
        )
        self.assertEqual(result.entries_seen, 1)
        self.assertEqual(result.alerts[0].title, "GET /users: changed response schema type")


class OpenApiDashboardTests(unittest.TestCase):
    def test_indicator_renders_baseline_and_ignore_count(self):
        source = Source(
            name="Spec",
            url="https://acme.test/openapi.json",
            kind="openapi",
            openapi_baseline=LIVE,
            openapi_ignore_paths=["paths./a", "paths./b"],
        )
        markup = render_dashboard({}, [source], [])
        self.assertIn("baseline", markup)
        self.assertIn("ignore 2", markup)
        self.assertNotIn("api.acme.com/support", markup)

    def test_indicator_is_dash_when_unset(self):
        source = Source(name="Spec", url="https://acme.test/openapi.json", kind="openapi")
        markup = render_dashboard({}, [source], [])
        self.assertIn("\u2013", markup)
        self.assertNotIn("baseline", markup)


if __name__ == "__main__":
    unittest.main()
