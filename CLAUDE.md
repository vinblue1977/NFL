# Working in this repo

## Pull requests

- Before starting new work, run `gh pr list`. If a PR is open, get it merged first (fix conflicts or failing checks, `gh pr update-branch <n>`, wait for the merge), then `git pull` main.
- Don't open a second PR while one is open, unless it's docs-only. If two are open, run `gh pr update-branch` on the other as soon as one merges, without waiting to be told.
- `git pull` main before branching. Use a conventional title (`feat:`, `fix:`, `ci:`, `docs:`).
- Run `make check` before pushing.
- **Code review is required for every change except docs-only ones (`*.md`).** Run `/code-review` on the branch before opening the PR, fix what it finds (or say why not), and note in the PR description that the review was done.
- The repo is public, so `main` is protected: the `python`, `workflow-lint` and `conventional-title` checks must pass before a merge (admins aren't forced to, so don't push straight to main anyway). Once the review is done, merge with `gh pr merge --auto --squash`; it waits for the checks and merges by itself (GitHub deletes the remote branch).
- After a PR merges, delete its branches: `git fetch --prune`, then `git branch -D` every local branch marked `gone`. Never delete a branch with an open PR.

## Deploying

- A merge to main deploys automatically: the `deploy` job in `ci.yml` runs `./deploy.sh` after the checks pass, in the `production` environment (main only) using GitHub OIDC. Every push to main that changes more than `*.md` deploys (an unchanged package is a no-op; a docs-only push starts no CI run at all; the deploy step skips itself, with a warning annotation, if its commit is no longer the head of main and non-docs files changed since, so a forced run from an old commit does nothing), so a failed deploy is retried by the next code push; `gh workflow run ci.yml --ref main` forces one.
- The deploy role is a separate stack, `nfl-pickem-deploy-role` (`deploy-role.yaml`), deployed once by hand (the command is in the file). Its ARN is the `AWS_DEPLOY_ROLE_ARN` secret in the `production` environment. Change it only with admin credentials (`aws login`).
- Manual fallback: `env -u AWS_BEARER_TOKEN_BEDROCK ./deploy.sh [Param=value ...]`.
- `aws cloudformation deploy` keeps previous parameter values, so changing a template `Default` does nothing: pass the parameter explicitly (manual deploys), or set it in the stack once by hand.
- Don't rename a resource's logical ID while keeping its fixed `Name:` (schedules, functions): CloudFormation creates the new one before deleting the old and fails on the name conflict.
