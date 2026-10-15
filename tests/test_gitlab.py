import json
import unittest

from apiwatch import parsers
from apiwatch.app import ApiError, normalize_gitlab_url, validate_source_payload
from apiwatch.engine import Engine
from apiwatch.fetcher import FakeFetcher
from apiwatch.models import SOURCE_KINDS, Source
from apiwatch.store import Store

GITLAB_API_URL = "https://gitlab.com/api/v4/projects/group%2Fproject/releases"
GITLAB_HTML_URL = "https://gitlab.com/group/project"

GITLAB_RELEASES = json.dumps(
    [
        {
            "name": "v2.0.0",
            "tag_name": "v2.0.0",
            "description": "The legacy /v1 endpoints are deprecated and will be removed.",
            "released_at": "2024-05-01T00:00:00.000Z",
            "upcoming_release": False,
            "_links": {
                "self": "https://gitlab.com/api/v4/projects/1/releases/v2.0.0",
                "edit_url": "https://gitlab.com/group/project/-/releases/v2.0.0/edit",
            },
        },
        {
            "name": "v1.9.0",
            "tag_name": "v1.9.0",
            "description": "Minor bug fixes.",
            "released_at": "2024-04-01T00:00:00.000Z",
            "upcoming_release": True,
            "_links": {"self": "https://gitlab.com/api/v4/projects/1/releases/v1.9.0"},
        },
    ]
)


class GitlabParserTests(unittest.TestCase):
    def test_parses_two_releases(self):
        entries = parsers.parse_gitlab(GITLAB_RELEASES, base_url=GITLAB_API_URL)
        self.assertEqual(len(entries), 2)

        first = entries[0]
        self.assertEqual(first.title, "v2.0.0")
        self.assertIn("deprecated", first.body)
        self.assertEqual(
            first.link, "https://gitlab.com/api/v4/projects/1/releases/v2.0.0"
        )
        self.assertEqual(first.published, "2024-05-01T00:00:00.000Z")
        self.assertIn("v2.0.0", first.categories)
        self.assertNotIn("upcoming_release", first.categories)

        second = entries[1]
        self.assertEqual(second.title, "v1.9.0")
        self.assertEqual(second.body, "Minor bug fixes.")
        self.assertIn("upcoming_release", second.categories)

    def test_title_falls_back_to_tag_name(self):
        text = json.dumps([{"tag_name": "v3.1.4", "description": "notes"}])
        entries = parsers.parse_gitlab(text)
        self.assertEqual(entries[0].title, "v3.1.4")

    def test_dotted_selector_narrows_extraction(self):
        text = json.dumps(
            {
                "data": {"releases": [{"tag_name": "v1.0.0", "description": "hello"}]},
                "other": [{"tag_name": "nope"}],
            }
        )
        entries = parsers.parse_gitlab(text, selector="data.releases")
        self.assertEqual([e.title for e in entries], ["v1.0.0"])

    def test_dispatch(self):
        entries = parsers.parse("gitlab", GITLAB_RELEASES)
        self.assertEqual(len(entries), 2)

    def test_empty_and_non_json_return_empty(self):
        self.assertEqual(parsers.parse_gitlab(""), [])
        self.assertEqual(parsers.parse_gitlab("   "), [])
        self.assertEqual(parsers.parse_gitlab("not json"), [])
        self.assertEqual(parsers.parse_gitlab('{"not": "a list"}'), [])
        self.assertEqual(parsers.parse("gitlab", ""), [])
        self.assertEqual(parsers.parse("gitlab", "not json"), [])


class NormalizeGitlabUrlTests(unittest.TestCase):
    def test_html_url(self):
        self.assertEqual(normalize_gitlab_url(GITLAB_HTML_URL), GITLAB_API_URL)

    def test_html_url_with_git_suffix(self):
        self.assertEqual(
            normalize_gitlab_url(GITLAB_HTML_URL + ".git"), GITLAB_API_URL
        )

    def test_html_url_with_trailing_slash(self):
        self.assertEqual(
            normalize_gitlab_url(GITLAB_HTML_URL + "/"), GITLAB_API_URL
        )

    def test_nested_subgroup(self):
        self.assertEqual(
            normalize_gitlab_url("https://gitlab.com/group/subgroup/project"),
            "https://gitlab.com/api/v4/projects/group%2Fsubgroup%2Fproject/releases",
        )

    def test_api_url_unchanged(self):
        self.assertEqual(normalize_gitlab_url(GITLAB_API_URL), GITLAB_API_URL)

    def test_non_gitlab_url_unchanged(self):
        other = "https://example.com/group/project"
        self.assertEqual(normalize_gitlab_url(other), other)


class ValidateGitlabPayloadTests(unittest.TestCase):
    def test_accepts_gitlab_and_stores_normalized_url(self):
        source = validate_source_payload(
            {"name": "Foo", "url": GITLAB_HTML_URL, "kind": "gitlab"}
        )
        self.assertEqual(source.kind, "gitlab")
        self.assertEqual(source.url, GITLAB_API_URL)

    def test_api_url_is_left_unchanged(self):
        source = validate_source_payload(
            {"name": "Foo", "url": GITLAB_API_URL, "kind": "gitlab"}
        )
        self.assertEqual(source.url, GITLAB_API_URL)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            validate_source_payload(
                {"name": "Foo", "url": GITLAB_HTML_URL, "kind": "sitemap"}
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("gitlab", ctx.exception.message)

    def test_source_kinds_includes_gitlab(self):
        self.assertIn("gitlab", SOURCE_KINDS)


class GitlabEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_scan_creates_alerts_and_is_idempotent(self):
        source = self.store.add_source(
            Source(
                name="Widget on GitLab",
                url=GITLAB_API_URL,
                kind="gitlab",
                tracked_versions=["v1"],
                min_severity="warning",
            )
        )
        engine = Engine(self.store, FakeFetcher({GITLAB_API_URL: GITLAB_RELEASES}))
        result = engine.scan_source(source)
        self.assertEqual(result.entries_new, 2)
        self.assertEqual(result.alerts_created, 1)
        alert = result.alerts[0]
        self.assertIn(alert.severity, ("critical", "warning"))
        self.assertIn("deprecated", (alert.title + alert.reason).lower())

        second = engine.scan_source(source)
        self.assertEqual(second.entries_new, 0)
        self.assertEqual(second.alerts_created, 0)
        self.assertEqual(len(self.store.list_alerts()), 1)


if __name__ == "__main__":
    unittest.main()
