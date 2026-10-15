import json
import unittest

from apiwatch import parsers


RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example API Changelog</title>
    <item>
      <title>v1 endpoints deprecated</title>
      <description>The v1 endpoints are deprecated and will be removed in June.</description>
      <link>https://example.com/changelog/v1</link>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
      <category>deprecation</category>
    </item>
    <item>
      <title>New dashboard</title>
      <description>A fresh dashboard.</description>
      <link>/changelog/dashboard</link>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed</title>
  <entry>
    <title>Sunsetting v2</title>
    <summary>v2 will be sunset soon.</summary>
    <link href="https://example.com/sunset"/>
    <updated>2024-02-01T10:00:00Z</updated>
  </entry>
</feed>
"""


class RssParserTests(unittest.TestCase):
    def test_parses_rss_items(self):
        entries = parsers.parse_rss(RSS, base_url="https://example.com/feed")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].title, "v1 endpoints deprecated")
        self.assertEqual(entries[0].link, "https://example.com/changelog/v1")
        self.assertIn("deprecation", entries[0].categories)
        self.assertEqual(entries[1].link, "https://example.com/changelog/dashboard")

    def test_parses_atom_entries(self):
        entries = parsers.parse_rss(ATOM)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Sunsetting v2")
        self.assertEqual(entries[0].link, "https://example.com/sunset")

    def test_malformed_returns_empty(self):
        self.assertEqual(parsers.parse_rss("<rss><channel>"), [])


class JsonParserTests(unittest.TestCase):
    def test_parses_list_shape(self):
        text = (
            '[{"title": "Remove v1", "body": "v1 is removed", '
            '"url": "https://x/1", "date": "2024-01-01", "tags": ["breaking"]}]'
        )
        entries = parsers.parse_json(text, base_url="https://x")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Remove v1")
        self.assertIn("breaking", entries[0].categories)

    def test_parses_container_and_nested_content(self):
        text = (
            '{"releases": [{"name": "2024-05-01", "content": {"rendered": "Deprecates v2"}, '
            '"link": "/rel/1"}]}'
        )
        entries = parsers.parse_json(text, base_url="https://api.example.com")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].body, "Deprecates v2")
        self.assertEqual(entries[0].link, "https://api.example.com/rel/1")

    def test_selector(self):
        text = '{"data": {"releases": [{"title": "A"}]}, "other": [{"title": "B"}]}'
        entries = parsers.parse_json(text, selector="data.releases")
        self.assertEqual([e.title for e in entries], ["A"])

    def test_nested_list_selector_with_index(self):
        text = json.dumps(
            {"data": {"changelogs": [{"entries": [{"title": "Removed v1"}]}]}}
        )
        entries = parsers.parse_json(text, selector="data.changelogs.0.entries")
        self.assertEqual([e.title for e in entries], ["Removed v1"])

    def test_nested_list_selector_with_negative_index(self):
        text = json.dumps(
            {
                "data": {
                    "changelogs": [
                        {"entries": [{"title": "Old"}]},
                        {"entries": [{"title": "Newest"}]},
                    ]
                }
            }
        )
        entries = parsers.parse_json(text, selector="data.changelogs.-1.entries")
        self.assertEqual([e.title for e in entries], ["Newest"])

    def test_index_selector_on_object_yields_no_entries(self):
        text = json.dumps({"data": {"changelogs": {"entries": [{"title": "A"}]}}})
        self.assertEqual(parsers.parse_json(text, selector="data.changelogs.0.entries"), [])

    def test_out_of_range_index_yields_no_entries(self):
        text = json.dumps({"data": {"changelogs": [{"entries": [{"title": "A"}]}]}})
        self.assertEqual(parsers.parse_json(text, selector="data.changelogs.5.entries"), [])

    def test_non_integer_index_on_list_yields_no_entries(self):
        text = json.dumps({"data": {"changelogs": [{"entries": [{"title": "A"}]}]}})
        self.assertEqual(parsers.parse_json(text, selector="data.changelogs.first.entries"), [])

    def test_invalid_json(self):
        self.assertEqual(parsers.parse_json("{nope"), [])


class SelectorWalkerTests(unittest.TestCase):
    def test_positive_index(self):
        data = {"data": {"releases": [{"title": "A"}, {"title": "B"}]}}
        self.assertEqual(
            parsers._apply_selector(data, "data.releases.1"), {"title": "B"}
        )

    def test_negative_one_is_last(self):
        data = {"data": {"releases": [{"title": "A"}, {"title": "B"}]}}
        self.assertEqual(
            parsers._apply_selector(data, "data.releases.-1"), {"title": "B"}
        )

    def test_out_of_range_returns_none(self):
        data = {"data": {"releases": [{"title": "A"}]}}
        self.assertIsNone(parsers._apply_selector(data, "data.releases.3"))
        self.assertIsNone(parsers._apply_selector(data, "data.releases.-2"))

    def test_non_integer_on_list_returns_none(self):
        data = {"data": {"releases": [{"title": "A"}]}}
        self.assertIsNone(parsers._apply_selector(data, "data.releases.first"))

    def test_integer_on_dict_returns_none(self):
        data = {"data": {"releases": {"0": {"title": "A"}}}}
        self.assertIsNone(parsers._apply_selector(data, "data.releases.0"))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(parsers._apply_selector({"a": 1}, "b"))

    def test_nested_dict_and_index(self):
        data = {"a": [{"b": [10, 20]}]}
        self.assertEqual(parsers._apply_selector(data, "a.0.b.-1"), 20)


STATUSPAGE = json.dumps(
    {
        "page": {"id": "abc", "name": "Example Status"},
        "incidents": [
            {
                "name": "v1 API degraded",
                "status": "resolved",
                "impact": "major",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T01:00:00Z",
                "shortlink": "https://stspg.io/abc",
                "incident_updates": [
                    {"body": "We are deprecating the v1 endpoints.", "status": "investigating"},
                    {"body": "The deprecated v1 endpoints have been removed.", "status": "resolved"},
                ],
            }
        ],
        "scheduled_maintenances": [
            {
                "name": "Database maintenance",
                "title": "Database maintenance",
                "status": "scheduled",
                "impact": "maintenance",
                "created_at": "2024-02-01T00:00:00Z",
                "updated_at": "2024-02-01T00:00:00Z",
                "shortlink": "https://stspg.io/def",
                "incident_updates": [{"body": "A scheduled maintenance window."}],
            }
        ],
    }
)


class StatuspageParserTests(unittest.TestCase):
    def test_parses_incidents_and_maintenances(self):
        entries = parsers.parse_statuspage(STATUSPAGE, base_url="https://status.example.com")
        self.assertEqual(len(entries), 2)

        incident = entries[0]
        self.assertEqual(incident.title, "v1 API degraded")
        self.assertIn("deprecating the v1 endpoints", incident.body)
        self.assertIn("have been removed", incident.body)
        self.assertEqual(incident.link, "https://stspg.io/abc")
        self.assertEqual(incident.published, "2024-01-01T00:00:00Z")
        self.assertIn("resolved", incident.categories)
        self.assertIn("major", incident.categories)

        maintenance = entries[1]
        self.assertEqual(maintenance.title, "Database maintenance")
        self.assertEqual(maintenance.body, "A scheduled maintenance window.")
        self.assertEqual(maintenance.link, "https://stspg.io/def")
        self.assertIn("scheduled", maintenance.categories)
        self.assertIn("maintenance", maintenance.categories)

    def test_dispatch(self):
        entries = parsers.parse("statuspage", STATUSPAGE, base_url="https://status.example.com")
        self.assertEqual(len(entries), 2)

    def test_malformed_returns_empty(self):
        self.assertEqual(parsers.parse_statuspage("{not json"), [])
        self.assertEqual(parsers.parse("statuspage", "{not json"), [])

    def test_empty_returns_empty(self):
        self.assertEqual(parsers.parse_statuspage(""), [])
        self.assertEqual(parsers.parse_statuspage("   "), [])


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
            "body": "Minor bug fixes.",
            "html_url": "https://github.com/acme/widget/releases/tag/v1.9.0",
            "published_at": "2024-04-01T00:00:00Z",
            "prerelease": False,
            "draft": False,
        },
    ]
)


class GithubParserTests(unittest.TestCase):
    def test_parses_two_releases(self):
        entries = parsers.parse_github(
            GITHUB_RELEASES, base_url="https://api.github.com/repos/acme/widget/releases"
        )
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first.title, "v2.0.0")
        self.assertIn("breaking change", first.body)
        self.assertEqual(first.link, "https://github.com/acme/widget/releases/tag/v2.0.0")
        self.assertEqual(first.published, "2024-05-01T00:00:00Z")
        self.assertIn("v2.0.0", first.categories)
        self.assertNotIn("prerelease", first.categories)

    def test_title_falls_back_to_tag_name_when_name_missing(self):
        text = json.dumps([{"tag_name": "v1.2.3", "body": "notes"}])
        entries = parsers.parse_github(text)
        self.assertEqual(entries[0].title, "v1.2.3")

    def test_drafts_are_skipped(self):
        text = json.dumps(
            [
                {"tag_name": "v3.0.0", "body": "still cooking", "draft": True},
                {"tag_name": "v2.5.0", "body": "shipped"},
            ]
        )
        entries = parsers.parse_github(text)
        self.assertEqual([e.title for e in entries], ["v2.5.0"])

    def test_prerelease_kept_and_categorized(self):
        text = json.dumps(
            [
                {
                    "name": "v3.0.0-rc1",
                    "tag_name": "v3.0.0-rc1",
                    "body": "release candidate",
                    "prerelease": True,
                }
            ]
        )
        entries = parsers.parse_github(text)
        self.assertEqual(len(entries), 1)
        self.assertIn("v3.0.0-rc1", entries[0].categories)
        self.assertIn("prerelease", entries[0].categories)

    def test_dotted_selector(self):
        text = json.dumps(
            {
                "data": {"releases": [{"tag_name": "v1.0.0"}]},
                "other": [{"tag_name": "nope"}],
            }
        )
        entries = parsers.parse_github(text, selector="data.releases")
        self.assertEqual([e.title for e in entries], ["v1.0.0"])

    def test_malformed_returns_empty(self):
        self.assertEqual(parsers.parse_github("not json"), [])
        self.assertEqual(parsers.parse_github(""), [])
        self.assertEqual(parsers.parse_github("   "), [])
        self.assertEqual(parsers.parse_github('{"not": "a list"}'), [])
        self.assertEqual(parsers.parse("github", "not json"), [])

    def test_dispatch(self):
        entries = parsers.parse("github", GITHUB_RELEASES)
        self.assertEqual(len(entries), 2)


class HtmlParserTests(unittest.TestCase):
    def test_headings_become_entries(self):
        markup = """
        <html><body>
          <h2>Deprecating v1</h2>
          <p>v1 is deprecated.</p>
          <p>Use v2 instead.</p>
          <h2>New feature</h2>
          <p>Something new.</p>
        </body></html>
        """
        entries = parsers.parse_html(markup)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].title, "Deprecating v1")
        self.assertIn("Use v2 instead.", entries[0].body)
        self.assertEqual(entries[1].title, "New feature")

    def test_list_without_headings(self):
        markup = "<ul><li>Deprecated v1</li><li>Sunset v2</li></ul>"
        entries = parsers.parse_html(markup)
        self.assertEqual([e.title for e in entries], ["Deprecated v1", "Sunset v2"])

    def test_selector_filters_text_substring(self):
        markup = "<h2>Deprecation</h2><p>old</p><h2>Marketing</h2><p>new</p>"
        entries = parsers.parse_html(markup, selector="deprecation")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Deprecation")

    def test_selector_by_tag_and_class(self):
        markup = (
            '<h2 class="deprecation">Deprecating v1</h2>'
            "<p>v1 is deprecated.</p>"
            '<h2 class="marketing">New feature</h2>'
            "<p>Something new.</p>"
        )
        entries = parsers.parse_html(markup, selector="h2.deprecation")
        self.assertEqual([e.title for e in entries], ["Deprecating v1"])

    def test_selector_by_tag(self):
        markup = "<h2>Deprecating v1</h2><p>old</p><h3>Details</h3><p>more</p>"
        entries = parsers.parse_html(markup, selector="h3")
        self.assertEqual([e.title for e in entries], ["Details"])

    def test_selector_by_container_id(self):
        markup = (
            '<div id="changelog"><h2>Deprecating v1</h2><p>v1 is deprecated.</p></div>'
            '<div id="marketing"><h2>Buy now</h2><p>Sale.</p></div>'
        )
        entries = parsers.parse_html(markup, selector="#changelog")
        self.assertEqual([e.title for e in entries], ["Deprecating v1"])
        self.assertIn("v1 is deprecated.", entries[0].body)

    def test_selector_by_class(self):
        markup = (
            '<section class="releases"><h2>Deprecating v1</h2><p>old</p></section>'
            '<section class="news"><h2>Hello</h2><p>new</p></section>'
        )
        entries = parsers.parse_html(markup, selector=".releases")
        self.assertEqual([e.title for e in entries], ["Deprecating v1"])

    def test_dispatch_unknown_kind(self):
        self.assertEqual(parsers.parse("xml", "<x/>"), [])


def _spec(paths, *, version="1.0.0", contact="/support"):
    return {
        "openapi": "3.0.0",
        "info": {"title": "Acme", "version": version, "contact": {"url": contact}},
        "paths": paths,
    }


def _op(parameters=None, responses=None, **extra):
    operation = {"responses": responses or {"200": {"description": "ok"}}}
    if parameters is not None:
        operation["parameters"] = parameters
    operation.update(extra)
    return operation


OPENAPI_V1 = _spec(
    {
        "/users": {
            "get": _op(
                parameters=[{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                responses={
                    "200": {"description": "ok"},
                    "404": {"description": "not found"},
                },
            ),
            "post": _op(responses={"201": {"description": "created"}}),
        },
        "/legacy": {"get": _op()},
    },
    version="1.0.0",
)

OPENAPI_V2 = _spec(
    {
        "/users": {
            "get": _op(
                parameters=[
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "org", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                responses={"200": {"description": "ok"}},
            ),
            "post": _op(responses={"201": {"description": "created"}}),
        },
        "/teams": {"get": _op()},
    },
    version="2.0.0",
)


class OpenApiChangesTests(unittest.TestCase):
    def _kinds(self, old, new):
        return [change["kind"] for change in parsers._openapi_changes(old, new)]

    def _one(self, old, new, kind):
        changes = [c for c in parsers._openapi_changes(old, new) if c["kind"] == kind]
        self.assertEqual(len(changes), 1, "expected exactly one %r in %r" % (kind, changes))
        return changes[0]

    def test_removed_path_is_critical(self):
        old = _spec({"/users": {"get": _op()}})
        new = _spec({})
        change = self._one(old, new, "removed path")
        self.assertEqual(change["severity"], "critical")
        self.assertEqual(change["path"], "/users")

    def test_added_path_is_info(self):
        old = _spec({})
        new = _spec({"/users": {"get": _op()}})
        change = self._one(old, new, "added path")
        self.assertEqual(change["severity"], "info")

    def test_removed_operation_is_critical(self):
        old = _spec({"/users": {"get": _op(), "delete": _op()}})
        new = _spec({"/users": {"get": _op()}})
        change = self._one(old, new, "removed operation")
        self.assertEqual(change["severity"], "critical")
        self.assertEqual(change["method"], "DELETE")

    def test_removed_required_parameter_is_critical(self):
        old = _spec({"/users": {"get": _op(parameters=[
            {"name": "org", "in": "query", "required": True, "schema": {"type": "string"}}
        ])}})
        new = _spec({"/users": {"get": _op(parameters=[])}})
        change = self._one(old, new, "removed required parameter")
        self.assertEqual(change["severity"], "critical")

    def test_removed_enum_value_is_critical(self):
        old = _spec({"/users": {"get": _op(parameters=[
            {"name": "state", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}}
        ])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "state", "in": "query", "schema": {"type": "string", "enum": ["a"]}}
        ])}})
        change = self._one(old, new, "removed enum value")
        self.assertEqual(change["severity"], "critical")

    def test_changed_parameter_location_is_critical(self):
        old = _spec({"/users": {"get": _op(parameters=[
            {"name": "token", "in": "query", "schema": {"type": "string"}}
        ])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "token", "in": "header", "schema": {"type": "string"}}
        ])}})
        change = self._one(old, new, "changed parameter location")
        self.assertEqual(change["severity"], "critical")

    def test_changed_response_status_code_set_is_critical(self):
        old = _spec({"/users": {"get": _op(responses={"200": {"description": "ok"},
                                                       "404": {"description": "nope"}})}})
        new = _spec({"/users": {"get": _op(responses={"200": {"description": "ok"}})}})
        change = self._one(old, new, "changed response status code set")
        self.assertEqual(change["severity"], "critical")

    def test_added_required_parameter_is_warning(self):
        old = _spec({"/users": {"get": _op(parameters=[])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "org", "in": "query", "required": True, "schema": {"type": "string"}}
        ])}})
        change = self._one(old, new, "added required parameter")
        self.assertEqual(change["severity"], "warning")

    def test_added_optional_parameter_is_info(self):
        old = _spec({"/users": {"get": _op(parameters=[])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "limit", "in": "query", "schema": {"type": "integer"}}
        ])}})
        change = self._one(old, new, "added optional parameter")
        self.assertEqual(change["severity"], "info")

    def test_added_enum_value_is_info(self):
        old = _spec({"/users": {"get": _op(parameters=[
            {"name": "state", "in": "query", "schema": {"type": "string", "enum": ["a"]}}
        ])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "state", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}}
        ])}})
        change = self._one(old, new, "added enum value")
        self.assertEqual(change["severity"], "info")

    def test_narrowed_enum_in_schema_is_warning(self):
        old = _spec({"/users": {"get": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "object", "properties": {
                "state": {"type": "string", "enum": ["a", "b"]}
            }}
        }}})}})
        new = _spec({"/users": {"get": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "object", "properties": {
                "state": {"type": "string", "enum": ["a"]}
            }}
        }}})}})
        change = self._one(old, new, "narrowed enum")
        self.assertEqual(change["severity"], "warning")

    def test_changed_parameter_type_is_warning(self):
        old = _spec({"/users": {"get": _op(parameters=[
            {"name": "limit", "in": "query", "schema": {"type": "integer"}}
        ])}})
        new = _spec({"/users": {"get": _op(parameters=[
            {"name": "limit", "in": "query", "schema": {"type": "string"}}
        ])}})
        change = self._one(old, new, "changed parameter type")
        self.assertEqual(change["severity"], "warning")

    def test_changed_request_schema_type_is_warning(self):
        old = _spec({"/users": {"post": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "object"}
        }}})}})
        new = _spec({"/users": {"post": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "string"}
        }}})}})
        change = self._one(old, new, "changed request schema type")
        self.assertEqual(change["severity"], "warning")

    def test_changed_response_schema_type_is_warning(self):
        old = _spec({"/users": {"get": _op(responses={"200": {"content": {"application/json": {
            "schema": {"type": "object"}
        }}}})}})
        new = _spec({"/users": {"get": _op(responses={"200": {"content": {"application/json": {
            "schema": {"type": "string"}
        }}}})}})
        change = self._one(old, new, "changed response schema type")
        self.assertEqual(change["severity"], "warning")

    def test_changed_required_property_is_warning(self):
        old = _spec({"/users": {"post": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string"}, "org": {"type": "string"}
            }}
        }}})}})
        new = _spec({"/users": {"post": _op(requestBody={"content": {"application/json": {
            "schema": {"type": "object", "required": ["name", "org"], "properties": {
                "name": {"type": "string"}, "org": {"type": "string"}
            }}
        }}})}})
        change = self._one(old, new, "changed required property")
        self.assertEqual(change["severity"], "warning")

    def test_added_response_status_code_is_info(self):
        old = _spec({"/users": {"get": _op(responses={"200": {"description": "ok"}})}})
        new = _spec({"/users": {"get": _op(responses={"200": {"description": "ok"},
                                                       "404": {"description": "nope"}})}})
        change = self._one(old, new, "added response status code")
        self.assertEqual(change["severity"], "info")

    def test_changed_description_is_info(self):
        old = _spec({"/users": {"get": _op(description="List users")}})
        new = _spec({"/users": {"get": _op(description="List all users")}})
        change = self._one(old, new, "changed description")
        self.assertEqual(change["severity"], "info")

    def test_noop_diff_is_empty(self):
        self.assertEqual(parsers._openapi_changes(OPENAPI_V1, OPENAPI_V1), [])

    def test_whitespace_only_description_change_is_ignored(self):
        old = _spec({"/users": {"get": _op(description="List   users")}})
        new = _spec({"/users": {"get": _op(description="List users")}})
        self.assertEqual(parsers._openapi_changes(old, new), [])

    def test_reordered_parameters_are_ignored(self):
        first = {"name": "a", "in": "query", "schema": {"type": "string"}}
        second = {"name": "b", "in": "query", "schema": {"type": "string"}}
        old = _spec({"/users": {"get": _op(parameters=[first, second])}})
        new = _spec({"/users": {"get": _op(parameters=[second, first])}})
        self.assertEqual(parsers._openapi_changes(old, new), [])


class OpenApiParserTests(unittest.TestCase):
    def test_two_versions_with_selector(self):
        text = json.dumps({"versions": [OPENAPI_V1, OPENAPI_V2]})
        entries = parsers.parse_openapi(
            text, base_url="https://api.acme.com/openapi.json", selector="versions"
        )
        self.assertEqual(
            [e.title for e in entries],
            [
                "/legacy: removed path",
                "/teams: added path",
                "GET /users: added required parameter",
                "GET /users: changed response status code set",
            ],
        )
        self.assertEqual(
            [e.severity for e in entries],
            ["critical", "info", "warning", "critical"],
        )
        for entry in entries:
            self.assertTrue(entry.title)
            self.assertTrue(entry.categories)
            self.assertIn("old:", entry.body)
            self.assertIn("new:", entry.body)

        removed = entries[0]
        self.assertIn("removed path", removed.categories)
        self.assertIn("/legacy", removed.categories)
        self.assertEqual(removed.link, "https://api.acme.com/support")
        self.assertEqual(removed.published, "2.0.0")

    def test_single_spec_without_selector_surfaces_baseline_as_info(self):
        entries = parsers.parse_openapi(json.dumps(OPENAPI_V1))
        self.assertEqual(
            [e.title for e in entries],
            ["/legacy: added path", "/users: added path"],
        )
        self.assertTrue(all(e.severity == "info" for e in entries))

    def test_dispatch(self):
        text = json.dumps({"versions": [OPENAPI_V1, OPENAPI_V2]})
        entries = parsers.parse("openapi", text, selector="versions")
        self.assertEqual(len(entries), 4)

    def test_malformed_returns_empty(self):
        self.assertEqual(parsers.parse_openapi("not json"), [])
        self.assertEqual(parsers.parse_openapi(""), [])
        self.assertEqual(parsers.parse_openapi("   "), [])
        self.assertEqual(parsers.parse_openapi('{"paths": }'), [])
        self.assertEqual(parsers.parse("openapi", "not json"), [])

    def test_json_string_wrapper_is_unwrapped(self):
        wrapped = json.dumps(json.dumps(OPENAPI_V1))
        entries = parsers.parse_openapi(wrapped)
        self.assertGreater(len(entries), 0)


if __name__ == "__main__":
    unittest.main()
