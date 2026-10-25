"""Paths the other suites skip: pickups end to end, league lookups, odds fallbacks and the lineup handler's AI branches."""

from datetime import UTC, datetime, timedelta

import pytest
from test_lineup import SLOTS, player

import app
import lineup
from lineup import BENCH, IR


def espn_player(pid, name, pos, team, ros, status=None):
    stat = {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0}
    return {
        "id": pid,
        "fullName": name,
        "defaultPositionId": pos,
        "proTeamId": team,
        "injuryStatus": status,
        "stats": [{**stat, "appliedTotal": ros, "appliedAverage": ros / 10}],
    }


class TestSuggestPickups:
    WR = 3

    @pytest.fixture
    def env(self, monkeypatch):
        entries = [
            {"lineupSlotId": BENCH, "playerPoolEntry": {"player": espn_player(10, "My WR", self.WR, 99, 40)}},
            {"lineupSlotId": IR, "playerPoolEntry": {"player": espn_player(11, "My IR", self.WR, 99, 1)}},
        ]
        free = [
            {"player": espn_player(1, "Free WR", self.WR, 13, 100), "status": "FREEAGENT"},
            {"player": espn_player(2, "Started WR", self.WR, 12, 300), "status": "FREEAGENT"},  # his game already kicked off
            {"player": espn_player(3, "Hurt WR", self.WR, 13, 200, status="OUT"), "status": "WAIVERS"},
        ]
        monkeypatch.setattr(lineup, "pro_teams", lambda season: {12: ("KC", 10), 13: ("LV", 7), 99: ("NE", 5)})
        monkeypatch.setattr(lineup, "fetch_free_agents", lambda season, week: free)
        monkeypatch.setattr(lineup, "player_news", lambda pid: [{"when": "10/24", "headline": f"news {pid}", "story": ""}])
        games = [{"home_id": 12, "away_id": 14, "started": True}]
        return {"roster": {"entries": entries}}, games

    def run(self, env):
        team, games = env
        return lineup.suggest_pickups(2026, 7, team, games, SLOTS)

    def test_projection_swap_skips_ir_started_and_injured_players(self, env, monkeypatch):
        monkeypatch.setattr(lineup, "USE_AI", False)
        moves, summary, note = self.run(env)
        assert [(a["name"], d["name"]) for a, d, _ in moves] == [
            ("Free WR", "My WR")
        ]  # not the IR guy, not the started or hurt WR
        assert summary == "" and "AI off" in note
        assert "news 1" in moves[0][2]  # the latest news rides along in the reason

    def test_ai_choice_is_used_when_it_validates(self, env, monkeypatch):
        monkeypatch.setattr(lineup, "USE_AI", True)
        seen = {}

        def fake_ai(pool, mine, droppable, slots, week):
            seen["pool"], seen["droppable"] = [p["name"] for p in pool], [p["name"] for p in droppable]
            return [("add", "drop", "because")], "ai summary"

        monkeypatch.setattr(lineup, "ai_pickups", fake_ai)
        moves, summary, note = self.run(env)
        assert moves == [("add", "drop", "because")] and summary == "ai summary" and "Pickups chosen by" in note
        assert seen["droppable"] == ["My WR"] and "Started WR" not in seen["pool"]

    def test_ai_failure_falls_back_to_projections(self, env, monkeypatch):
        monkeypatch.setattr(lineup, "USE_AI", True)
        monkeypatch.setattr(lineup, "ai_pickups", lambda *a: (_ for _ in ()).throw(ValueError("bad ids")))
        moves, summary, note = self.run(env)
        assert len(moves) == 1 and summary == "" and "AI pickup review failed (ValueError: bad ids)" in note


