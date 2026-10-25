# Roadmap

## Worth doing

1. ~~**Tests.**~~ Done for the pure logic (pick'em probabilities, Kalshi matching, confidence points, lineup slotting, validation, alert/waiver gating): `make test`. Still untested: the email builders, `app.handler`, `ai_picks`/`ai_lineup` prompts, and the live ESPN/Kalshi fetchers (add recorded-response fixtures).
2. **Pick'em failure alerts.** The fantasy Lambda has an error alarm; the pick'em Lambda does not because the account is at the 10-alarm free-tier limit. Free options: have the Lambda publish to `ComedTopic` itself when it catches an exception, use a metric-math alarm, or retire a less useful alarm.
3. ~~**Waiver and injury alerts.**~~ Done: `alert` run (Thu 5pm, Sun 6am/2pm/5pm and Mon 5:30pm CT, each only when a game kicks off within two hours, emails only if a start/bench change is needed) and `waivers` run (Tuesday 8am CT, emails only if there are pickups).
4. **Bye-week and playoff handling.** Verify the pick'em season-type gate and the lineup Lambda in the offseason and the fantasy playoffs (weeks 15-17), where different logic or silence may be wanted.

## Nice to have

5. **Pick'em results tracking.** Store each week's picks (SSM or DynamoDB), score them after the games, and include "last week: 11/14, 87 points" in the next email. Shows whether the Kalshi picks are working.
6. **Trade suggestions.** Extend the pickup logic to look at other teams' rosters in the 9-team league.
7. **README and cost notes.** Document the SSM params, the gotchas (deploy keeps previous parameter values, `AWS_BEARER_TOKEN_BEDROCK` in the shell) and the expected cost per week.
8. ~~**CI.**~~ Done, mirrored from TennisTracker: ruff, cfn-lint, pytest, pip-audit, actionlint/zizmor, dependency review, Dependabot, PR-title check. No deploy job (deploys are manual via `./deploy.sh`); add one with GitHub OIDC if wanted.
9. **Smarter alerts.** Alerts compare against your current ESPN lineup, so an ignored recommendation repeats at the next alert. Save the last recommendation (SSM) and email only when it changes.
