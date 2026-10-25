import os

# The Lambda modules read their config and build boto3 clients at import time.
os.environ.setdefault("RECIPIENT_EMAIL", "to@example.com")
os.environ.setdefault("SENDER_EMAIL", "from@example.com")
os.environ.setdefault("LEAGUE_ID", "1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
