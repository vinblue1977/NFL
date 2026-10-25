# Working in this repo

## Pull requests

- Before starting new work, run `gh pr list`. If a PR is open, get it merged first (fix conflicts or failing checks, `gh pr update-branch <n>`, wait for the merge), then `git pull` main.
- Don't open a second PR while one is open, unless it's docs-only. If two are open, run `gh pr update-branch` on the other as soon as one merges, without waiting to be told.
- `git pull` main before branching. Use a conventional title (`feat:`, `fix:`, `ci:`, `docs:`).
- Run `make check` before pushing.
- **Code review is required for every change except docs-only ones (`*.md`).** Run `/code-review` on the branch before opening the PR, fix what it finds (or say why not), and note in the PR description that the review was done.
- This repo is private on a free GitHub plan, so there is no branch protection and `gh pr merge --auto` isn't available. Wait for the CI checks to pass, then merge with `gh pr merge --squash --delete-branch`.
- After a PR merges, delete its branches: `git fetch --prune`, then `git branch -D` every local branch marked `gone` (and any remote branch of a merged PR that GitHub left behind). Never delete a branch with an open PR.

## Deploying

- The stack is deployed by hand: `env -u AWS_BEARER_TOKEN_BEDROCK ./deploy.sh [Param=value ...]` (needs `aws login`). Deploy after the PR merges, from an up-to-date main.
- `aws cloudformation deploy` keeps previous parameter values, so changing a template `Default` does nothing: pass the parameter explicitly.
