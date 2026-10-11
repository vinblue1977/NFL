# NFL Pick'em emailer

Emails pick'em picks with confidence points (1..N, each used once per week), during the regular season only.
Preseason and playoff runs exit without sending (based on ESPN's season type):

- **Thursday 08:45 CT**: Thursday game only. Its points are saved in SSM (`/nfl-pickem/locked/<season>-w<week>`).
- **Sunday 06:00 CT**: every game that hasn't started, using the leftover points.

Schedule, injuries, transactions and news come from ESPN's public API. Win probabilities come from
[Kalshi](https://kalshi.com)'s public NFL game-winner markets (price midpoint, no key needed). Games
Kalshi doesn't price fall back to [The Odds API](https://the-odds-api.com) consensus (no-vig moneyline
averaged across US sportsbooks) if a key is set, then to ESPN's line. The email notes any fallback.
With `UseAI=true`, Claude on Amazon Bedrock makes the picks; otherwise, or if the AI call fails,
it picks the likelier winner and gives more points to bigger favorites.

Optional Odds API key (each call uses 2 credits of the free plan's monthly quota):

```bash
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/odds-api-key \
  --type SecureString --value '<your key>' --overwrite
```

## Fantasy start/sit lineup

A second function (`nfl-fantasy-lineup`) emails start/sit moves for my ESPN fantasy team
(league `410405345`, param `LeagueId`):

- **Thursday 08:45 CT**: decisions for players in the Thursday game (rest is a preview).
- **Saturday 09:00 CT**: full preview after Friday injury reports.
- **Sunday 10:45 CT**: final lineup after noon-game inactives (London-game players are already locked by then).

It reads my roster, the league's lineup slots and ESPN's weekly projections (scored with the league's
rules) from ESPN's fantasy API, benches anyone out/doubtful/IR/on bye, and fills the slots with the
highest projections. With `LineupUseAI=true`, Claude reviews close calls (bench within 3 points of a
starter, or an injury-tagged starter) and explains them; an invalid or failed AI answer falls back to
the projection lineup. The email lists the moves vs. the current ESPN lineup.

One-time setup (private league): copy the `espn_s2` and `SWID` cookies from your browser while
logged in to fantasy.espn.com (DevTools > Application > Cookies), then:

```bash
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/espn-s2 --type SecureString --value '<espn_s2>' --overwrite
aws ssm put-parameter --region us-east-2 --name /nfl-pickem/espn-swid --type SecureString --value '{<SWID>}' --overwrite
```

Keep the braces in SWID. The cookies last about a year; re-run these if ESPN logs you out.

## Deploy

```bash
./deploy.sh                          # defaults
./deploy.sh UseAI=false              # free mode
./deploy.sh UseAI=false LineupUseAI=true
```

First deploy: click the SES verification link AWS emails to the sender address. Emails can't be sent until you do.
AI mode uses Claude Opus 4.6 through bedrock-runtime (`us.anthropic.claude-opus-4-6-v1`), which needs its model agreement in us-east-2.
`aws cloudformation deploy` keeps previous parameter values, so pass any changed parameter explicitly.

## Test

```bash
aws lambda invoke --region us-east-2 --function-name nfl-pickem \
  --cli-binary-format raw-in-base64-out --payload '{"run":"sunday","test":true}' /dev/stdout
```

```bash
aws lambda invoke --region us-east-2 --function-name nfl-fantasy-lineup \
  --cli-binary-format raw-in-base64-out --payload '{"run":"saturday","test":true}' /dev/stdout
```

`"test": true` adds `[TEST]` to the subject and doesn't save Thursday's lock.
