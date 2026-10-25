# Roadmap

## Worth doing

1. ~~**Tests.**~~ Done: pure logic, the handlers' flows, the email builders, the AI prompts and validation, and the ESPN/Kalshi/Odds API/fantasy fetchers against canned responses (`make test`, about 90% coverage). Still untested: `suggest_pickups` end to end, `fetch_league`/`secret`, and the Odds API/Kalshi error paths.
2. ~~**Pick'em failure alerts.**~~ Done: the Lambda publishes to `ComedTopic` itself when a run raises (no extra alarm, so the account stays at the free-tier limit). Async retries are off for the Lambda so one failure sends one email. A timeout or out-of-memory kill dies before the handler can publish, so each run also records a heartbeat in SSM and the `nfl-watchdog` Lambda (schedules a few hours after each expected run) emails if it is missing. An ESPN cookie rejection (HTTP 401/403) emails how to refresh the cookies, at most once a day, instead of failing every run.
3. ~~**Waiver and injury alerts.**~~ Done: `alert` run (Thu 6pm, Sun 6:15am/2pm/6pm and Mon 5:30pm CT, each only when a game kicks off within two hours, emails only if a start/bench change is needed) and `waivers` run (Tuesday 8am CT, emails only if there are pickups).
4. **Bye-week and playoff handling.** Verify the pick'em season-type gate and the lineup Lambda in the offseason and the fantasy playoffs (weeks 15-17), where different logic or silence may be wanted.

## Nice to have

5. ~~**Pick'em results tracking.**~~ Done: each sent pick sheet is saved in SSM (`/nfl-pickem/picks/<season>-w<week>`); the next Thursday/Sunday run scores last week once its games are final (`/nfl-pickem/results/<season>`) and the email opens with "Week 6: 11/14 correct, 87/105 points. Season ...". Once there are both AI and odds picks it also compares them (flagged per pick). A tie counts as wrong; a week waits (logged) until every picked game is final.
6. **Trade suggestions.** Extend the pickup logic to look at other teams' rosters in the 9-team league.
7. **README and cost notes.** Document the SSM params, the gotchas (deploy keeps previous parameter values, `AWS_BEARER_TOKEN_BEDROCK` in the shell) and the expected cost per week.
8. ~~**CI.**~~ Done, mirrored from TennisTracker: ruff, cfn-lint, pytest, pip-audit, actionlint/zizmor, Dependabot, PR-title check. Merges to main deploy through a `production` environment with a GitHub OIDC role (`deploy-role.yaml`); no dependency-review job (needs GitHub Advanced Security: add it back if the repo goes public); add one with GitHub OIDC if wanted.
9. ~~**Smarter alerts.**~~ Done: an alert saves a digest of each move it emails in SSM (`/nfl-pickem/alerts/<season>-w<week>`) and later alerts email only if a move is new. The saved list clears once the lineup needs no change at all, so a problem that comes back alerts again. SSM trouble fails open (the alert still goes out).
