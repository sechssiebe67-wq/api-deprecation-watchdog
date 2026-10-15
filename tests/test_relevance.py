import unittest

from apiwatch.parsers import ParsedEntry
from apiwatch.relevance import analyze, max_severity


class RelevanceTests(unittest.TestCase):
    def test_critical_on_breaking(self):
        entry = ParsedEntry(title="Breaking change in v2", body="The endpoint will be removed.")
        verdict = analyze(entry, versions=["v2"])
        self.assertTrue(verdict.relevant)
        self.assertEqual(verdict.severity, "critical")
        self.assertIn("v2", verdict.version_hits)

    def test_warning_on_deprecated(self):
        entry = ParsedEntry(title="v1 deprecated", body="Please migrate to v2.")
        verdict = analyze(entry, versions=["v1"])
        self.assertTrue(verdict.relevant)
        self.assertEqual(verdict.severity, "warning")

    def test_info_on_release(self):
        entry = ParsedEntry(title="New release", body="Version 3.1 is available.")
        verdict = analyze(entry, versions=["v3"])
        self.assertTrue(verdict.relevant)
        self.assertEqual(verdict.severity, "info")

    def test_irrelevant_entry(self):
        entry = ParsedEntry(title="Company picnic", body="Join us on Friday.")
        verdict = analyze(entry, versions=["v1"])
        self.assertFalse(verdict.relevant)

    def test_require_version_filters_other_versions(self):
        entry = ParsedEntry(title="Deprecated v9", body="v9 is deprecated.")
        verdict = analyze(entry, versions=["v1"], require_version=True)
        self.assertFalse(verdict.relevant)

    def test_word_boundaries(self):
        entry = ParsedEntry(title="Undeprecated feature", body="No change here.")
        verdict = analyze(entry, versions=[])
        self.assertEqual(verdict.matched_terms, [])

    def test_max_severity(self):
        self.assertEqual(max_severity(["info", "critical", "warning"]), "critical")
        self.assertEqual(max_severity([]), "info")


if __name__ == "__main__":
    unittest.main()
