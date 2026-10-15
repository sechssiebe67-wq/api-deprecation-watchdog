import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apiwatch.app import create_server
from apiwatch.config import Config
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.store import Store

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


if __name__ == "__main__":
    unittest.main()
