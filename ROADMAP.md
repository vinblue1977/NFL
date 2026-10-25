# Roadmap

## Worth doing

1. **Tests.** Add a pytest suite for the parts that can break quietly: confidence-point ranking, the Kalshi ticker mapping (ESPN WSH/JAX vs Kalshi WAS/JAC), ESPN season-stats filtering (`seasonId`), and the fallback paths. A bad ESPN or Kalshi response currently produces a wrong email, not an error.
2. **Pick'em failure alerts.** The fantasy Lambda has an error alarm; the pick'em Lambda does not because the account is at the 10-alarm free-tier limit. Free options: have the Lambda publish to `ComedTopic` itself when it catches an exception, use a metric-math alarm, or retire a less useful alarm.
3. ~~**Waiver and injury alerts.**~~ Done: `alert` run (Sunday 6:00am (morning games), Sunday 2:00pm (late window) and Monday 5:30pm CT, emails only if a start/bench change is needed) and `waivers` run (Tuesday 8am CT, emails only if there are pickups).
4. **Bye-week and playoff handling.** Verify the pick'em season-type gate and the lineup Lambda in the offseason and the fantasy playoffs (weeks 15-17), where different logic or silence may be wanted.

## Nice to have

5. **Pick'em results tracking.** Store each week's picks (SSM or DynamoDB), score them after the games, and include "last week: 11/14, 87 points" in the next email. Shows whether the Kalshi picks are working.
6. **Trade suggestions.** Extend the pickup logic to look at other teams' rosters in the 9-team league.
7. **README and cost notes.** Document the SSM params, the gotchas (deploy keeps previous parameter values, `AWS_BEARER_TOKEN_BEDROCK` in the shell) and the expected cost per week.
8. **CI.** A GitHub Action running pytest and `cfn-lint` on pushes to main.
