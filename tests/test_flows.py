"""End-to-end handler flows, email builders and AI prompts, with every network call stubbed."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_lineup import FakeSsm, player

import app
import lineup


def pgame(gid, home, away, hours, started=False, fav=None, fav_by=0.0):
    kickoff = datetime(2026, 10, 25, 12, tzinfo=lineup.CENTRAL) + timedelta(hours=hours)
    return {
        "id": gid,
        "home": home,
        "away": away,
        "home_name": home.title(),
        "away_name": away.title(),
        "home_record": "3-1",
        "away_record": "2-2",
        "kickoff": kickoff.astimezone(UTC),
        "kickoff_ct": kickoff,
        "started": started,
        "line": "KC -3",
        "over_under": 44.5,
        "fav": fav,
        "fav_by": fav_by,
    }


class TestPickemHandler:
    @pytest.fixture
    def env(self, monkeypatch):
        """Thursday game (g1) plus two Sunday games; returns the recorded side effects."""
        weekday = datetime(2026, 10, 22, 20, tzinfo=lineup.CENTRAL)  # a Thursday
        games = [
            pgame("g1", "KC", "LV", 0, fav="KC", fav_by=7),
            pgame("g2", "BUF", "NE", 72, fav="BUF", fav_by=3),
            pgame("g3", "SF", "LA", 73, fav="LA", fav_by=1),
        ]
        games[0]["kickoff_ct"] = weekday
        rec = SimpleNamespace(sent=[], saved=[], games=games, locked={})
        monkeypatch.setattr(app, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(app, "parse_games", lambda sb: rec.games)
        monkeypatch.setattr(app, "add_kalshi", lambda games: 0)
        monkeypatch.setattr(app, "odds_api_key", lambda: None)
        monkeypatch.setattr(app, "load_locked", lambda season, week: dict(rec.locked))
        monkeypatch.setattr(app, "save_locked", lambda season, week, locked: rec.saved.append(locked))
        monkeypatch.setattr(app, "send_email", lambda *a: rec.sent.append(a))
        monkeypatch.setattr(app, "USE_AI", False)
        return rec

    def test_off_season_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(app, "fetch", lambda path: {"season": {"type": 3}})
        assert app.run_pickem({}) == {"status": "off season"}

    def test_no_games(self, env):
        env.games.clear()
        assert app.run_pickem({"run": "sunday"}) == {"status": "no games"}

    def test_thursday_ranks_whole_week_but_sends_and_locks_only_thursday(self, env):
        result = app.run_pickem({"run": "thursday"})
        assert result["status"] == "sent"
        assert [p["game_id"] for p in result["picks"]] == ["g1"]
        assert result["picks"][0]["confidence"] == 3  # biggest favorite of the three gets the top point
        assert env.saved == [{"g1": {"winner": "KC", "confidence": 3}}]
        assert "Thursday" in env.sent[0][0]

    def test_thursday_test_run_does_not_lock(self, env):
        app.run_pickem({"run": "thursday", "test": True})
        assert not env.saved and env.sent[0][0].startswith("[TEST]")

    def test_sunday_uses_points_left_after_the_thursday_lock(self, env):
        env.locked = {"g1": {"winner": "KC", "confidence": 3}}
        result = app.run_pickem({"run": "sunday"})
        assert sorted(p["confidence"] for p in result["picks"]) == [1, 2]
        assert not env.saved

    def test_sunday_with_nothing_left_does_nothing(self, env):
        for g in env.games:
            g["started"] = True
        env.locked = {g["id"]: {"winner": g["home"], "confidence": i + 1} for i, g in enumerate(env.games)}
        assert app.run_pickem({"run": "sunday"}) == {"status": "no target games"}

    def test_started_unlocked_game_takes_lowest_points_and_is_noted(self, env):
        env.games[1]["started"] = True  # picked by hand before the run
        result = app.run_pickem({"run": "sunday"})
        assert sorted(p["confidence"] for p in result["picks"]) == [2, 3]
        assert any("already kicked off" in n for n in env.sent[0][3])

    def test_failed_email_leaves_the_week_unlocked(self, env, monkeypatch):
        def boom(*a):
            raise RuntimeError("ses down")

        monkeypatch.setattr(app, "send_email", boom)
        with pytest.raises(RuntimeError):
            app.run_pickem({"run": "thursday"})
        assert not env.saved

    def test_market_failures_become_notes_not_errors(self, env, monkeypatch):
        def boom(games):
            raise OSError("kalshi down")

        monkeypatch.setattr(app, "add_kalshi", boom)
        app.run_pickem({"run": "sunday"})
        notes = env.sent[0][3]
        assert any("Kalshi failed" in n for n in notes) and any("No market price" in n for n in notes)

    def test_ai_failure_falls_back_to_spread_picks(self, env, monkeypatch):
        def boom(*a):
            raise RuntimeError("bedrock down")

        monkeypatch.setattr(app, "USE_AI", True)
        monkeypatch.setattr(app, "ai_picks", boom)
        result = app.run_pickem({"run": "sunday"})
        assert result["status"] == "sent" and any("AI picks failed" in n for n in env.sent[0][3])

    def test_default_run_follows_the_weekday(self, env, monkeypatch):
        monkeypatch.setattr(app, "datetime", SimpleNamespace(now=lambda tz: datetime(2026, 10, 22, 8, tzinfo=tz)))
        assert app.run_pickem({})["run"] == "thursday"
        monkeypatch.setattr(app, "datetime", SimpleNamespace(now=lambda tz: datetime(2026, 10, 25, 6, tzinfo=tz)))
        assert app.run_pickem({})["run"] == "sunday"


class TestPickemEmail:
    def test_builds_html_and_text_and_escapes(self, monkeypatch):
        sent = []
        monkeypatch.setattr(app, "deliver", lambda *a: sent.append(a))
        g = pgame("g1", "KC", "LV", 0)
        picks = [
            app.Pick(game_id="g1", winner="KC", confidence=1, reasoning="low <b>"),
            app.Pick(game_id="g1", winner="LV", confidence=9, reasoning="high & mighty"),
        ]
        app.send_email("Subj", picks, [g], ["a note"], "the <summary>")
        subject, body, text = sent[0]
        assert subject == "Subj"
        assert body.index("high &amp; mighty") < body.index("low &lt;b&gt;")  # most confident first
        assert "the &lt;summary&gt;" in body and "a note" in body
        assert text.splitlines()[0] == "the <summary>"
        assert "  9  LV" in text

    def test_deliver_sends_one_multipart_message(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app.ses, "send_email", lambda **kw: calls.append(kw))
        app.deliver("S", "<p>h</p>", "t")
        body = calls[0]["Content"]["Simple"]["Body"]
        assert calls[0]["Destination"]["ToAddresses"] == [app.RECIPIENT]
        assert body["Html"]["Data"] == "<p>h</p>" and body["Text"]["Data"] == "t"


class TestPickemAi:
    def test_prompt_lists_games_values_and_context(self):
        g = pgame("g1", "KC", "LV", 0)
        g.update(home_prob=0.7, prob_source="Kalshi market")
        prompt = app.build_prompt(
            [g], {1, 2}, {"KC": ["QB out"], "LV": []}, ["10/20 KC: signed X"], ["10/24 News - body"], "Locked: LV (5)."
        )
        assert "[1, 2]" in prompt and "game_id=g1" in prompt
        assert "Kalshi market: KC 70% to win" in prompt
        assert "KC: QB out" in prompt and "LV:" not in prompt  # teams with nothing to report are left out
        assert "signed X" in prompt and "News - body" in prompt and "Locked: LV (5)." in prompt

    def test_ai_picks_validates_and_renumbers(self, monkeypatch):
        games = [pgame("g1", "KC", "LV", 0), pgame("g2", "BUF", "NE", 1)]
        for name in ("injuries_by_team", "recent_transactions"):
            monkeypatch.setattr(app, name, lambda *a, **k: {})
        monkeypatch.setattr(app, "recent_news", lambda: [])
        sheet = {
            "picks": [
                {"game_id": "g1", "winner": "KC", "confidence": 40, "reasoning": "r"},
                {"game_id": "g2", "winner": "NE", "confidence": 40, "reasoning": "r"},
            ],
            "summary": "s",
        }
        monkeypatch.setattr(app, "claude_tool_call", lambda *a: sheet)
        picks, summary = app.ai_picks(games, {1, 2}, "")
        assert summary == "s" and sorted(p.confidence for p in picks) == [1, 2]

    def test_ai_picks_rejects_malformed_output(self, monkeypatch):
        for name in ("injuries_by_team", "recent_transactions"):
            monkeypatch.setattr(app, name, lambda *a, **k: {})
        monkeypatch.setattr(app, "recent_news", lambda: [])
        monkeypatch.setattr(app, "claude_tool_call", lambda *a: {"picks": "nope"})
        with pytest.raises(ValueError):
            app.ai_picks([pgame("g1", "KC", "LV", 0)], {1}, "")


class FakeClient:
    def __init__(self, content, stop_reason="tool_use"):
        self.response = SimpleNamespace(content=content, stop_reason=stop_reason, usage="u")
        self.kwargs = None
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class TestClaudeToolCall:
    def call(self, monkeypatch, content, stop_reason="tool_use"):
        import anthropic

        client = FakeClient(content, stop_reason)
        monkeypatch.setattr(anthropic, "AnthropicBedrock", lambda **kw: client)
        return client, lambda: app.claude_tool_call("prompt", "submit", "desc", {"type": "object"})

    def test_returns_the_tool_input(self, monkeypatch):
        block = SimpleNamespace(type="tool_use", name="submit", input={"a": 1})
        client, run = self.call(monkeypatch, [SimpleNamespace(type="text", name="x"), block])
        assert run() == {"a": 1}
        assert client.kwargs["model"] == app.MODEL_ID and client.kwargs["tools"][0]["name"] == "submit"
        assert "calling submit" in client.kwargs["messages"][0]["content"]

    def test_refusal_and_missing_tool_call_raise(self, monkeypatch):
        _, run = self.call(monkeypatch, [], stop_reason="refusal")
        with pytest.raises(RuntimeError, match="declined"):
            run()
        _, run = self.call(monkeypatch, [SimpleNamespace(type="text", name="x")], stop_reason="end_turn")
        with pytest.raises(RuntimeError, match="did not call submit"):
            run()


def lplayer(pid, pos, proj, **kw):
    return player(pid, pos, proj, **kw)


class TestLineupEmail:
    SLOTS = [("QB", 0), ("RB1", 2)]

    def build(self, monkeypatch, actions, pickups=None, summary="sum <x>"):
        sent = []
        monkeypatch.setattr(lineup, "deliver", lambda *a: sent.append(a))
        qb, rb, bench = lplayer(1, "QB", 20, slot=0), lplayer(2, "RB", 15, slot=2), lplayer(3, "RB", 5, status="OUT")
        lu = {"QB": qb, "RB1": rb}
        lineup.send_email(
            "Subj", {"name": "Rockers"}, [qb, rb, bench], lu, actions, ["a note"], summary, {1: "why <1>"}, pickups, ""
        )
        return sent[0]

    def test_actions_summary_bench_and_escaping(self, monkeypatch):
        subject, body, text = self.build(monkeypatch, ["Start P1 at QB"])
        assert subject == "Subj" and "Rockers" in body
        assert "Action needed" in body and "<b>Start P1 at QB</b>" in body
        assert "sum &lt;x&gt;" in body and "why &lt;1&gt;" in body and "a note" in body
        assert "Lineup (projected 35.0)" in body and "Out" in body
        assert text.startswith("Rockers\n\nACTION NEEDED:") and "BENCH:" in text

    def test_no_actions_says_no_changes(self, monkeypatch):
        _, body, text = self.build(monkeypatch, [])
        assert "No changes needed" in body and "No changes needed." in text

    def test_pickups_section_present_or_left_out(self, monkeypatch):
        add = {"name": "A", "pos": "WR", "team": "KC", "ros_avg": 9.0, "availability": "WAIVERS"}
        _, body, text = self.build(monkeypatch, [], pickups=[(add, {"name": "D", "ros_avg": 3.0}, "because")])
        assert "Free-agent pickups" in body and "A (waivers)" in body and "FREE-AGENT PICKUPS" in text
        _, body, text = self.build(monkeypatch, [], pickups=[])
        assert "No pickups worth making" in body and "none worth making" in text
        _, body, text = self.build(monkeypatch, [], pickups=None)
        assert "Free-agent pickups" not in body and "FREE-AGENT" not in text

    def test_row_formats_empty_bye_and_injured_players(self):
        assert "(empty)" in lineup.row("QB", None, {}, True)
        bye = lplayer(1, "QB", 0, game=False)
        assert lineup.why(bye, {}) == "Bye week"
        assert lineup.why(lplayer(2, "QB", 9, status="OUT"), {}) == "Out"
        assert lineup.why(lplayer(3, "QB", 9), {}) == ""
        assert lineup.why(lplayer(4, "QB", 9), {4: "AI says"}) == "AI says"


class TestLineupHandlerFlow:
    @pytest.fixture
    def env(self, monkeypatch):
        rec = SimpleNamespace(sent=[], ai_calls=0)
        qb, rb = lplayer(1, "QB", 20, slot=0), lplayer(2, "RB", 15, slot=lineup.BENCH)
        monkeypatch.setattr(lineup, "ssm", FakeSsm())
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(lineup, "parse_games", lambda sb: [qb["game"]])
        monkeypatch.setattr(lineup, "fetch_league", lambda season, week: ({}, "swid"))
        monkeypatch.setattr(lineup, "my_team", lambda *a: {"name": "T"})
        monkeypatch.setattr(lineup, "lineup_slots", lambda *a: [("QB", 0), ("RB", 2)])
        monkeypatch.setattr(lineup, "players", lambda *a: [qb, rb])
        monkeypatch.setattr(lineup, "suggest_pickups", lambda *a: ([], "", "pickup note"))
        monkeypatch.setattr(lineup, "send_email", lambda *a: rec.sent.append(a))
        monkeypatch.setattr(lineup, "USE_AI", False)
        rec.roster = [qb, rb]
        return rec

    def test_sunday_sends_lineup_with_pickups_note(self, env):
        result = lineup.handler({"run": "sunday"}, None)
        assert result["status"] == "sent" and result["moves"] == ["Start P2 at RB"]
        assert result["lineup"] == {"QB": "P1", "RB": "P2"}
        assert "pickup note" in env.sent[0][5] and "Fantasy Week 7 Lineup - Sunday final" in env.sent[0][0]

    def test_pickup_failure_still_sends_the_lineup(self, env, monkeypatch):
        def boom(*a):
            raise RuntimeError("espn down")

        monkeypatch.setattr(lineup, "suggest_pickups", boom)
        assert lineup.handler({"run": "sunday"}, None)["status"] == "sent"
        assert any("pickups failed" in n for n in env.sent[0][5])

    def test_ai_failure_falls_back_to_projections(self, env, monkeypatch):
        qb, rb = env.roster
        qb["status"] = "QUESTIONABLE"  # a close call for the AI to review

        def boom(*a):
            raise RuntimeError("bedrock down")

        monkeypatch.setattr(lineup, "USE_AI", True)
        monkeypatch.setattr(lineup, "ai_lineup", boom)
        assert lineup.handler({"run": "sunday"}, None)["status"] == "sent"
        assert any("AI review failed" in n for n in env.sent[0][5])

    def test_ai_lineup_is_used_when_it_validates(self, env, monkeypatch):
        qb, rb = env.roster
        qb["status"] = "QUESTIONABLE"
        monkeypatch.setattr(lineup, "USE_AI", True)
        monkeypatch.setattr(lineup, "ai_lineup", lambda roster, slots, proposed, calls: (proposed, {1: "r"}, "ai summary"))
        lineup.handler({"run": "sunday"}, None)
        assert env.sent[0][6] == "ai summary" and any("reviewed by" in n for n in env.sent[0][5])

    def test_thursday_adds_the_preview_note(self, env):
        lineup.handler({"run": "thursday"}, None)
        assert any("Only Thursday-game players" in n for n in env.sent[0][5])


class TestLineupAiPrompt:
    def test_prompt_contains_roster_proposal_and_close_calls(self, monkeypatch):
        qb = lplayer(1, "QB", 20, slot=0, status="QUESTIONABLE")
        captured = {}
        monkeypatch.setattr(lineup, "injuries_by_team", lambda teams: {"KC": ["QB (Q) - Questionable"]})
        monkeypatch.setattr(lineup, "recent_news", lambda: ["10/24 headline - body"])

        def fake(prompt, name, desc, schema):
            captured["prompt"] = prompt
            return {"lineup": [{"slot": "QB", "player_id": 1}], "reasons": [{"player_id": 1, "reason": "keep"}], "summary": "s"}

        monkeypatch.setattr(lineup, "claude_tool_call", fake)
        result, reasons, summary = lineup.ai_lineup([qb], [("QB", 0)], {"QB": qb}, ["P1 (QB) is questionable"])
        p = captured["prompt"]
        assert "SLOTS: QB" in p and "id=1 P1 QB KC" in p and "P1 (QB) is questionable" in p
        assert "KC: QB (Q) - Questionable" in p and "headline - body" in p
        assert result["QB"] is qb and reasons == {1: "keep"} and summary == "s"
