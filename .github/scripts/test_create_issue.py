"""Offline checks for blank bootstrap, current context and issue idempotency."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("create_issue", Path(__file__).with_name("create_issue.py"))
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, previous)
        subprocess.run(["git", "init", "-q"], check=True)
        self.env = patch.dict(os.environ, {
            "CYCLE_KEY": "revision-123", "GITHUB_REPOSITORY": "owner/repo",
            "ASSIGNEE": "any-user", "RUNNER_TEMP": self.temp.name,
            "DEEPSEEK_API_KEY": "test-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def track(self, name, text):
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(["git", "add", name], check=True)

    def test_blank_start_excludes_infrastructure_and_bootstrap_readme(self):
        self.track(".github/workflows/work.yml", "old automation context")
        self.track("README.md", "bootstrap placeholder")
        self.assertEqual(cycle.current_product(), "No product or concept exists yet.")

    def test_later_cycle_includes_concept_code_and_readme(self):
        self.track("CONCEPT.md", "Project goal")
        self.track("src/app.py", "print('working product')")
        self.track("README.md", "Current usage")
        self.track(".github/README.md", "Infrastructure only")
        context = cycle.current_product()
        self.assertIn("Project goal", context)
        self.assertIn("working product", context)
        self.assertIn("Current usage", context)
        self.assertNotIn("Infrastructure only", context)

    def test_empty_concept_preserves_blank_start(self):
        self.track("README.md", "bootstrap placeholder")
        self.track(".github/README.md", "Infrastructure only")
        for content in ("", " \n\t"):
            with self.subTest(content=content):
                self.track("CONCEPT.md", content)
                self.assertEqual(cycle.current_product(), "No product or concept exists yet.")

    def test_repeated_cycle_does_not_call_ai_or_create_another_issue(self):
        pages = [[{"body": "<!-- ai-cycle:revision-123 -->"}]]
        with patch.object(cycle, "gh", return_value=json.dumps(pages)) as gh, \
             patch.object(cycle, "urlopen") as api:
            cycle.main()
        api.assert_not_called()
        self.assertEqual(gh.call_count, 1)

    def generate(self, result):
        response = io.BytesIO(json.dumps({"choices": [{"message": {
            "content": json.dumps(result)}}]}).encode())
        calls = []

        def github(*args):
            calls.append(args)
            return "[[]]" if args[0] == "api" else "https://github.com/owner/repo/issues/1"

        with patch.object(cycle, "gh", side_effect=github), \
             patch.object(cycle, "urlopen", return_value=response):
            cycle.main()
        return calls

    def test_created_issue_is_assigned_labeled_and_marked(self):
        calls = self.generate({"title": "feat: start project", "body":
                               "# Outcome\nUseful project\n# Tasks\nCreate concept\n# Acceptance criteria\nIt runs"})
        creation = next(call for call in calls if call[:2] == ("issue", "create"))
        self.assertEqual(creation[creation.index("--assignee") + 1], "any-user")
        self.assertEqual(creation[creation.index("--label") + 1], "scope-override")
        body = Path(creation[creation.index("--body-file") + 1]).read_text()
        self.assertIn("<!-- ai-cycle:revision-123 -->", body)
        self.assertTrue(any(call[:2] == ("label", "create") for call in calls))
        self.assertFalse(Path("CONCEPT.md").exists(), "Concept belongs in the implementation PR")

    def test_invalid_ai_output_fails_before_issue_creation(self):
        with self.assertRaisesRegex(ValueError, "Missing issue heading"):
            self.generate({"title": "feat: start", "body": "Incomplete response"})


if __name__ == "__main__":
    unittest.main()
