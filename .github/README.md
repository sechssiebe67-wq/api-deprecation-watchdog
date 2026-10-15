# Autonomous development cycle

Issue → create/extend CONCEPT.md → implement → PR → review → improve → review
(repeat until approved) → squash merge → next issue.

The product starts empty. The generator receives only current product files,
not this automation, the repository name, old issues, PRs, or git history.
The first issue proposes a project; its implementer writes CONCEPT.md before
implementation. Every subsequent issue includes a concept update. The concept
and implementation enter main together through the reviewed PR.

## Starting and continuing

Push this setup to main, run **AI Issue on Merge** manually, or create an issue.
An issue starts work when it is open, has at least one assignee (anyone), and has
`scope-override`. Adding the assignee or label later also starts work. Generated
issues always have both; `AI_ASSIGNEE` selects the assignee and defaults to the
repository owner (set it to an eligible user for organization repositories).

The implementer can change every file, including workflows and the whole concept.
If implementation produces no changes, README is updated and a PR is still opened.
The reviewer runs for every pushed revision of an `ai-fix/issue-*` PR. Review fixes
have no iteration cap. No-change fixes also produce a README update and re-review;
only an explicit APPROVED verdict allows merging. Each main push, including a
concept-only merge, produces one new issue. Retries of the same push are deduplicated.
Manual generator runs intentionally create a new cycle.

## Credentials and GitHub configuration

- `TAMBINOS_PAT`: implementation, issue creation, review fallback and merge. Needs
  repository contents, issues, pull requests and workflow modification access.
  A PAT is used so its issues, pushes and reviews trigger the next workflow.
- `DEEPSEEK_API_KEY`: implementation and review.
- `GURKE_DEEPSEEK_API_KEY`: optional issue-generation key; falls back to the above.
- `REVIEWER_PAT`: optional separate reviewer account with pull request review
  access. This creates actual GitHub approvals / changes-requested reviews.
  Without it, the author account records the AI verdict as a comment review,
  because GitHub forbids self-approval. That does not satisfy a branch rule
  requiring an independent approval.
- Enable squash merging, and auto-merge when required checks must finish first.
  Branch protection and required checks remain authoritative; no admin bypass.

A merge is restricted to the reviewed head commit. Stale reviews are discarded,
and the resolver accepts only reviews from the configured review account.
API failures, failed checks, merge conflicts or missing permissions can interrupt
execution and require repair/retry. The loop has no intentional stop or budget cap.
The empty starting point preserves existing remote issues, branches, PRs and
git history. The explicit reset operation described below is separate.

## Local verification

Run `python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v` for
offline generator tests, and `actionlint` to validate the workflow definitions.

A main commit starting with `chore(reset):` explicitly cancels older active runs,
closes open issues/PRs and deletes all branches except main before generating a
new issue. Push a product-empty tree for this reset. Ordinary merges never reset.

The AI Cycle Watchdog retries failed, timed-out or cancelled review/resolver runs
for the current revision of an open AI PR. It runs after failures and every five
minutes as a fallback, with no retry limit. Closed PRs, stale revisions and a
newer successful or running review are ignored. The PAT needs Actions write access.
