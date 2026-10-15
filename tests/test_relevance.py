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

    def test_extra_keyword_promotes_and_matches_case_insensitively(self):
        entry = ParsedEntry(
            title="Acme will wind v1 down",
            body="This is the end of support for the old interface.",
        )
        base = analyze(entry, versions=["v1"])
        self.assertEqual(base.severity, "info")

        verdict = analyze(
            entry, versions=["v1"], extra_keywords=["  END OF SUPPORT  "]
        )
        self.assertTrue(verdict.relevant)
        self.assertEqual(verdict.severity, "critical")
        self.assertIn("end of support", verdict.matched_terms)

    def test_suppress_keyword_removes_builtin_match(self):
        entry = ParsedEntry(title="Sunset planned", body="Please prepare.")
        self.assertTrue(analyze(entry, versions=[]).relevant)

        verdict = analyze(entry, versions=[], suppress_keywords=["SUNSET"])
        self.assertFalse(verdict.relevant)
        self.assertEqual(verdict.matched_terms, [])

    def test_empty_overrides_match_legacy_behaviour(self):
        entry = ParsedEntry(title="v1 deprecated", body="Please migrate.")
        legacy = analyze(entry, versions=["v1"])
        with_empty = analyze(
            entry, versions=["v1"], extra_keywords=[], suppress_keywords=[]
        )
        self.assertEqual(legacy.severity, with_empty.severity)
        self.assertEqual(legacy.relevant, with_empty.relevant)
        self.assertEqual(legacy.matched_terms, with_empty.matched_terms)

    def test_suppress_beats_extra_for_same_term(self):
        entry = ParsedEntry(title="Widget is winding down", body="Nothing else.")
        verdict = analyze(
            entry,
            versions=[],
            extra_keywords=["winding down"],
            suppress_keywords=["winding down"],
        )
        self.assertFalse(verdict.relevant)


if __name__ == "__main__":
    unittest.main()
