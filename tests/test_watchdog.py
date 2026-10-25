"""Silent-failure cover: heartbeats, the watchdog, and the ESPN cookie warning."""

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

import app
import lineup
import watchdog


@pytest.fixture
def sent(monkeypatch):
    """Capture SNS publishes from app, lineup and the watchdog."""
    out = []
    monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "arn:topic")
    for client in (app.sns, watchdog.sns):
        monkeypatch.setattr(client, "publish", lambda **kw: out.append(kw))
    return out


class TestHeartbeat:
    def test_handler_records_a_heartbeat_after_a_normal_run(self, fake_ssm, monkeypatch):
        monkeypatch.setattr(app, "run_pickem", lambda event: {"status": "off season"})
        assert app.handler({}, None) == {"status": "off season"}
        assert datetime.fromisoformat(fake_ssm.values["/nfl-pickem/heartbeat/nfl-pickem"])

    def test_a_failed_run_leaves_no_heartbeat(self, fake_ssm, monkeypatch):
        monkeypatch.setattr(app, "run_pickem", lambda event: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            app.handler({}, None)
        assert not fake_ssm.values

    def test_a_heartbeat_write_failure_never_breaks_the_run(self, monkeypatch):
        monkeypatch.setattr(app.ssm, "put_parameter", lambda **kw: (_ for _ in ()).throw(OSError("denied")))
        app.heartbeat("nfl-pickem")

    def test_lineup_handler_records_its_own_heartbeat(self, fake_ssm, monkeypatch):
        monkeypatch.setattr(lineup, "run_lineup", lambda event: {"status": "no game soon"})
        lineup.handler({}, None)
        assert "/nfl-pickem/heartbeat/nfl-fantasy-lineup" in fake_ssm.values


class TestWatchdog:
    def beat(self, fake_ssm, function, age):
        fake_ssm.values[f"/nfl-pickem/heartbeat/{function}"] = (datetime.now(UTC) - age).isoformat()

    def test_fresh_heartbeat_is_quiet(self, fake_ssm, sent):
        self.beat(fake_ssm, "nfl-pickem", timedelta(hours=2))
        assert watchdog.handler({"function": "nfl-pickem", "max_age_hours": 5}, None)["status"] == "ok"
        assert not sent

    def test_stale_heartbeat_emails(self, fake_ssm, sent):
        self.beat(fake_ssm, "nfl-pickem", timedelta(hours=30))
        assert watchdog.handler({"function": "nfl-pickem", "max_age_hours": 5}, None)["status"] == "overdue"
        assert "nfl-pickem" in sent[0]["Subject"] and "/aws/lambda/nfl-pickem" in sent[0]["Message"]

    def test_no_heartbeat_at_all_emails(self, fake_ssm, sent):
        assert watchdog.handler({"function": "nfl-fantasy-lineup"}, None)["status"] == "overdue"
        assert "no heartbeat recorded" in sent[0]["Message"]

    def test_functions_are_checked_separately(self, fake_ssm, sent):
        self.beat(fake_ssm, "nfl-pickem", timedelta(hours=1))
        assert watchdog.handler({"function": "nfl-fantasy-lineup"}, None)["status"] == "overdue"


def http_error(code):
    return urllib.error.HTTPError("https://espn", code, "msg", {}, io.BytesIO(b""))


class TestCookieExpiry:
    @pytest.fixture(autouse=True)
    def cookies(self, monkeypatch):
        monkeypatch.setattr(lineup, "secret", lambda name: "v")

    @pytest.mark.parametrize("code", [401, 403])
    def test_rejected_cookies_raise_a_specific_error(self, monkeypatch, code):
        monkeypatch.setattr(lineup.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(http_error(code)))
        with pytest.raises(lineup.EspnCookieError, match=str(code)):
            lineup.fantasy_get("x")

    def test_other_http_errors_stay_ordinary(self, monkeypatch):
        monkeypatch.setattr(lineup.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(http_error(500)))
        with pytest.raises(urllib.error.HTTPError):
            lineup.fantasy_get("x")

    def expired(self, monkeypatch):
        monkeypatch.setattr(
            lineup, "run_lineup", lambda event: (_ for _ in ()).throw(lineup.EspnCookieError("ESPN refused (HTTP 401)"))
        )

    def test_handler_sends_one_how_to_fix_email_instead_of_failing(self, sent, fake_ssm, monkeypatch):
        self.expired(monkeypatch)
        assert lineup.handler({"run": "sunday"}, None) == {"status": "espn cookies expired"}
        assert "cookies expired" in sent[0]["Subject"] and "put-parameter --name /nfl-pickem/espn-s2" in sent[0]["Message"]
        assert "/nfl-pickem/heartbeat/nfl-fantasy-lineup" in fake_ssm.values  # a handled run still counts as a run

    def test_warns_at_most_once_a_day(self, sent, fake_ssm, monkeypatch):
        self.expired(monkeypatch)
        lineup.handler({}, None)
        lineup.handler({}, None)
        assert len(sent) == 1
        fake_ssm.values["/nfl-pickem/cookie-alert"] = json.dumps((datetime.now(UTC) - timedelta(hours=21)).isoformat())
        lineup.handler({}, None)
        assert len(sent) == 2

    def test_fails_loudly_when_it_cannot_tell_me(self, monkeypatch):
        self.expired(monkeypatch)
        monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "")
        with pytest.raises(lineup.EspnCookieError):
            lineup.handler({}, None)


class TestNotify:
    def test_reports_whether_it_sent_and_never_raises(self, monkeypatch):
        monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "arn:topic")
        monkeypatch.setattr(app.sns, "publish", lambda **kw: None)
        assert app.notify("s", "m")
        monkeypatch.setattr(app.sns, "publish", lambda **kw: (_ for _ in ()).throw(OSError("down")))
        assert not app.notify("s", "m")
        monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "")
        assert not app.notify("s", "m")
