# NFL Pick'em emailer

Emails pick'em picks with confidence points (1..N, each used once per week), during the regular season only.
Preseason and playoff runs exit without sending (based on ESPN's season type):

- **Thursday 10:00 CT**: Thursday game only. Its points are saved in SSM (`/nfl-pickem/locked/<season>-w<week>`).
- **Sunday 07:00 CT**: every game that hasn't started, using the leftover points.

Data comes from ESPN's public API (lines, injuries, transactions, news). With `UseAI=true`,
Claude on Amazon Bedrock makes the picks; otherwise, or if the AI call fails,
it picks the favorite and gives more points to bigger spreads.

## Deploy

```bash
./deploy.sh                          # defaults
./deploy.sh UseAI=false              # free mode
./deploy.sh UseAI=true ModelId=anthropic.claude-sonnet-5-5
```

First deploy: click the SES verification link AWS emails to the sender address. Emails can't be sent until you do.
AI mode also needs the Bedrock Marketplace agreement accepted for the chosen model in us-east-2.

## Test

```bash
aws lambda invoke --region us-east-2 --function-name nfl-pickem \
  --cli-binary-format raw-in-base64-out --payload '{"run":"sunday","test":true}' /dev/stdout
```

`"test": true` adds `[TEST]` to the subject and doesn't save Thursday's lock.
