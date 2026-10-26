# NFL Pick'em emailer

Emails NFL pick'em picks and fantasy football advice during the regular season, from three Lambdas in one
CloudFormation stack (`nfl-pickem`, us-east-2):

| Lambda | What it does |
| --- | --- |
| `nfl-pickem` | Pick'em picks with confidence points (1..N, each used once per week) |
| `nfl-fantasy-lineup` | Start/sit lineup, late alerts, waiver pickups and the weekly recap for my ESPN fantasy team |
| `nfl-watchdog` | Emails if one of the other two didn't finish a run on time |

Preseason and postseason runs exit without sending (based on ESPN's season type).

## Schedule

All times Central. Every schedule is a template parameter, so `./deploy.sh ThursdaySchedule='cron(...)'` changes one.

| When | Lambda / run | What it sends |
| --- | --- | --- |
| Thu 08:00 | pick'em `thursday` | The Thursday game's pick; its points are saved so Sunday uses the leftovers |
| Sun 06:00 | pick'em `sunday` | Every game that hasn't started, with the leftover points |
| Thu 08:00 | lineup `thursday` | Decisions for the Thursday game's players, the rest is a preview |
| Sat 09:00 | lineup `saturday` | Full preview after Friday's injury reports |
| Sun 10:45 | lineup `sunday` | Final lineup after the noon inactives |
| Thu 18:00, Sun 06:15 / 14:00 / 18:00, Mon 17:30 | lineup `alert` | Only if a game kicks off within 2 hours and a start/bench change is needed that no earlier alert already reported |
| Tue 07:30 (retry Wed 07:30) | lineup `recap` | How last week went: points vs ESPN's projections, best lineup in hindsight, matchup result. Waits until every game is final; sent once per week |
| Tue 08:00 | lineup `waivers` | Free-agent pickups ahead of the Wednesday waiver run, only if there are any |
| Thu, Sun 10:00 | watchdog | Checks the pick'em heartbeat |
| Tue, Thu, Sat, Sun 12:30 | watchdog | Checks the lineup heartbeat |

## Pick'em

Schedule, injuries, transactions and news come from ESPN's public API. Win probabilities come from
[Kalshi](https://kalshi.com)'s public NFL game-winner markets (price midpoint, no key needed). Games Kalshi
doesn't price fall back to [The Odds API](https://the-odds-api.com) consensus (no-vig moneyline averaged across
US sportsbooks) if a key is set, then to ESPN's line. The email notes any fallback.

With `UseAI=true`, Claude on Amazon Bedrock makes the picks; otherwise, or if the AI call fails, it picks the
likelier winner and gives more points to bigger favorites. The default is `UseAI=false`.

Each email opens with last week's record ("Week 6: 11/14 correct, 87/105 points. Season ..."), scored from the
saved picks once the games are final. Once there are both AI and odds picks on record it compares them. A tie
counts as wrong.

## Fantasy lineup

For my ESPN fantasy team (league `410405345`, param `LeagueId`). It reads my roster, the league's lineup slots
and ESPN's weekly projections (scored with the league's rules), benches anyone out/doubtful/IR/on bye, and fills
the slots with the highest projections. With `LineupUseAI=true` (the default), Claude reviews close calls (bench
within 3 points of a starter, or an injury-tagged starter); an invalid or failed AI answer falls back to the
projection lineup. The email lists the moves vs. the current ESPN lineup.

Every non-alert lineup email also suggests up to 3 free-agent pickups: the league's free agents and waiver
players ranked by ESPN's rest-of-season projection, checked against the last two weeks of Rotowire news. With
`LineupUseAI=true` Claude picks the moves; otherwise it suggests same-position swaps gaining at least 15
projected rest-of-season points.

## SSM parameters

Everything the Lambdas keep lives in Systems Manager Parameter Store (`/nfl-pickem/...`, standard tier, free).

| Parameter | Type | Who writes it | What it holds |
| --- | --- | --- | --- |
| `/nfl-pickem/espn-s2`, `/nfl-pickem/espn-swid` | SecureString | **you**, once | ESPN cookies for the private league |
| `/nfl-pickem/odds-api-key` | SecureString | **you**, optional | The Odds API key (fallback odds) |
| `/nfl-pickem/locked/<season>-w<week>` | String | pick'em Thursday run | Thursday's picks and points, so Sunday uses the leftovers |
| `/nfl-pickem/picks/<season>-w<week>` | String | pick'em runs | Every emailed pick, for scoring |
| `/nfl-pickem/results/<season>` | String | pick'em runs | The scored weeks (correct, points, AI vs odds) |
| `/nfl-pickem/alerts/<season>-w<week>` | String | lineup alert runs | Digests of the moves already alerted this week |
| `/nfl-pickem/recap/<season>` | String | lineup recap run | Weeks already recapped |
| `/nfl-pickem/heartbeat/<function>` | String | pick'em and lineup runs | When a run last finished (the watchdog reads it) |
| `/nfl-pickem/cookie-alert` | String | lineup runs | When the "ESPN cookies expired" email last went out |

