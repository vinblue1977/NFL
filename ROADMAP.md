# Roadmap

## Worth doing

1. ~~**Tests.**~~ Done: pure logic, the handlers' flows, the email builders, the AI prompts and validation, and the ESPN/Kalshi/Odds API/fantasy fetchers against canned responses (`make test`, about 90% coverage). Still untested: `suggest_pickups` end to end, `fetch_league`/`secret`, and the Odds API/Kalshi error paths.
2. ~~**Pick'em failure alerts.**~~ Done: the Lambda publishes to `ComedTopic` itself when a run raises (no extra alarm, so the account stays at the free-tier limit). Async retries are off for the Lambda so one failure sends one email. Not covered: a timeout or out-of-memory kill, which dies before the handler can publish.
3. ~~**Waiver and injury alerts.**~~ Done: `alert` run (Thu 5pm, Sun 6am/2pm/5pm and Mon 5:30pm CT, each only when a game kicks off within two hours, emails only if a start/bench change is needed) and `waivers` run (Tuesday 8am CT, emails only if there are pickups).
4. **Bye-week and playoff handling.** Verify the pick'em season-type gate and the lineup Lambda in the offseason and the fantasy playoffs (weeks 15-17), where different logic or silence may be wanted.

## Nice to have

5. **Pick'em results tracking.** Store each week's picks (SSM or DynamoDB), score them after the games, and include "last week: 11/14, 87 points" in the next email. Shows whether the Kalshi picks are working.
6. **Trade suggestions.** Extend the pickup logic to look at other teams' rosters in the 9-team league.
7. **README and cost notes.** Document the SSM params, the gotchas (deploy keeps previous parameter values, `AWS_BEARER_TOKEN_BEDROCK` in the shell) and the expected cost per week.
8. ~~**CI.**~~ Done, mirrored from TennisTracker: ruff, cfn-lint, pytest, pip-audit, actionlint/zizmor, Dependabot, PR-title check. Merges to main deploy through a `production` environment with a GitHub OIDC role (`deploy-role.yaml`); no dependency-review job (needs GitHub Advanced Security: add it back if the repo goes public); add one with GitHub OIDC if wanted.
9. ~~**Smarter alerts.**~~ Done: an alert saves a hash of its moves in SSM (`/nfl-pickem/alerts/<season>-w<week>`) and the next alert emails only if the moves changed. A clean lineup clears it, so a problem that comes back alerts again.
