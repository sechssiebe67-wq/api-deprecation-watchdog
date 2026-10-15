import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import ApiError, create_server, validate_source_payload
from apiwatch.config import Config
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import Alert, Digest, Entry
from apiwatch.store import Store, _row_to_entry

FEED_URL = "https://example.com/feed"
RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>v1 deprecated</title><description>v1 will be removed.</description></item>
</channel></rss>
"""

STATUS_URL = "https://status.example.com/api/v2/incidents.json"
STATUSPAGE = json.dumps(
    {
        "incidents": [
            {
                "name": "v1 API deprecated",
                "status": "resolved",
                "impact": "major",
                "created_at": "2024-01-01T00:00:00Z",
                "shortlink": "https://stspg.io/xyz",
                "incident_updates": [{"body": "The v1 API is deprecated in favor of v2."}],
            }
        ]
    }
)


GITHUB_HTML_URL = "https://github.com/acme/widget"
GITHUB_API_URL = "https://api.github.com/repos/acme/widget/releases"
GITLAB_HTML_URL = "https://gitlab.com/acme/widget"
GITLAB_API_URL = "https://gitlab.com/api/v4/projects/acme%2Fwidget/releases"
GITLAB_RELEASES = json.dumps(
    [
        {
            "name": "v2.0.0",
            "tag_name": "v2.0.0",
            "description": "The legacy /v1 endpoints are deprecated.",
            "released_at": "2024-05-01T00:00:00.000Z",
            "upcoming_release": False,
            "_links": {"self": "https://gitlab.com/acme/widget/-/releases/v2.0.0"},
        }
    ]
)
GITHUB_RELEASES = json.dumps(
    [
        {
            "name": "v2.0.0",
            "tag_name": "v2.0.0",
            "body": "This release contains a breaking change in the auth API.",
            "html_url": "https://github.com/acme/widget/releases/tag/v2.0.0",
            "published_at": "2024-05-01T00:00:00Z",
            "prerelease": False,
            "draft": False,
        },
        {
            "name": "v1.9.0",
            "tag_name": "v1.9.0",
            "body": "New version released with minor fixes.",
            "html_url": "https://github.com/acme/widget/releases/tag/v1.9.0",
            "published_at": "2024-04-01T00:00:00Z",
            "prerelease": False,
            "draft": False,
        },
    ]
)


class AppTestCase(unittest.TestCase):
    token = None

    def setUp(self):
        self.store = Store(":memory:")
        self.fetcher = FakeFetcher({FEED_URL: RSS})
        self.engine = Engine(self.store, self.fetcher)
        self.config = Config(
            host="127.0.0.1",
            port=0,
            disable_scheduler=True,
            seed_sources=False,
            api_token=self.token,
        )
        self.server = create_server(self.store, self.engine, self.config, host="127.0.0.1", port=0)
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
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
                return response.status, body
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


class OpenAppTests(AppTestCase):
    def test_healthz(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_dashboard_renders(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("API Deprecation Watchdog", body)

    def test_dashboard_shows_keyword_indicator(self):
        self.request(
            "POST",
            "/api/sources",
            {
                "name": "Tuned",
                "url": FEED_URL,
                "kind": "rss",
                "extra_keywords": ["retired", "eol"],
                "suppress_keywords": ["notice"],
            },
        )
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Keywords", body)
        self.assertIn("+2 / -1", body)

    def test_create_and_list_source(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": FEED_URL, "kind": "rss", "tracked_versions": ["v1"]},
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["name"], "Example")
        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["sources"]), 1)

    def test_invalid_source_rejected(self):
        status, body = self.request("POST", "/api/sources", {"name": "x", "url": "ftp://x", "kind": "rss"})
        self.assertEqual(status, 400)

    def test_unknown_kind_rejected(self):
        status, _ = self.request(
            "POST", "/api/sources", {"name": "x", "url": FEED_URL, "kind": "sitemap"}
        )
        self.assertEqual(status, 400)

    def test_create_source_with_min_severity(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": FEED_URL, "kind": "rss", "min_severity": "warning"},
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["min_severity"], "warning")

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["min_severity"], "warning")

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sources"][0]["min_severity"], "warning")

    def test_source_min_severity_defaults_to_info(self):
        status, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["min_severity"], "info")

    def test_invalid_min_severity_rejected(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": FEED_URL, "kind": "rss", "min_severity": "fatal"},
        )
        self.assertEqual(status, 400)
        self.assertIn("min_severity", json.loads(body)["error"])

    def test_create_source_with_headers_and_auth_round_trips(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Private",
                "url": FEED_URL,
                "kind": "rss",
                "headers": {"X-Api-Key": "secret"},
                "auth": "user:pass",
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(source["auth"], "user:pass")

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        listed = json.loads(body)["sources"][0]
        self.assertEqual(listed["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(listed["auth"], "user:pass")

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(json.loads(body)["auth"], "user:pass")

    def test_source_without_headers_has_empty_defaults(self):
        status, body = self.request(
            "POST", "/api/sources", {"name": "Plain", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["headers"], {})
        self.assertIsNone(source["auth"])

    def test_headers_normalized_case_insensitively(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Plain",
                "url": FEED_URL,
                "kind": "rss",
                "headers": {"user-agent": "watchdog"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["headers"], {"User-Agent": "watchdog"})

    def test_invalid_headers_rejected(self):
        for headers in (["nope"], {"X-Api-Key": 1}, {"X-Api-Key": "a\nb"}):
            status, body = self.request(
                "POST",
                "/api/sources",
                {"name": "Bad", "url": FEED_URL, "kind": "rss", "headers": headers},
            )
            self.assertEqual(status, 400)
            self.assertIn("headers", json.loads(body)["error"])

    def test_invalid_auth_rejected(self):
        for auth in ("nocolon", ":pass", "user:"):
            status, body = self.request(
                "POST",
                "/api/sources",
                {"name": "Bad", "url": FEED_URL, "kind": "rss", "auth": auth},
            )
            self.assertEqual(status, 400)
            self.assertIn("auth", json.loads(body)["error"])

    def test_validate_source_keywords_default_to_empty(self):
        source = validate_source_payload(
            {"name": "Plain", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(source.extra_keywords, [])
        self.assertEqual(source.suppress_keywords, [])

    def test_validate_source_keywords_normalize_case_and_whitespace(self):
        source = validate_source_payload(
            {
                "name": "Tuned",
                "url": FEED_URL,
                "kind": "rss",
                "extra_keywords": ["  Retired  ", "EOL", "retired"],
                "suppress_keywords": ["Notice"],
            }
        )
        self.assertEqual(source.extra_keywords, ["retired", "eol"])
        self.assertEqual(source.suppress_keywords, ["notice"])

    def test_validate_source_keywords_reject_bad_values(self):
        for field in ("extra_keywords", "suppress_keywords"):
            for value in ("not-a-list", [1], ["ok", 2], [""], ["   "]):
                with self.assertRaises(ApiError) as ctx:
                    validate_source_payload(
                        {"name": "Bad", "url": FEED_URL, "kind": "rss", field: value}
                    )
                self.assertEqual(ctx.exception.status, 400)
                self.assertIn(field, ctx.exception.message)

    def test_create_source_keywords_round_trip(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Tuned",
                "url": FEED_URL,
                "kind": "rss",
                "extra_keywords": ["retired", "eol"],
                "suppress_keywords": ["notice"],
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["extra_keywords"], ["retired", "eol"])
        self.assertEqual(source["suppress_keywords"], ["notice"])

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["extra_keywords"], ["retired", "eol"])
        self.assertEqual(json.loads(body)["suppress_keywords"], ["notice"])

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        listed = json.loads(body)["sources"][0]
        self.assertEqual(listed["extra_keywords"], ["retired", "eol"])
        self.assertEqual(listed["suppress_keywords"], ["notice"])

    def test_create_source_default_keywords_are_empty(self):
        status, body = self.request(
            "POST", "/api/sources", {"name": "Plain", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["extra_keywords"], [])
        self.assertEqual(source["suppress_keywords"], [])

    def test_invalid_keywords_rejected_over_http(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Bad",
                "url": FEED_URL,
                "kind": "rss",
                "extra_keywords": "not-a-list",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(body)["error"], "'extra_keywords' must be a list of strings"
        )

    def test_statuspage_source_and_check(self):
        self.fetcher.responses[STATUS_URL] = STATUSPAGE
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example Status",
                "url": STATUS_URL,
                "kind": "statuspage",
                "tracked_versions": ["v1"],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["kind"], "statuspage")

        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["alerts_created"], 1)
        self.assertEqual(payload["alerts"][0]["severity"], "warning")
        self.assertIn("v1", payload["alerts"][0]["version_hits"])

    def test_github_url_normalization(self):
        source = validate_source_payload(
            {"name": "Widget SDK", "url": GITHUB_HTML_URL, "kind": "github"}
        )
        self.assertEqual(source.url, GITHUB_API_URL)
        self.assertEqual(
            validate_source_payload(
                {"name": "Widget SDK", "url": GITHUB_HTML_URL + ".git", "kind": "github"}
            ).url,
            GITHUB_API_URL,
        )
        self.assertEqual(
            validate_source_payload(
                {"name": "Widget SDK", "url": GITHUB_API_URL, "kind": "github"}
            ).url,
            GITHUB_API_URL,
        )
        # Non-GitHub URLs are left untouched.
        self.assertEqual(
            validate_source_payload(
                {"name": "Feed", "url": FEED_URL, "kind": "rss"}
            ).url,
            FEED_URL,
        )

    def test_create_github_source_normalizes_url(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Widget SDK",
                "url": GITHUB_HTML_URL,
                "kind": "github",
                "api_name": "Widget",
                "tracked_versions": ["v2"],
                "min_severity": "warning",
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["kind"], "github")
        self.assertEqual(source["url"], GITHUB_API_URL)

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        sources = json.loads(body)["sources"]
        self.assertEqual(sources[0]["kind"], "github")
        self.assertEqual(sources[0]["url"], GITHUB_API_URL)

    def test_github_source_scan_respects_floor_and_alerts(self):
        self.fetcher.responses[GITHUB_API_URL] = GITHUB_RELEASES
        status, _ = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Widget SDK",
                "url": GITHUB_HTML_URL,
                "kind": "github",
                "api_name": "Widget",
                "tracked_versions": ["v2"],
                "min_severity": "warning",
            },
        )
        self.assertEqual(status, 201)

        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        # The breaking-change release alerts; the info release is stored but
        # suppressed by the warning floor.
        self.assertEqual(payload["entries_new"], 2)
        self.assertEqual(payload["alerts_created"], 1)
        self.assertEqual(payload["alerts"][0]["severity"], "critical")
        self.assertIn("breaking change", payload["alerts"][0]["title"] + payload["alerts"][0]["reason"])

        status, body = self.request("GET", "/api/entries")
        self.assertEqual(len(json.loads(body)["entries"]), 2)

    def test_create_gitlab_source_normalizes_url(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Widget SDK (GitLab)",
                "url": GITLAB_HTML_URL,
                "kind": "gitlab",
                "api_name": "Widget",
                "tracked_versions": ["v2"],
                "min_severity": "warning",
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["kind"], "gitlab")
        self.assertEqual(source["url"], GITLAB_API_URL)

        # An existing API URL is stored unchanged.
        status, body = self.request(
            "POST",
            "/api/sources",
            {"name": "Widget API (GitLab)", "url": GITLAB_API_URL, "kind": "gitlab"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["url"], GITLAB_API_URL)

    def test_gitlab_source_scan_alerts_on_deprecation(self):
        self.fetcher.responses[GITLAB_API_URL] = GITLAB_RELEASES
        status, _ = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Widget SDK (GitLab)",
                "url": GITLAB_HTML_URL,
                "kind": "gitlab",
                "api_name": "Widget",
                "tracked_versions": ["v1"],
                "min_severity": "warning",
            },
        )
        self.assertEqual(status, 201)

        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["entries_new"], 1)
        self.assertEqual(payload["alerts_created"], 1)
        self.assertEqual(payload["alerts"][0]["severity"], "warning")

    def test_create_openapi_source_accepts_selector_and_min_severity(self):
        url = "https://api.acme.com/openapi.json"
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Acme API spec",
                "url": url,
                "kind": "openapi",
                "api_name": "Acme",
                "tracked_versions": ["v2"],
                "selector": "versions",
                "min_severity": "warning",
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["kind"], "openapi")
        self.assertEqual(source["selector"], "versions")
        self.assertEqual(source["min_severity"], "warning")
        # Non-GitHub URLs are never normalized, including openapi sources.
        self.assertEqual(source["url"], url)

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        stored = json.loads(body)["sources"][0]
        self.assertEqual(stored["selector"], "versions")

    def test_openapi_url_is_not_normalized(self):
        source = validate_source_payload(
            {
                "name": "Acme API spec",
                "url": "https://github.com/acme/widget",
                "kind": "openapi",
            }
        )
        self.assertEqual(source.url, "https://github.com/acme/widget")

    def test_delete_source(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        status, _ = self.request("DELETE", "/api/sources/%d" % source_id)
        self.assertEqual(status, 200)
        status, _ = self.request("DELETE", "/api/sources/%d" % source_id)
        self.assertEqual(status, 404)

    def test_check_endpoint(self):
        self.request("POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"})
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["alerts_created"], 1)
        status, body = self.request("GET", "/api/alerts")
        self.assertEqual(len(json.loads(body)["alerts"]), 1)

    def test_unknown_path(self):
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_digests_empty(self):
        status, body = self.request("GET", "/api/digests")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["digests"], [])

    def test_digest_flush_when_nothing_pending(self):
        status, body = self.request("POST", "/api/digest/flush")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["digest"])

    def test_digest_flow(self):
        self.engine.digest_mode = "digest"
        self.request("POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"})
        status, body = self.request("POST", "/api/check")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alerts_created"], 1)

        status, body = self.request("POST", "/api/digest/flush")
        self.assertEqual(status, 200)
        digest = json.loads(body)["digest"]
        self.assertIsNotNone(digest)
        self.assertEqual(digest["alert_count"], 1)
        self.assertEqual(digest["subject"], "API Deprecation Watchdog digest: 1 alert")
        self.assertEqual(digest["omitted"], 0)
        self.assertTrue(digest["delivered"])

        status, body = self.request("GET", "/api/digests")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["digests"]), 1)

        status, body = self.request("GET", "/api/digests/%d" % digest["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], digest["id"])

        status, body = self.request("GET", "/api/digests/999999")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_dashboard_lists_digests(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Recent digests", body)

    def test_dashboard_renders_history_link(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("/api/sources/%d/entries" % source_id, body)
        self.assertIn('>History</a>', body)

    def test_source_entries_newest_first(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        for title in ("first", "second", "third"):
            self.store.save_entry(
                Entry(source_id=source_id, title=title, body="", fingerprint=title)
            )
        status, body = self.request("GET", "/api/sources/%d/entries" % source_id)
        self.assertEqual(status, 200)
        entries = json.loads(body)["entries"]
        self.assertEqual([e["title"] for e in entries], ["third", "second", "first"])

    def test_source_entries_limit(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        for title in ("first", "second", "third"):
            self.store.save_entry(
                Entry(source_id=source_id, title=title, body="", fingerprint=title)
            )
        status, body = self.request(
            "GET", "/api/sources/%d/entries?limit=1" % source_id
        )
        self.assertEqual(status, 200)
        entries = json.loads(body)["entries"]
        self.assertEqual([e["title"] for e in entries], ["third"])

    def test_source_entries_scoped_to_source(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "One", "url": FEED_URL, "kind": "rss"}
        )
        first_id = json.loads(body)["id"]
        _, body = self.request(
            "POST", "/api/sources", {"name": "Two", "url": FEED_URL + "-2", "kind": "rss"}
        )
        second_id = json.loads(body)["id"]
        self.store.save_entry(
            Entry(source_id=first_id, title="a", body="", fingerprint="a")
        )
        self.store.save_entry(
            Entry(source_id=second_id, title="b", body="", fingerprint="b")
        )
        status, body = self.request("GET", "/api/sources/%d/entries" % first_id)
        self.assertEqual(status, 200)
        self.assertEqual([e["title"] for e in json.loads(body)["entries"]], ["a"])

    def test_source_entries_unknown_source(self):
        status, body = self.request("GET", "/api/sources/999999/entries")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "source not found")

    def test_source_entries_invalid_id(self):
        status, body = self.request("GET", "/api/sources/abc/entries")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid id")

    def test_source_entries_invalid_limit(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        status, body = self.request(
            "GET", "/api/sources/%d/entries?limit=nope" % source_id
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid limit")

    def test_source_entries_does_not_shadow_source_routes(self):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        status, body = self.request("GET", "/api/sources/%d" % source_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], source_id)
        status, _ = self.request("DELETE", "/api/sources/%d" % source_id)
        self.assertEqual(status, 200)

    def test_create_source_with_notify_webhook_url(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": FEED_URL,
                "kind": "rss",
                "notify_channels": ["webhook"],
                "notify_webhook_url": "https://example.test/critical",
            },
        )
        self.assertEqual(status, 201)
        source = json.loads(body)
        self.assertEqual(source["notify_channels"], ["webhook"])
        self.assertEqual(source["notify_webhook_url"], "https://example.test/critical")

        status, body = self.request("GET", "/api/sources")
        self.assertEqual(status, 200)
        stored = json.loads(body)["sources"][0]
        self.assertEqual(stored["notify_webhook_url"], "https://example.test/critical")

        status, body = self.request("GET", "/api/sources/%d" % source["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["notify_webhook_url"], "https://example.test/critical")

    def test_notify_webhook_url_defaults_to_null(self):
        status, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(status, 201)
        self.assertIsNone(json.loads(body)["notify_webhook_url"])

    def test_invalid_notify_webhook_url_rejected(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": FEED_URL,
                "kind": "rss",
                "notify_webhook_url": "not-a-url",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("notify_webhook_url", json.loads(body)["error"])

    def test_unknown_channel_error_lists_webhook(self):
        status, body = self.request(
            "POST",
            "/api/sources",
            {
                "name": "Example",
                "url": FEED_URL,
                "kind": "rss",
                "notify_channels": ["pagerduty"],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("webhook", json.loads(body)["error"])

    def _seed_entries(self, titles=("first", "second", "third", "fourth")):
        _, body = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        source_id = json.loads(body)["id"]
        for title in titles:
            self.store.save_entry(
                Entry(source_id=source_id, title=title, body="", fingerprint=title)
            )
        return source_id

    def test_entries_offset_pages_the_feed(self):
        self._seed_entries(("first", "second", "third", "fourth"))
        status, body = self.request("GET", "/api/entries?limit=2&offset=2")
        self.assertEqual(status, 200)
        titles = [e["title"] for e in json.loads(body)["entries"]]
        self.assertEqual(titles, ["second", "first"])

    def test_entries_offset_composes_with_limit(self):
        self._seed_entries(("first", "second", "third", "fourth"))
        status, body = self.request("GET", "/api/entries?limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual([e["title"] for e in json.loads(body)["entries"]], ["third"])

    def test_entries_offset_past_end_is_empty(self):
        self._seed_entries(("first", "second"))
        status, body = self.request("GET", "/api/entries?limit=100&offset=100000")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"entries": []})

    def test_entries_invalid_offset_rejected(self):
        for value in ("-1", "abc", "true"):
            status, body = self.request("GET", "/api/entries?offset=%s" % value)
            self.assertEqual(status, 400, value)
            self.assertIn("offset", json.loads(body)["error"])

    def test_entries_without_offset_matches_offset_zero(self):
        self._seed_entries(("first", "second", "third"))
        status_default, body_default = self.request("GET", "/api/entries")
        status_zero, body_zero = self.request("GET", "/api/entries?offset=0")
        self.assertEqual(status_default, 200)
        self.assertEqual(status_zero, 200)
        self.assertEqual(body_default, body_zero)
        self.assertEqual(
            [e["title"] for e in json.loads(body_default)["entries"]],
            ["third", "second", "first"],
        )

    def test_entries_without_offset_is_byte_identical_to_pre_pagination(self):
        self._seed_entries(("first", "second", "third"))
        status, body = self.request("GET", "/api/entries")
        self.assertEqual(status, 200)
        # The pre-pagination endpoint ran exactly this query; reproduce its
        # payload and assert the response is byte-for-byte identical.
        legacy_rows = self.store._conn.execute(
            "SELECT * FROM entries ORDER BY id DESC LIMIT ?", (100,)
        ).fetchall()
        legacy = [_row_to_entry(row).to_dict() for row in legacy_rows]
        expected = json.dumps({"entries": legacy}, ensure_ascii=False)
        self.assertEqual(body, expected)

    def test_source_entries_without_offset_is_byte_identical_to_pre_pagination(self):
        source_id = self._seed_entries(("first", "second", "third"))
        status, body = self.request("GET", "/api/sources/%d/entries" % source_id)
        self.assertEqual(status, 200)
        # The pre-pagination endpoint ran exactly this query; reproduce its
        # payload and assert the response is byte-for-byte identical.
        legacy_rows = self.store._conn.execute(
            """SELECT * FROM entries
               WHERE source_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (source_id, 100),
        ).fetchall()
        legacy = [_row_to_entry(row).to_dict() for row in legacy_rows]
        expected = json.dumps({"entries": legacy}, ensure_ascii=False)
        self.assertEqual(body, expected)

    def test_source_entries_offset_pages(self):
        source_id = self._seed_entries(("first", "second", "third", "fourth"))
        status, body = self.request(
            "GET", "/api/sources/%d/entries?limit=2&offset=2" % source_id
        )
        self.assertEqual(status, 200)
        titles = [e["title"] for e in json.loads(body)["entries"]]
        self.assertEqual(titles, ["second", "first"])

    def test_source_entries_offset_past_end_is_empty(self):
        source_id = self._seed_entries(("first", "second"))
        status, body = self.request(
            "GET", "/api/sources/%d/entries?limit=100&offset=100000" % source_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"entries": []})

    def test_source_entries_invalid_offset_rejected(self):
        source_id = self._seed_entries(("first",))
        status, body = self.request(
            "GET", "/api/sources/%d/entries?offset=-1" % source_id
        )
        self.assertEqual(status, 400)
        self.assertIn("offset", json.loads(body)["error"])

    def test_source_entries_unknown_source_with_offset(self):
        status, body = self.request("GET", "/api/sources/999999/entries?offset=1")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "source not found")


