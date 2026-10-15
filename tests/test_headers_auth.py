import base64
import email.message
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from apiwatch.app import ApiError, validate_source_payload
from apiwatch.dashboard import render_dashboard
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher, Fetcher
from apiwatch.models import Source, canonical_header_name
from apiwatch.store import Store

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description></item>
</channel></rss>
"""


def _payload(**kwargs):
    payload = {
        "name": "Example",
        "url": "https://example.com/feed",
        "kind": "rss",
    }
    payload.update(kwargs)
    return payload


class HeaderNameTests(unittest.TestCase):
    def test_canonical_header_name_title_cases_each_part(self):
        self.assertEqual(canonical_header_name("user-agent"), "User-Agent")
        self.assertEqual(canonical_header_name("User-Agent"), "User-Agent")
        self.assertEqual(canonical_header_name("X-API-KEY"), "X-Api-Key")
        self.assertEqual(canonical_header_name("  x-api-key  "), "X-Api-Key")


class SourceModelTests(unittest.TestCase):
    def test_defaults(self):
        source = Source(name="x", url="https://x.test", kind="rss")
        self.assertEqual(source.headers, {})
        self.assertIsNone(source.auth)
        self.assertEqual(source.to_dict()["headers"], {})
        self.assertIsNone(source.to_dict()["auth"])

    def test_headers_are_normalized_and_last_write_wins(self):
        source = Source(
            name="x",
            url="https://x.test",
            kind="rss",
            headers={"user-agent": "first", "User-Agent": "second"},
        )
        self.assertEqual(source.headers, {"User-Agent": "second"})

    def test_to_dict_round_trips_headers_and_auth(self):
        source = Source(
            name="x",
            url="https://x.test",
            kind="rss",
            headers={"X-Api-Key": "secret"},
            auth="user:pass",
        )
        data = source.to_dict()
        self.assertEqual(data["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(data["auth"], "user:pass")

    def test_non_object_headers_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", headers=["nope"])
        self.assertIn("headers", str(ctx.exception))

    def test_non_string_header_value_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", headers={"X": 1})
        self.assertIn("headers", str(ctx.exception))

    def test_newline_header_value_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", headers={"X": "a\nb"})
        self.assertIn("headers", str(ctx.exception))

    def test_control_character_header_name_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", headers={"X\nY": "v"})
        self.assertIn("headers", str(ctx.exception))

    def test_auth_without_colon_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", auth="nocolon")
        self.assertIn("auth", str(ctx.exception))

    def test_auth_with_empty_user_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", auth=":pass")
        self.assertIn("auth", str(ctx.exception))

    def test_auth_with_empty_password_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            Source(name="x", url="https://x.test", kind="rss", auth="user:")
        self.assertIn("auth", str(ctx.exception))


class AppValidationTests(unittest.TestCase):
    def _error(self, **kwargs):
        try:
            validate_source_payload(_payload(**kwargs))
        except ApiError as exc:
            return exc
        self.fail("expected ApiError for %r" % (kwargs,))

    def test_non_object_headers_rejected_400(self):
        exc = self._error(headers=["nope"])
        self.assertEqual(exc.status, 400)
        self.assertIn("headers", exc.message)

    def test_non_string_header_value_rejected_400(self):
        exc = self._error(headers={"X-Api-Key": 1})
        self.assertEqual(exc.status, 400)
        self.assertIn("headers", exc.message)

    def test_newline_header_value_rejected_400(self):
        exc = self._error(headers={"X-Api-Key": "a\nb"})
        self.assertEqual(exc.status, 400)
        self.assertIn("headers", exc.message)

    def test_auth_without_colon_rejected_400(self):
        exc = self._error(auth="nocolon")
        self.assertEqual(exc.status, 400)
        self.assertIn("auth", exc.message)

    def test_auth_with_empty_user_rejected_400(self):
        exc = self._error(auth=":pass")
        self.assertEqual(exc.status, 400)
        self.assertIn("auth", exc.message)

    def test_auth_with_empty_password_rejected_400(self):
        exc = self._error(auth="user:")
        self.assertEqual(exc.status, 400)
        self.assertIn("auth", exc.message)

    def test_headers_are_normalized_on_registration(self):
        source = validate_source_payload(_payload(headers={"user-agent": "watchdog"}))
        self.assertEqual(source.headers, {"User-Agent": "watchdog"})

    def test_valid_headers_and_auth_accepted(self):
        source = validate_source_payload(
            _payload(headers={"X-Api-Key": "secret"}, auth="user:pass")
        )
        self.assertEqual(source.headers, {"X-Api-Key": "secret"})
        self.assertEqual(source.auth, "user:pass")


class _FakeResponse:
    def __init__(self):
        self.headers = email.message.Message()

    def read(self):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetcherTests(unittest.TestCase):
    def test_fetch_merges_headers_and_adds_basic_auth(self):
        fetcher = Fetcher(timeout=1, user_agent="UA/1")
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {
                key.lower(): value for key, value in request.headers.items()
            }
            return _FakeResponse()

        with mock.patch("apiwatch.fetcher.urlopen", side_effect=fake_urlopen):
            body = fetcher.fetch(
                "https://x.test/feed",
                headers={"X-Api-Key": "secret", "Accept": "application/json"},
                auth="user:pass",
            )

        self.assertEqual(body, "ok")
        self.assertEqual(captured["headers"]["user-agent"], "UA/1")
        self.assertEqual(captured["headers"]["accept"], "application/json")
        self.assertEqual(captured["headers"]["x-api-key"], "secret")
        self.assertEqual(
            captured["headers"]["authorization"],
            "Basic " + base64.b64encode(b"user:pass").decode("ascii"),
        )

    def test_fake_fetcher_records_headers_and_auth(self):
        fetcher = FakeFetcher({"https://x.test/feed": RSS})
        fetcher.fetch("https://x.test/feed", headers={"X-Api-Key": "secret"}, auth="u:p")
        self.assertEqual(fetcher.call_headers[-1], {"X-Api-Key": "secret"})
        self.assertEqual(fetcher.call_auth[-1], "u:p")


class StoreRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_headers_and_auth_round_trip(self):
        created = self.store.add_source(
            Source(
                name="Example",
                url="https://example.com/feed",
                kind="rss",
                headers={"X-Api-Key": "secret"},
                auth="user:pass",
            )
        )
        self.assertEqual(created.headers, {"X-Api-Key": "secret"})
        self.assertEqual(created.auth, "user:pass")
        fetched = self.store.get_source(created.id)
        self.assertEqual(fetched.headers, {"X-Api-Key": "secret"})
        self.assertEqual(fetched.auth, "user:pass")
        self.assertEqual(self.store.list_sources()[0].headers, {"X-Api-Key": "secret"})

    def test_defaults_round_trip(self):
        created = self.store.add_source(
            Source(name="Example", url="https://example.com/feed", kind="rss")
        )
        self.assertEqual(created.headers, {})
        self.assertIsNone(created.auth)
        self.assertEqual(created.to_dict()["headers"], {})
        self.assertIsNone(created.to_dict()["auth"])

    def test_migration_adds_headers_and_auth(self):
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
                    created_at TEXT NOT NULL
                );
                INSERT INTO sources
                    (name, url, kind, api_name, tracked_versions, enabled, selector,
                     min_severity, notify_channels, notify_webhook_url,
                     scan_interval_seconds, created_at)
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
            self.assertIn("headers", columns)
            self.assertIn("auth", columns)
            legacy = store.list_sources()[0]
            self.assertEqual(legacy.name, "Legacy")
            self.assertEqual(legacy.headers, {})
            self.assertIsNone(legacy.auth)
            self.assertEqual(legacy.to_dict()["headers"], {})
            self.assertIsNone(legacy.to_dict()["auth"])
            fresh = store.add_source(
                Source(
                    name="New",
                    url="https://new.example/feed",
                    kind="rss",
                    headers={"X-Api-Key": "secret"},
                    auth="user:pass",
                )
            )
            self.assertEqual(fresh.headers, {"X-Api-Key": "secret"})
            self.assertEqual(fresh.auth, "user:pass")
            store.close()
        finally:
            os.remove(path)


class EngineHeaderTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.url = "https://example.com/feed"

    def tearDown(self):
        self.store.close()

    def test_engine_passes_headers_and_auth_to_fetcher(self):
        source = self.store.add_source(
            Source(
                name="Example",
                url=self.url,
                kind="rss",
                headers={"X-Api-Key": "secret"},
                auth="user:pass",
            )
        )
        fetcher = FakeFetcher({self.url: RSS})
        Engine(self.store, fetcher).scan_source(source)
        self.assertEqual(fetcher.call_headers[-1], {"X-Api-Key": "secret"})
        self.assertEqual(fetcher.call_auth[-1], "user:pass")

    def test_engine_passes_empty_defaults(self):
        source = self.store.add_source(
            Source(name="Example", url=self.url, kind="rss")
        )
        fetcher = FakeFetcher({self.url: RSS})
        Engine(self.store, fetcher).scan_source(source)
        self.assertEqual(fetcher.call_headers[-1], {})
        self.assertIsNone(fetcher.call_auth[-1])


class DashboardAuthTests(unittest.TestCase):
    def _source(self, **kwargs):
        return Source(
            name="Example",
            url="https://example.com/feed",
            kind="rss",
            id=1,
            **kwargs,
        )

    def test_indicator_shown_without_secrets(self):
        source = self._source(
            headers={"X-Api-Key": "topsecret", "Accept": "application/json"},
            auth="user:hunter2",
        )
        html = render_dashboard({}, [source], [])
        self.assertIn("\U0001F512", html)
        self.assertIn("2 hdr", html)
        self.assertNotIn("topsecret", html)
        self.assertNotIn("hunter2", html)

    def test_no_indicator_without_headers_or_auth(self):
        html = render_dashboard({}, [self._source()], [])
        self.assertNotIn("\U0001F512", html)
        self.assertNotIn("hdr", html)


if __name__ == "__main__":
    unittest.main()
