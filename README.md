# AI Pipes — empty starting point

This branch contains the autonomous development pipeline and an empty
`CONCEPT.md`, with no product or product tests. Use it to improve the empty
starting point or create new, independent projects.

The first generated issue proposes a project from scratch. Its implementation
fills `CONCEPT.md` and creates product files and tests. The pipeline documentation lives
in [`.github/README.md`](.github/README.md), including the required GitHub secrets
and repository settings.

## Start again in this repository

Merge this starting point into `main` when you are ready to start a new project.
The push to `main` generates the first issue and starts the development cycle.
Working on `empty-repo` does not start issue generation; the generator always
uses `main`, even when started manually.

Existing issues, pull requests, branches, and history remain available. Avoid a
commit message beginning with `chore(reset):`: that explicitly triggers cleanup
of previous remote work when pushed to `main`.

## Create an independent project

Export this branch without its Git history:

```sh
mkdir ../my-new-project
git archive empty-repo | tar -x -C ../my-new-project
cd ../my-new-project
git init -b main
git add .
git commit -m "chore: initialize empty development pipeline"
```

Create a GitHub repository, configure the secrets and settings documented in
`.github/README.md`, then add its remote and push `main`. Each new repository
has its own project and development cycle.

## Verify the pipeline locally

```sh
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```

CI runs these pipeline tests on every push and pull request, and also runs
product tests from `tests/` once that directory exists.