class TestLeagueLookups:
    def setup_method(self):
        lineup.secret.cache_clear()

    def test_secret_reads_ssm_once(self, fake_ssm):
        fake_ssm.values["/x"] = "v1"
        assert lineup.secret("/x") == "v1"
        fake_ssm.values["/x"] = "v2"
        assert lineup.secret("/x") == "v1"  # cached for the life of the Lambda container

    LEAGUE = {"teams": [{"id": 1, "owners": ["{A}"]}, {"id": 2, "owners": ["{B}"]}]}

    def test_my_team_by_swid_or_by_team_id(self, monkeypatch):
        monkeypatch.setattr(lineup, "TEAM_ID", "")
        assert lineup.my_team(self.LEAGUE, "{B}")["id"] == 2
        monkeypatch.setattr(lineup, "TEAM_ID", "1")
        assert lineup.my_team(self.LEAGUE, "{B}")["id"] == 1  # an explicit team id wins

    def test_my_team_missing_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(lineup, "TEAM_ID", "")
        with pytest.raises(RuntimeError, match="couldn't find my team"):
            lineup.my_team(self.LEAGUE, "{nobody}")

    def test_team_name_falls_back_to_location_and_nickname(self):
        assert lineup.team_name({"name": "Rockers"}) == "Rockers"
        assert lineup.team_name({"location": "Kansas City", "nickname": "Chiefs"}) == "Kansas City Chiefs"

    def test_lineup_slots_number_only_repeated_slots(self):
        league = {"settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "23": 1, "16": 1, "20": 6}}}}
        assert lineup.lineup_slots(league) == [("QB", 0), ("RB1", 2), ("RB2", 2), ("D/ST", 16), ("FLEX", 23)]


class TestSmallLineupEdges:
    def test_close_calls_ignore_empty_and_locked_slots(self):
        locked = player(1, "QB", 20, slot=0, locked=True, status="OUT")
        bench = player(2, "RB", 19)
        assert lineup.close_calls([locked, bench], {"QB": locked, "RB1": None}, SLOTS) == []

    def test_validate_rejects_starting_a_benched_player_whose_game_started(self):
        qb = player(1, "QB", 20, slot=BENCH, locked=True)
        sheet = lineup.LineupSheet(lineup=[lineup.SlotPick(slot="QB", player_id=1)], reasons=[], summary="s")
        with pytest.raises(ValueError, match="benched player"):
            lineup.validate(sheet, [qb], [("QB", 0)], {"QB": None})

    def test_projection_swaps_need_a_droppable_player_at_the_position(self):
        fa = {"id": 1, "pos": "TE", "ros": 100, "status": "ACTIVE", "news": []}
        wr = {"id": 2, "pos": "WR", "ros": 10, "status": "ACTIVE", "news": []}
        assert lineup.projection_pickups([fa], [wr]) == []

    def test_player_news_skips_items_with_missing_or_bad_dates(self, http):
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        feed = [
            {"type": "Rotowire", "headline": "no date"},
            {"type": "Rotowire", "headline": "bad", "published": "yesterday"},
            {"type": "Rotowire", "headline": "good", "published": recent, "story": "s"},
        ]
        http({"news/players": {"feed": feed}})
        assert [n["headline"] for n in lineup.player_news(1)] == ["good"]


class TestOddsFallbacks:
    def games(self, **kw):
        base = {"id": "1", "home": "KC", "away": "LV", "home_name": "Kansas City", "away_name": "Las Vegas"}
        return [{**base, "kickoff": datetime(2026, 10, 25, 17, tzinfo=UTC), **kw}]

    def event(self, books, home="Kansas City", away="Las Vegas", when="2026-10-25T17:00:00Z"):
        return {"home_team": home, "away_team": away, "commence_time": when, "bookmakers": books}

    def book(self, *markets):
        return {"markets": [{"key": k, "outcomes": o} for k, o in markets]}

    def h2h(self, home, away):
        return ("h2h", [{"name": "Kansas City", "price": home}, {"name": "Las Vegas", "price": away}])

    def run(self, http, games, events):
        http({"the-odds-api": [events]})
        return app.add_consensus(games, "key")

    def test_odds_api_key_found_or_absent(self, fake_ssm):
        assert app.odds_api_key() is None
        fake_ssm.values["/nfl-pickem/odds-api-key"] = "abc"
        assert app.odds_api_key() == "abc"

    def test_games_that_already_have_a_price_are_left_alone(self, http):
        g = self.games(home_prob=0.9)
        assert self.run(http, g, [self.event([self.book(self.h2h(-200, 170))])]) == 0 and g[0]["home_prob"] == 0.9

    @pytest.mark.parametrize(
        "events",
        [
            [],  # nothing listed
            [{"home_team": "Other", "away_team": "Las Vegas", "commence_time": "2026-10-25T17:00:00Z", "bookmakers": []}],
            [{"home_team": "Kansas City", "away_team": "Las Vegas", "commence_time": "2026-10-28T17:00:00Z", "bookmakers": []}],
        ],
    )
    def test_unmatched_events_leave_the_game_unpriced(self, http, events):
        g = self.games()
        assert self.run(http, g, events) == 0 and "home_prob" not in g[0]

    def test_books_without_usable_moneylines_leave_it_unpriced(self, http):
        partial = ("h2h", [{"name": "Kansas City", "price": -150}])  # no price for the visitors
        only_spread = ("spreads", [{"name": "Kansas City", "point": -3}, {"name": "Las Vegas", "point": 3}])
        g = self.games()
        assert self.run(http, g, [self.event([self.book(partial), self.book(only_spread)])]) == 0 and "home_prob" not in g[0]

    def test_pick_em_and_away_favorites_get_the_right_line(self, http):

        def spreads(pt):
            return ("spreads", [{"name": "Kansas City", "point": pt}, {"name": "Las Vegas", "point": -pt}])

        g = self.games()
        self.run(http, g, [self.event([self.book(self.h2h(100, 100), spreads(0))])])
        assert (g[0]["fav"], g[0]["line"]) == (None, "PK")
        g = self.games()
        self.run(http, g, [self.event([self.book(self.h2h(150, -170), spreads(3))])])
        assert (g[0]["fav"], g[0]["fav_by"], g[0]["line"]) == ("LV", 3.0, "LV -3")

    def test_moneyline_without_a_spread_keeps_the_espn_line(self, http):
        g = self.games(fav="KC", fav_by=2.5, line="KC -2.5")
        self.run(http, g, [self.event([self.book(self.h2h(-130, 110))])])
        assert g[0]["home_prob"] > 0.5 and g[0]["line"] == "KC -2.5"


class PickemEnv:
    """run_pickem with every network call stubbed; one unpriced Sunday game."""

    @pytest.fixture
    def env(self, monkeypatch, fake_ssm):
        game = {
            "id": "g1",
            "home": "KC",
            "away": "LV",
            "home_name": "KC",
            "away_name": "LV",
            "home_record": "",
            "away_record": "",
            "line": "KC -3",
            "over_under": 44,
            "fav": "KC",
            "fav_by": 3.0,
            "started": False,
            "kickoff": datetime(2026, 10, 25, 17, tzinfo=UTC),
            "kickoff_ct": datetime(2026, 10, 25, 12, tzinfo=lineup.CENTRAL),
        }
        sent = []
        monkeypatch.setattr(app, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(app, "parse_games", lambda sb: [game])
        monkeypatch.setattr(app, "add_kalshi", lambda games: 0)
        monkeypatch.setattr(app, "send_email", lambda *a: sent.append(a))
        monkeypatch.setattr(app, "USE_AI", False)
        return sent, fake_ssm


class TestPickemOddsAndAi(PickemEnv):
    def test_odds_api_runs_when_a_key_exists_and_its_failure_is_a_note(self, env, monkeypatch):
        sent, _ = env
        monkeypatch.setattr(app, "odds_api_key", lambda: "key")
        monkeypatch.setattr(app, "add_consensus", lambda games, key: (_ for _ in ()).throw(OSError("quota")))
        app.run_pickem({"run": "sunday"})
        assert any("Odds API failed (OSError: quota)" in n for n in sent[0][3])

    def test_a_successful_odds_api_call_fills_the_price(self, env, monkeypatch):
        sent, _ = env
        monkeypatch.setattr(app, "odds_api_key", lambda: "key")
        monkeypatch.setattr(
            app, "add_consensus", lambda games, key: games[0].update(home_prob=0.7, prob_source="consensus of 3 sportsbooks")
        )
        result = app.run_pickem({"run": "sunday"})
        assert "70% to win (consensus of 3 sportsbooks)" in sent[0][1][0].reasoning and result["status"] == "sent"

    def test_ai_picks_are_used_flagged_and_noted(self, env, monkeypatch):
        sent, store = env
        monkeypatch.setattr(app, "odds_api_key", lambda: None)
        monkeypatch.setattr(app, "USE_AI", True)
        monkeypatch.setattr(
            app, "ai_picks", lambda pool, avail, note: ([app.Pick(game_id="g1", winner="LV", confidence=1, reasoning="r")], "sum")
        )
        app.run_pickem({"run": "sunday"})
        assert any("Picks by" in n for n in sent[0][3]) and sent[0][4] == "sum"
        assert '"ai": true' in store.values["/nfl-pickem/picks/2026-w7"]


class TestLineupHandlerBranches:
    @pytest.fixture
    def env(self, monkeypatch, fake_ssm):
        now = datetime.now(lineup.CENTRAL)
        qb, rb = player(1, "QB", 20, slot=0), player(2, "RB", 15, slot=BENCH)
        for p in (qb, rb):
            p["game"]["kickoff_ct"] = now + timedelta(minutes=60)
        sent = []
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(lineup, "parse_games", lambda sb: [qb["game"]])
        monkeypatch.setattr(lineup, "fetch_league", lambda season, week: ({}, "swid"))
        monkeypatch.setattr(lineup, "my_team", lambda *a: {"name": "T"})
        monkeypatch.setattr(lineup, "lineup_slots", lambda *a: [("QB", 0), ("RB", 2)])
        monkeypatch.setattr(lineup, "players", lambda *a: [qb, rb])
        monkeypatch.setattr(lineup, "suggest_pickups", lambda *a: ([], "", "pickups"))
        monkeypatch.setattr(lineup, "send_email", lambda *a: sent.append(a))
        monkeypatch.setattr(lineup, "USE_AI", False)
        return sent, qb, rb

    def test_notes_say_why_the_ai_did_not_run(self, env, monkeypatch):
        sent, qb, rb = env
        lineup.handler({"run": "sunday"}, None)
        assert "Lineup from ESPN projections (AI off)." in sent[0][5]
        monkeypatch.setattr(
            lineup, "USE_AI", True
        )  # healthy starters and no bench player close behind: nothing for Claude to review
        rb["proj"] = 1.0
        lineup.handler({"run": "sunday"}, None)
        assert any("no close calls" in n for n in sent[1][5])

    def test_an_unfillable_slot_is_called_out(self, env, monkeypatch):
        sent, qb, rb = env
        rb["status"] = "OUT"
        lineup.handler({"run": "sunday"}, None)
        assert any("no healthy eligible player" in n for n in sent[0][5])

    def test_an_ai_review_that_undoes_the_only_change_cancels_the_alert(self, env, monkeypatch):
        sent, qb, rb = env
        rb["proj"] = 18.0
        backup = player(3, "RB", 17.0)  # a bench RB right behind the starter is a close call, so Claude is asked
        backup["game"]["kickoff_ct"] = qb["game"]["kickoff_ct"]
        monkeypatch.setattr(lineup, "players", lambda *a: [qb, rb, backup])
        monkeypatch.setattr(lineup, "USE_AI", True)
        monkeypatch.setattr(lineup, "ai_lineup", lambda roster, slots, proposed, calls: ({"QB": qb, "RB": None}, {}, "keep it"))
        result = lineup.handler({"run": "alert"}, None)
        assert result["status"] == "no changes" and not sent


class TestRecapEdges:
    def test_bad_starts_are_listed(self):
        r = [player(1, "QB", 20, slot=0, actual=2), player(2, "QB", 5, slot=BENCH, actual=30)]
        text = "\n".join(lineup.recap_summary({"name": "T"}, 3, lineup.recap_facts(r, [("QB", 0)], None)))
        assert "Should have started: P2 (30.0)" in text and "Started instead: P1 (2.0)" in text

    def test_week_zero_has_nothing_to_recap(self, monkeypatch):
        monkeypatch.setattr(lineup, "parse_games", lambda s: [{"started": False}])  # week 1 hasn't kicked off yet
        assert lineup.recap_run(2026, {"week": {"number": 1}}, test=False) == {"status": "no week to recap"}

    def test_a_failed_recap_record_does_not_fail_the_run(self, monkeypatch, fake_ssm):
        sb = {"season": {"type": 2, "year": 2026}, "week": {"number": 7}}
        monkeypatch.setattr(lineup, "parse_games", lambda s: [{"id": "g", "started": True}])
        monkeypatch.setattr(lineup, "final_winners", lambda s: {"g": "KC"})
        monkeypatch.setattr(lineup, "fetch_league", lambda *a, **k: ({"teams": [], "schedule": []}, "swid"))
        monkeypatch.setattr(lineup, "my_team", lambda *a: {"id": 1, "name": "T"})
        monkeypatch.setattr(lineup, "lineup_slots", lambda league: SLOTS)
        monkeypatch.setattr(lineup, "players", lambda *a: [player(1, "QB", 20, slot=0, actual=5)])
        monkeypatch.setattr(lineup, "deliver", lambda *a: None)
        monkeypatch.setattr(lineup, "save_param", lambda *a: (_ for _ in ()).throw(OSError("denied")))
        assert lineup.recap_run(2026, sb, test=False)["status"] == "sent"