The per-week records (`locked`, `picks`, `alerts`) older than last week are deleted by the Thursday pick'em run.
Results, recap, heartbeat and cookie parameters are kept.

One-time setup:

```bash
# Private league: copy the espn_s2 and SWID cookies from your browser while logged in to fantasy.espn.com
# (DevTools > Application > Cookies). Keep the braces in SWID.
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/espn-s2 --type SecureString --value '<espn_s2>' --overwrite
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/espn-swid --type SecureString --value '{<SWID>}' --overwrite

# Optional: The Odds API key (each call uses 2 credits of the free plan's monthly quota)
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/odds-api-key --type SecureString --value '<your key>' --overwrite
```

The cookies last about a year. When ESPN rejects them the lineup Lambda emails these same steps (at most once a day).

## When something breaks

Failure emails are plain text through the existing `ComedTopic` SNS topic (it lives outside this stack and must
exist), so they don't depend on SES:

- **A run raised.** The pick'em handler publishes the error itself; the lineup Lambda has the `nfl-fantasy-lineup-errors` CloudWatch alarm. Async retries are off for pick'em, so one failure is one email.
- **A run never finished** (timeout, out-of-memory, never started, schedule disabled). Each completed run writes a heartbeat; `nfl-watchdog` checks it a few hours after each expected run and emails if it is missing. A manual `test` run doesn't write a heartbeat.
- **ESPN cookies expired.** One email with the fix, at most once a day. While it is suppressed no heartbeat is written, so the watchdog keeps reminding you.

The account is at the 10-alarm CloudWatch free-tier limit, which is why only the lineup Lambda has an alarm.

## Cost

Roughly **$0 a month** while AI is off, and a few dollars a month in season with the defaults. These are estimates
from per-call figures, not from a bill:

- **Lambda, SES, SSM, EventBridge Scheduler, CloudWatch logs:** inside the free tiers (about 15 short runs a week).
- **Pick'em:** $0 with `UseAI=false` (the default). With `UseAI=true`, one Opus 4.6 call per run, about $0.15-0.30, so $0.30-0.60 a week.
- **Lineup, `LineupUseAI=true` (the default):** one pickup call per non-alert run (about $0.15-0.25, four runs a week) plus a close-call review when there is one (about $0.15-0.30). Roughly $1-2 a week, or $20-35 over an 18-week season. Set `LineupUseAI=false` to drop it to $0.
- **Alerts and the watchdog:** no AI unless an alert actually needs a lineup change reviewed.
- **Kalshi, ESPN, The Odds API (free plan):** $0.

To measure it, look at Bedrock in Cost Explorer or the `usage:` lines the Lambdas log.

## Deploy

```bash
./deploy.sh                          # defaults
./deploy.sh UseAI=false              # free mode
./deploy.sh UseAI=false LineupUseAI=true
```

A merge to `main` that changes more than `*.md` deploys itself (GitHub Actions, OIDC, `production` environment);
docs-only pushes start no run. See `CLAUDE.md` for the PR workflow and the deploy-role setup.

- First deploy: click the SES verification link AWS emails to the sender address. Emails can't be sent until you do.
- AI mode uses Claude Opus 4.6 through bedrock-runtime (`us.anthropic.claude-opus-4-6-v1`), which needs its model agreement in us-east-2.
- `ModelId=us.amazon.nova-pro-v1:0` switches both Lambdas to Amazon Nova Pro through the Converse API (about $0.80/$3.20 per million input/output tokens, versus Opus's much higher price). Any non-Anthropic ID takes that path. Effort and thinking are Claude-only and ignored for Nova. Set it once by hand with `env -u AWS_BEARER_TOKEN_BEDROCK ./deploy.sh ModelId=us.amazon.nova-pro-v1:0`; CloudFormation keeps the value on later auto-deploys.
- `aws cloudformation deploy` keeps previous parameter values, so changing a template `Default` does nothing: pass the parameter explicitly.
- `AWS_BEARER_TOKEN_BEDROCK` in the shell makes the AWS CLI use a limited Bedrock-only IAM user. For manual deploys and admin commands, run them as `env -u AWS_BEARER_TOKEN_BEDROCK ./deploy.sh ...`.

## Test

```bash
aws lambda invoke --region us-east-2 --function-name nfl-pickem \
  --cli-binary-format raw-in-base64-out --payload '{"run":"sunday","test":true}' /dev/stdout
```

```bash
aws lambda invoke --region us-east-2 --function-name nfl-fantasy-lineup \
  --cli-binary-format raw-in-base64-out --payload '{"run":"saturday","test":true}' /dev/stdout
```

The lineup runs are `thursday`, `saturday`, `sunday`, `alert`, `waivers` and `recap`.
`"test": true` adds `[TEST]` to the subject, bypasses the "is there a game soon / is the week over" checks, and
doesn't save the Thursday lock, the picks, the alert digests, the recap record or a heartbeat.

Locally: `make install` once, then `make check` runs everything CI does (ruff, cfn-lint, pytest, pip-audit).
