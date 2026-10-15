"""Tests for the per-fetch response body byte cap and truncation reporting."""
import email.message
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import ApiError, create_server, validate_source_payload
from apiwatch.config import (
    FETCH_MAX_BYTES_DEFAULT,
    FETCH_MAX_BYTES_MIN,
    Config,
)
from apiwatch.dashboard import render_dashboard
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher, FetchError, Fetcher
from apiwatch.models import Source
from apiwatch.store import Store

FEED_URL = "https://example.com/feed"
FEED_JSON = json.dumps(
    {
        "releases": [
            {
                "title": "v1 deprecated",
                "body": "v1 will be removed.",
                "link": "https://example.com/1",
            }
        ]
    }
)


class _FakeResponse:
    """Minimal ``urlopen`` response with a configurable body and headers."""

    def __init__(self, body: bytes, content_length=None):
        self._body = body
        self._pos = 0
        self.headers = email.message.Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, size=-1):
        if size is None or size < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + size]
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fetch(body, max_bytes=None, content_length=None, allow_truncated=False):
    response = _FakeResponse(body, content_length=content_length)
    with mock.patch("apiwatch.fetcher.urlopen", return_value=response):
        return Fetcher(timeout=1).fetch(
            "https://x.test/feed",
            max_bytes=max_bytes,
            allow_truncated=allow_truncated,
        )


class FetcherCapTests(unittest.TestCase):
    def test_no_cap_reads_the_whole_body(self):
        body = b"x" * 5000
        text, truncated = _fetch(body)
        self.assertEqual(len(text), 5000)
        self.assertFalse(truncated)

    def test_body_exactly_at_the_limit_is_not_truncated(self):
        body = b"x" * 100
        text, truncated = _fetch(body, max_bytes=100, content_length=100)
        self.assertEqual(len(text), 100)
        self.assertFalse(truncated)

    def test_body_one_byte_over_is_truncated(self):
        body = b"x" * 101
        text, truncated = _fetch(body, max_bytes=100)
        self.assertTrue(truncated)
        self.assertEqual(len(text), 100)

    def test_content_length_over_cap_raises(self):
        body = b"x" * 5000
        with self.assertRaises(FetchError) as ctx:
            _fetch(body, max_bytes=100, content_length=5000)
        self.assertIn("max_bytes", str(ctx.exception))

    def test_content_length_over_cap_allowed_with_opt_in(self):
        body = b"x" * 5000
        text, truncated = _fetch(
            body, max_bytes=100, content_length=5000, allow_truncated=True
        )
        self.assertTrue(truncated)
        self.assertEqual(len(text), 100)

    def test_content_length_within_cap_is_not_truncated(self):
        body = b"x" * 50
        text, truncated = _fetch(body, max_bytes=100, content_length=50)
        self.assertEqual(len(text), 50)
        self.assertFalse(truncated)

    def test_fake_fetcher_mirrors_the_cap(self):
        fetcher = FakeFetcher({FEED_URL: "abcdef"})
        text, truncated = fetcher.fetch(FEED_URL, max_bytes=3)
        self.assertEqual(text, "abc")
        self.assertTrue(truncated)
        self.assertEqual(fetcher.call_max_bytes[-1], 3)

        full, truncated = fetcher.fetch(FEED_URL, max_bytes=100)
        self.assertEqual(full, "abcdef")
        self.assertFalse(truncated)


class ConfigFetchMaxBytesTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(Config.from_env({}).fetch_max_bytes, FETCH_MAX_BYTES_DEFAULT)
        self.assertEqual(FETCH_MAX_BYTES_DEFAULT, 2_000_000)

    def test_value_is_read(self):
        self.assertEqual(
            Config.from_env({"FETCH_MAX_BYTES": "5000"}).fetch_max_bytes, 5000
        )

    def test_value_is_floored(self):
        self.assertEqual(
            Config.from_env({"FETCH_MAX_BYTES": "10"}).fetch_max_bytes,
            FETCH_MAX_BYTES_MIN,
        )

    def test_non_numeric_falls_back_to_default(self):
        self.assertEqual(
            Config.from_env({"FETCH_MAX_BYTES": "lots"}).fetch_max_bytes,
            FETCH_MAX_BYTES_DEFAULT,
        )

    def test_blank_falls_back_to_default(self):
        self.assertEqual(
            Config.from_env({"FETCH_MAX_BYTES": "  "}).fetch_max_bytes,
            FETCH_MAX_BYTES_DEFAULT,
        )


class SourceMaxBytesModelTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(name="x", url="https://x.test", kind="rss", **kwargs)

    def test_default_is_none(self):
        self.assertIsNone(self._source().max_bytes)
        self.assertIsNone(self._source().to_dict()["max_bytes"])

    def test_positive_integer_is_kept(self):
        source = self._source(max_bytes=5000)
        self.assertEqual(source.max_bytes, 5000)
        self.assertEqual(source.to_dict()["max_bytes"], 5000)

    def test_invalid_values_rejected(self):
        for value in (0, -1, True, False, 1.5, "1024"):
            with self.assertRaises(ValueError):
                self._source(max_bytes=value)


