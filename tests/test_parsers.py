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

    def test_invalid_json(self):
        self.assertEqual(parsers.parse_json("{nope"), [])


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


if __name__ == "__main__":
    unittest.main()
