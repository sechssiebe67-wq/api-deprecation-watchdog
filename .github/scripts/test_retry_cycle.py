import copy
import unittest
from retry_cycle import retry_candidates


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"state": "open", "draft": False,
                   "head": {"ref": "ai-fix/issue-1", "sha": "current", "repo": {"full_name": "o/r"}},
                   "base": {"repo": {"full_name": "o/r"}}}
        self.failed = {"id": 1, "name": "AI PR Reviewer", "head_sha": "current",
                       "status": "completed", "conclusion": "failure"}

    def test_failed_current_review_is_retried(self):
        self.assertEqual(retry_candidates(self.pr, [self.failed]), [1])

    def test_closed_pr_or_stale_revision_is_not_retried(self):
        closed = copy.deepcopy(self.pr); closed["state"] = "closed"
        self.assertEqual(retry_candidates(closed, [self.failed]), [])
        stale = dict(self.failed, head_sha="old")
        self.assertEqual(retry_candidates(self.pr, [stale]), [])

    def test_running_or_successful_newer_review_suppresses_old_failure(self):
        for status, conclusion in [("in_progress", None), ("completed", "success")]:
            newer = dict(self.failed, id=2, status=status, conclusion=conclusion)
            self.assertEqual(retry_candidates(self.pr, [self.failed, newer]), [])

    def test_failed_resolver_is_retried_too(self):
        resolver = dict(self.failed, id=2, name="Review Resolver")
        self.assertEqual(retry_candidates(self.pr, [resolver]), [2])
