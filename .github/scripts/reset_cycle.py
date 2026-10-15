"""Explicit reset commits close previous work before starting an empty cycle."""

import json
import os
import subprocess
import time
from urllib.parse import quote


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True).strip()


def pages(path):
    return [item for page in json.loads(gh("api", "--paginate", "--slurp", path)) for item in page]


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    own_run = int(os.environ["GITHUB_RUN_ID"])
    prefix = f"repos/{repo}"
    active = [run for run in json.loads(gh("api", f"{prefix}/actions/runs?per_page=100"))["workflow_runs"]
              if run["id"] != own_run and run["status"] != "completed"]
    for run in active:
        gh("api", f"{prefix}/actions/runs/{run['id']}/cancel", "--method", "POST")
    # Wait for old writers to stop before deleting their branches.
    deadline = time.monotonic() + 120
    for run in active:
        while json.loads(gh("api", f"{prefix}/actions/runs/{run['id']}"))["status"] != "completed":
            if time.monotonic() > deadline:
                raise RuntimeError("Old workflow writers did not stop; reset aborted")
            time.sleep(3)
    for pr in pages(f"{prefix}/pulls?state=open&per_page=100"):
        gh("api", f"{prefix}/pulls/{pr['number']}", "--method", "PATCH", "-f", "state=closed")
        print(f"Closed PR #{pr['number']}")
    for issue in pages(f"{prefix}/issues?state=open&per_page=100"):
        if "pull_request" not in issue:
            gh("api", f"{prefix}/issues/{issue['number']}", "--method", "PATCH",
               "-f", "state=closed", "-f", "state_reason=not_planned")
            print(f"Closed issue #{issue['number']}")
    for branch in pages(f"{prefix}/branches?per_page=100"):
        if branch["name"] != "main":
            gh("api", f"{prefix}/git/refs/heads/{quote(branch['name'], safe='')}", "--method", "DELETE")
            print(f"Deleted branch {branch['name']}")
    print("Reset complete: previous work closed, only main remains.")


if __name__ == "__main__":
    main()