class AppMaxBytesValidationTests(unittest.TestCase):
    def _payload(self, **kwargs):
        payload = {"name": "Example", "url": FEED_URL, "kind": "rss"}
        payload.update(kwargs)
        return payload

    def test_omitted_is_none(self):
        self.assertIsNone(validate_source_payload(self._payload()).max_bytes)

    def test_null_is_none(self):
        self.assertIsNone(validate_source_payload(self._payload(max_bytes=None)).max_bytes)

    def test_positive_integer_is_kept(self):
        source = validate_source_payload(self._payload(max_bytes=5000))
        self.assertEqual(source.max_bytes, 5000)
        self.assertEqual(source.to_dict()["max_bytes"], 5000)

    def test_invalid_values_rejected(self):
        for value in (0, -1, True, 1.5, "1024"):
            with self.assertRaises(ApiError) as ctx:
                validate_source_payload(self._payload(max_bytes=value))
            self.assertEqual(ctx.exception.status, 400, "value=%r" % (value,))
            self.assertIn("max_bytes", ctx.exception.message)


class StoreMaxBytesTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_round_trip(self):
        created = self.store.add_source(
            Source(name="Example", url=FEED_URL, kind="rss", max_bytes=5000)
        )
        self.assertEqual(created.max_bytes, 5000)
        self.assertEqual(self.store.get_source(created.id).max_bytes, 5000)
        self.assertEqual(self.store.list_sources()[0].max_bytes, 5000)
        self.assertEqual(created.to_dict()["max_bytes"], 5000)

    def test_default_is_none(self):
        created = self.store.add_source(Source(name="Example", url=FEED_URL, kind="rss"))
        self.assertIsNone(created.max_bytes)

    def test_truncation_stat_round_trip(self):
        source = self.store.add_source(Source(name="Example", url=FEED_URL, kind="rss"))
        self.assertEqual(self.store.stats()["sources_truncated"], 0)
        self.store.record_source_truncation(source.id, True)
        self.assertTrue(self.store.get_source(source.id).truncated)
        self.assertEqual(self.store.stats()["sources_truncated"], 1)
        self.store.record_source_truncation(source.id, False)
        self.assertFalse(self.store.get_source(source.id).truncated)
        self.assertEqual(self.store.stats()["sources_truncated"], 0)

    def test_migrates_old_db_without_max_bytes(self):
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
                    extra_keywords TEXT,
                    suppress_keywords TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    breaker_until TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, notify_webhook_url,
                     scan_interval_seconds, headers, auth, extra_keywords,
                     suppress_keywords, failure_count, breaker_until, created_at)
                VALUES ('Legacy', 'https://legacy.example/feed', 'rss', '', '[]', 1,
                        NULL, 'info', '["all"]', NULL, NULL, NULL, NULL, NULL, NULL,
                        0, NULL, '2024-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            self.assertIn("max_bytes", columns)
            self.assertIn("truncated", columns)
            legacy = store.list_sources()[0]
            self.assertIsNone(legacy.max_bytes)
            self.assertFalse(legacy.truncated)
            fresh = store.add_source(
                Source(name="New", url="https://new.example/feed", kind="rss", max_bytes=4096)
            )
            self.assertEqual(fresh.max_bytes, 4096)
            store.close()
        finally:
            os.remove(path)


class EngineTruncationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.padded = FEED_JSON + " " * 50000

    def tearDown(self):
        self.store.close()

    def test_config_cap_truncates_but_still_parses_without_tripping(self):
        source = self.store.add_source(
            Source(name="Example", url=FEED_URL, kind="json")
        )
        fetcher = FakeFetcher({FEED_URL: self.padded})
        engine = Engine(self.store, fetcher, fetch_max_bytes=len(FEED_JSON) + 100)
        result = engine.scan_source(source)
        self.assertEqual(result.sources_truncated, 1)
        self.assertEqual(result.truncated_sources, ["Example"])
        self.assertEqual(result.entries_seen, 1)
        self.assertEqual(result.entries_new, 1)
        self.assertEqual(result.alerts_created, 1)
        # A clipped (but valid) body is not a failure and must not trip the breaker.
        self.assertEqual(result.sources_failed, 0)
        stored = self.store.get_source(source.id)
        self.assertEqual(stored.failure_count, 0)
        self.assertIsNone(stored.breaker_until)
        self.assertTrue(stored.truncated)
        self.assertEqual(self.store.stats()["sources_truncated"], 1)

    def test_per_source_override_used_over_config(self):
        source = self.store.add_source(
            Source(name="Overridden", url=FEED_URL, kind="json", max_bytes=5000)
        )
        fetcher = FakeFetcher({FEED_URL: FEED_JSON})
        engine = Engine(self.store, fetcher, fetch_max_bytes=10)
        result = engine.scan_source(source)
        # The per-source cap (5000) wins over the tiny engine default (10).
        self.assertEqual(fetcher.call_max_bytes[-1], 5000)
        self.assertEqual(result.sources_truncated, 0)
        self.assertEqual(result.entries_seen, 1)

    def test_untruncated_scan_clears_previous_truncation(self):
        source = self.store.add_source(
            Source(name="Example", url=FEED_URL, kind="json", max_bytes=len(FEED_JSON) + 100)
        )
        engine = Engine(self.store, FakeFetcher({FEED_URL: self.padded}))
        engine.scan_source(source)
        self.assertTrue(self.store.get_source(source.id).truncated)

        engine.fetcher.responses[FEED_URL] = FEED_JSON
        result = engine.scan_source(source)
        self.assertEqual(result.sources_truncated, 0)
        self.assertFalse(self.store.get_source(source.id).truncated)
        self.assertEqual(self.store.stats()["sources_truncated"], 0)

    def test_truncated_openapi_body_is_not_a_failure_and_records_flag(self):
        doc = json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Acme", "version": "1.0.0"},
                "paths": {
                    "/users": {"get": {"responses": {"200": {"description": "ok"}}}}
                },
            }
        )
        url = "https://example.com/openapi.json"
        source = self.store.add_source(Source(name="Spec", url=url, kind="openapi"))
        fetcher = FakeFetcher({url: doc + " " + "A" * 5000})
        engine = Engine(self.store, fetcher, fetch_max_bytes=len(doc) + 10)
        result = engine.scan_source(source)
        self.assertEqual(result.sources_truncated, 1)
        # The clipped body is not valid JSON, but that is an artifact of the cap
        # rather than malformed input, so it must not increment failure_count or
        # trip the breaker, and the truncation flag must still be persisted.
        stored = self.store.get_source(source.id)
        self.assertEqual(result.sources_failed, 0)
        self.assertEqual(stored.failure_count, 0)
        self.assertIsNone(stored.breaker_until)
        self.assertTrue(stored.truncated)
        self.assertEqual(self.store.stats()["sources_truncated"], 1)

    def test_repeated_openapi_truncation_never_trips_breaker(self):
        doc = json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Acme", "version": "1.0.0"},
                "paths": {},
            }
        )
        url = "https://example.com/openapi.json"
        source = self.store.add_source(Source(name="Spec", url=url, kind="openapi"))
        fetcher = FakeFetcher({url: doc + " " + "A" * 5000})
        engine = Engine(
            self.store, fetcher, breaker_threshold=2, fetch_max_bytes=len(doc) + 10
        )
        for _ in range(5):
            result = engine.scan_source(source)
            self.assertEqual(result.sources_truncated, 1)
        stored = self.store.get_source(source.id)
        self.assertEqual(stored.failure_count, 0)
        self.assertIsNone(stored.breaker_until)
        self.assertTrue(stored.truncated)

    def test_scan_all_aggregates_truncation(self):
        self.store.add_source(Source(name="Example", url=FEED_URL, kind="json"))
        engine = Engine(
            self.store, FakeFetcher({FEED_URL: self.padded}), fetch_max_bytes=len(FEED_JSON) + 50
        )
        result = engine.scan_all()
        self.assertEqual(result.sources_truncated, 1)
        self.assertEqual(result.truncated_sources, ["Example"])


class AppMaxBytesRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.engine = Engine(self.store, FakeFetcher({FEED_URL: FEED_JSON}))
        self.config = Config(
            host="127.0.0.1", port=0, disable_scheduler=True, seed_sources=False
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

    def test_round_trip_and_null(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": FEED_URL,
                "kind": "rss",
                "max_bytes": 5000,
            },
        )
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertEqual(created["max_bytes"], 5000)
        self.assertFalse(created["truncated"])

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources"][0]["max_bytes"], 5000)

        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Plain", "url": FEED_URL + "-2", "kind": "rss"},
        )
        self.assertEqual(status, 201)
        self.assertIsNone(json.loads(body)["max_bytes"])

    def test_invalid_values_rejected(self):
        for value in (0, -1, True, 1.5, "1024"):
            status, body = self.request(
                "POST",
                "/api/sources",
                {
                    "name": "Example",
                    "url": FEED_URL,
                    "kind": "rss",
                    "max_bytes": value,
                },
            )
            self.assertEqual(status, 400, "value=%r" % (value,))
            self.assertIn("max_bytes", json.loads(body)["error"])

    def test_stats_reports_sources_truncated(self):
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources_truncated"], 0)


class DashboardTruncationTests(unittest.TestCase):
    def test_truncated_badge_is_rendered_without_body(self):
        source = Source(
            name="Example",
            url=FEED_URL,
            kind="json",
            id=1,
            truncated=True,
        )
        html = render_dashboard({}, [source], [])
        self.assertIn("truncated", html)
        self.assertNotIn(FEED_JSON, html)

    def test_healthy_source_has_no_truncated_badge(self):
        source = Source(name="Example", url=FEED_URL, kind="json", id=1)
        html = render_dashboard({}, [source], [])
        self.assertNotIn("truncated", html)


if __name__ == "__main__":
    unittest.main()
