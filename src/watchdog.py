"""Emails me when a scheduled Lambda didn't finish a run on time.

The pick'em and lineup handlers record a heartbeat when a run completes (including quiet ones such as
"off season" or "no game soon"). A timeout, an out-of-memory kill, an import error or a schedule that never
fired leaves no heartbeat, which no error handler inside those Lambdas can report. Standalone on purpose:
it imports nothing from app.py, so a broken app doesn't take the watchdog down with it.

Event: {"function": "nfl-pickem", "max_age_hours": 5}
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

HEARTBEAT_PREFIX = os.environ.get("HEARTBEAT_PARAM_PREFIX", "/nfl-pickem/heartbeat")
TOPIC_ARN = os.environ["FAILURE_TOPIC_ARN"]

ssm = boto3.client("ssm")
sns = boto3.client("sns")


def last_run(function):
    try:
        return datetime.fromisoformat(ssm.get_parameter(Name=f"{HEARTBEAT_PREFIX}/{function}")["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None


def handler(event, context):
    function, max_age = event["function"], timedelta(hours=float(event.get("max_age_hours", 5)))
    now = datetime.now(UTC)
    last = last_run(function)
    if last and now - last <= max_age:
        log.info("%s finished a run at %s; fine", function, last)
        return {"status": "ok", "function": function}
    since = f"since {last:%a %Y-%m-%d %H:%M} UTC" if last else "ever (no heartbeat recorded)"
    sns.publish(
        TopicArn=TOPIC_ARN,
        Subject=f"{function}: no completed run - check it"[:100],
        Message=(
            f"{function} hasn't finished a run {since}, but one was due within the last {max_age}.\n\n"
            "It probably timed out, ran out of memory, failed to start, or its schedule is disabled.\n"
            f"Check the /aws/lambda/{function} logs, and the schedules (aws scheduler list-schedules)."
        ),
    )
    log.warning("%s overdue (last run %s)", function, last)
    return {"status": "overdue", "function": function}
