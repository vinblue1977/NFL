import os

# The Lambda modules read their config and build boto3 clients at import time.
os.environ.setdefault("RECIPIENT_EMAIL", "to@example.com")
os.environ.setdefault("SENDER_EMAIL", "from@example.com")
os.environ.setdefault("LEAGUE_ID", "1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")


import io  # noqa: E402
import json  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def http(monkeypatch):
    """Replace urllib's urlopen with canned JSON: http({url_substring: body, ...}) -> list of requested URLs.

    Bodies are served in order when a list is given, so paginated endpoints can be faked.
    """

    def install(routes):
        seen = []

        def urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            seen.append(url)
            for part, body in routes.items():
                if part in url:
                    if isinstance(body, list):
                        body = body.pop(0)
                    if isinstance(body, Exception):
                        raise body
                    resp = io.BytesIO(json.dumps(body).encode())
                    resp.headers = {}
                    return resp
            raise AssertionError(f"unexpected request: {url}")

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return seen

    return install