class ExportTests(AppTestCase):
    def _seed(self, sources=2, entries=3, alerts=3, digests=3):
        for index in range(sources):
            self.request(
                "POST",
                "/api/sources",
                {"name": "Source %d" % index, "url": FEED_URL, "kind": "rss"},
            )
        for index in range(entries):
            self.store.save_entry(
                Entry(source_id=1, title="entry %d" % index, body="", fingerprint="e%d" % index)
            )
        for index in range(alerts):
            self.store.create_alert(
                Alert(source_id=1, entry_id=index + 1, severity="warning", reason="deprecated")
            )
        for index in range(digests):
            self.store.create_digest(Digest(alert_count=1, severity="warning", subject="s"))

    def test_export_returns_all_keys(self):
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for key in ("generated_at", "stats", "sources", "entries", "alerts", "digests"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["sources"], list)
        self.assertIsInstance(payload["stats"], dict)
        self.assertIn("sources", payload["stats"])

    def test_export_limit_caps_collections_but_not_sources(self):
        self._seed(sources=2, entries=3, alerts=3, digests=3)
        status, body = self.request("GET", "/api/export?limit=1")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertEqual(len(payload["digests"]), 1)

    def test_export_default_limit_returns_everything_seeded(self):
        self._seed(sources=1, entries=3, alerts=3, digests=3)
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["entries"]), 3)
        self.assertEqual(len(payload["alerts"]), 3)
        self.assertEqual(len(payload["digests"]), 3)

    def test_export_rejects_non_integer_limit(self):
        status, body = self.request("GET", "/api/export?limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid limit")

    def test_export_rejects_out_of_range_limit(self):
        for limit in ("0", "-1", "10001"):
            status, body = self.request("GET", "/api/export?limit=%s" % limit)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"], "invalid limit")

    def test_dashboard_has_export_link(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('href="/api/export"', body)
        self.assertIn("Download JSON", body)


class SecuredAppTests(AppTestCase):
    token = "secret-token"

    def test_mutation_requires_token(self):
        status, _ = self.request(
            "POST", "/api/sources", {"name": "Example", "url": FEED_URL, "kind": "rss"}
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": FEED_URL, "kind": "rss"},
            token="wrong",
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST",
            "/api/sources",
            {"name": "Example", "url": FEED_URL, "kind": "rss"},
            token="secret-token",
        )
        self.assertEqual(status, 201)

    def test_reads_stay_open(self):
        status, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)

    def test_digest_flush_requires_token(self):
        status, _ = self.request("POST", "/api/digest/flush")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/api/digest/flush", token="secret-token")
        self.assertEqual(status, 200)

    def test_digests_read_stays_open(self):
        status, _ = self.request("GET", "/api/digests")
        self.assertEqual(status, 200)

    def test_export_stays_open_without_token(self):
        status, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("sources", json.loads(body))

    def test_export_stays_open_with_token(self):
        status, body = self.request("GET", "/api/export", token="secret-token")
        self.assertEqual(status, 200)
        self.assertIn("sources", json.loads(body))


if __name__ == "__main__":
    unittest.main()
