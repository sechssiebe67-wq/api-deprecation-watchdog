"""Retry failed automation while rejecting closed PRs and stale revisions."""

import json
import os
import subprocess
from urllib.parse import urlencode


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True).strip()


def retry_candidates(pr, runs):
    if pr["state"] != "open" or pr.get("draft"):
        return []
    if not pr["head"]["ref"].startswith("ai-fix/issue-"):
        return []
    if pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        return []
    candidates = []
    for name in ("AI PR Reviewer", "Review Resolver"):
        relevant = [run for run in runs if run["name"] == name
                    and run["head_sha"] == pr["head"]["sha"]]
        if not relevant:
            continue
        latest = max(relevant, key=lambda run: run["id"])
        if latest["status"] == "completed" and latest["conclusion"] in {
            "failure", "timed_out", "cancelled"
        }:
            candidates.append(latest["id"])
    return candidates


def main():
    prefix = f"repos/{os.environ['GITHUB_REPOSITORY']}"
    pages = json.loads(gh("api", "--paginate", "--slurp", f"{prefix}/pulls?state=open&per_page=100"))
    for pr in (pr for page in pages for pr in page):
        if not pr["head"]["ref"].startswith("ai-fix/issue-"):
            continue
        query = urlencode({"branch": pr["head"]["ref"], "per_page": 100})
        runs = json.loads(gh("api", f"{prefix}/actions/runs?{query}"))["workflow_runs"]
        for run_id in retry_candidates(pr, runs):
            # Re-check immediately before retrying; the PR may have merged meanwhile.
            current = json.loads(gh("api", f"{prefix}/pulls/{pr['number']}"))
            if current["state"] != "open" or current["head"]["sha"] != pr["head"]["sha"]:
                break
            latest = json.loads(gh("api", f"{prefix}/actions/runs/{run_id}"))
            if latest["status"] != "completed":
                continue
            endpoint = "rerun" if latest["conclusion"] == "cancelled" else "rerun-failed-jobs"
            gh("api", f"{prefix}/actions/runs/{run_id}/{endpoint}", "--method", "POST")
            print(f"Retried automation run {run_id} for PR #{pr['number']}")


if __name__ == "__main__":
    main()
