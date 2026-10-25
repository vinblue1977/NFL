"""Tuesday recap: actual points vs projections, the best lineup in hindsight, and the matchup result."""

import pytest
from test_lineup import SLOTS, player

import lineup
from lineup import BENCH, IR


def roster():
    return [
        player(1, "QB", 20, slot=0, actual=12),  # started, scored little
        player(2, "RB", 15, slot=2, actual=18),
        player(3, "RB", 14, slot=2, actual=4),  # started, flopped
        player(4, "WR", 12, slot=4, actual=10),
        player(5, "RB", 9, slot=BENCH, actual=26),  # the one that got away
        player(6, "D/ST", 8, slot=16, actual=7),
        player(7, "WR", 30, slot=IR, actual=40),  # IR can't be started in hindsight
    ]


class TestHindsight:
    def test_best_lineup_by_actual_points_and_ir_excluded(self):
        names = {label: p["name"] for label, p in lineup.hindsight(roster(), SLOTS).items()}
        assert names == {"QB": "P1", "RB1": "P5", "RB2": "P2", "WR1": "P4", "FLEX": "P3", "D/ST": "P6"}

    def test_a_player_on_bye_who_scored_nothing_cannot_be_in_the_best_lineup(self):
        r = roster()
        r[4]["game"], r[4]["actual"] = None, 0.0  # on bye
        assert lineup.hindsight(r, SLOTS)["RB1"]["name"] == "P2"


class TestPlayersActual:
    def test_reads_the_weekly_actual_not_the_projection_or_season_total(self):
        stats = [
            {"scoringPeriodId": 7, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 21.5},  # projection
            {"scoringPeriodId": 7, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": 17.2},  # actual
            {"scoringPeriodId": 0, "statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": 99.0},  # season total
            {"scoringPeriodId": 6, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": 3.0},  # another week
        ]
        entry = {
            "playerId": 1,
            "lineupSlotId": 0,
            "playerPoolEntry": {
                "player": {"fullName": "Pat", "defaultPositionId": 1, "eligibleSlots": [0], "proTeamId": 12, "stats": stats}
            },
        }
        (p,) = lineup.players({"roster": {"entries": [entry]}}, [], 7)
        assert (p["proj"], p["actual"]) == (21.5, 17.2)


class TestMatchupResult:
    TEAMS = [{"id": 1, "name": "Rockers"}, {"id": 2, "name": "Rivals"}]

    def sched(self, **kw):
        return {"teams": self.TEAMS, "schedule": [{"matchupPeriodId": 7, **kw}]}

    def test_home_and_away_sides(self):
        league = self.sched(home={"teamId": 1, "totalPoints": 110.5}, away={"teamId": 2, "totalPoints": 99})
        assert lineup.matchup_result(league, {"id": 1}, 7) == (110.5, "Rivals", 99)
        assert lineup.matchup_result(league, {"id": 2}, 7) == (99, "Rockers", 110.5)

    @pytest.mark.parametrize(
        "league",
        [
            {"teams": [], "schedule": []},
            {
                "teams": [],
                "schedule": [
                    {"matchupPeriodId": 6, "home": {"teamId": 1, "totalPoints": 1}, "away": {"teamId": 2, "totalPoints": 2}}
                ],
            },
            {"teams": [], "schedule": [{"matchupPeriodId": 7, "home": {"teamId": 1, "totalPoints": 1}}]},  # bye
            {"teams": [], "schedule": [{"matchupPeriodId": 7, "home": {"teamId": 1}, "away": {"teamId": 2}}]},  # not scored yet
            {  # ESPN hasn't tallied the matchup yet (seen live mid-week: 0 to 0 while players had scored)
                "teams": [],
                "schedule": [
                    {"matchupPeriodId": 7, "home": {"teamId": 1, "totalPoints": 0.0}, "away": {"teamId": 2, "totalPoints": 0.0}}
                ],
            },
        ],
    )
    def test_none_when_there_is_nothing_to_report(self, league):
        assert lineup.matchup_result(league, {"id": 1}, 7) is None


class TestRecapWeek:
    def sb(self, week, *started):
        return {"week": {"number": week}, "events": started}

    def test_current_week_unless_espn_already_rolled_over(self, monkeypatch):
        monkeypatch.setattr(lineup, "parse_games", lambda sb: [{"started": s} for s in sb["events"]])
        assert lineup.recap_week(self.sb(7, True, True)) == 7  # finished games: still the week that just ended
        assert lineup.recap_week(self.sb(8, False, False)) == 7  # rolled to next week, nothing started yet
        assert lineup.recap_week(self.sb(8)) == 8  # no games to go by


class TestRecapContent:
    def facts(self, result=None):
        return lineup.recap_facts(roster(), SLOTS, result)

    def test_totals_misses_and_order(self):
        f = self.facts()
        assert f["scored"] == 51 and f["projected"] == 69 and f["best"] == 77
        assert [p["name"] for p in f["bench_misses"]] == ["P5"]
        assert [p["name"] for p in f["bad_starts"]] == []  # the RB flopped but still makes the best lineup at FLEX
        assert [p["name"] for p in f["starters"]][:2] == ["P1", "P2"]  # lineup order: QB first

    def test_summary_mentions_result_gap_and_names(self):
        text = "\n".join(lineup.recap_summary({"name": "Rockers"}, 7, self.facts((110.5, "Rivals", 99))))
        assert "Rockers: Week 7 recap" in text and "Won 110.5 to 99 against Rivals." in text
        assert "scored 51.0 (ESPN projected 69.0)" in text and "26.0 points were left on the bench" in text
        assert "Should have started: P5 (26.0)" in text

    def test_a_perfect_week_says_so_and_a_loss_says_lost(self):
        r = [player(1, "QB", 10, slot=0, actual=30)]
        f = lineup.recap_facts(r, [("QB", 0)], (80, "Rivals", 90))
        text = "\n".join(lineup.recap_summary({"name": "T"}, 3, f))
        assert "best lineup you could have fielded" in text and "Lost 80 to 90" in text

    def test_email_escapes_and_has_both_parts(self):
        r = roster()
        r[0]["name"] = "<b>Pat</b>"
        body, text = lineup.recap_email({"name": "A & B"}, 7, lineup.recap_facts(r, SLOTS, None))
        assert "&lt;b&gt;Pat&lt;/b&gt;" in body and "A &amp; B" in body and "<table" in body
        assert text.startswith("A & B: Week 7 recap") and "STARTERS" in text and "<b>Pat</b>" in text


class TestRecapRun:
    @pytest.fixture
    def env(self, monkeypatch):
        sent, asked = [], []
        sb = {"season": {"type": 2, "year": 2026}, "week": {"number": 7}}
        games = [{"id": "g1", "started": True}, {"id": "g2", "started": True}]
        monkeypatch.setattr(lineup, "fetch", lambda path: asked.append(path) or sb)
        monkeypatch.setattr(lineup, "parse_games", lambda s: games)
        monkeypatch.setattr(lineup, "final_winners", lambda s: {"g1": "KC", "g2": "LV"})
        monkeypatch.setattr(
            lineup,
            "fetch_league",
            lambda season, week, matchups=False: ({"teams": [], "schedule": [], "w": week, "m": matchups}, "swid"),
        )
        monkeypatch.setattr(lineup, "my_team", lambda *a: {"id": 1, "name": "T"})
        monkeypatch.setattr(lineup, "lineup_slots", lambda league: SLOTS)
        monkeypatch.setattr(lineup, "players", lambda *a: roster())
        monkeypatch.setattr(lineup, "deliver", lambda *a: sent.append(a))
        return sb, games, sent, asked

    def test_sends_the_recap_once_every_game_is_final(self, env):
        sb, _, sent, _ = env
        result = lineup.recap_run(2026, sb, test=False)
        assert result["status"] == "sent" and result["scored"] == 51 and "Fantasy Week 7 Recap" in sent[0][0]

    def test_waits_while_any_game_is_still_going(self, env, monkeypatch):
        sb, _, sent, _ = env
        monkeypatch.setattr(lineup, "final_winners", lambda s: {"g1": "KC"})
        assert lineup.recap_run(2026, sb, test=False)["status"] == "week not over" and not sent
        assert lineup.recap_run(2026, sb, test=True)["status"] == "sent" and sent[0][0].startswith("[TEST]")

    def test_after_rollover_it_fetches_last_weeks_scoreboard(self, env):
        sb, games, sent, asked = env
        sb["week"]["number"] = 8
        for g in games:
            g["started"] = False
        assert lineup.recap_run(2026, sb, test=False)["week"] == 7
        assert "week=7" in asked[0] and "dates=2026" in asked[0] and "seasontype=2" in asked[0]

    def test_handler_routes_the_recap_run_and_records_its_heartbeat(self, env, fake_ssm):
        assert lineup.handler({"run": "recap"}, None)["status"] == "sent"
        assert "/nfl-pickem/heartbeat/nfl-fantasy-lineup" in fake_ssm.values

    def test_off_season_sends_no_recap(self, env, monkeypatch):
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 3}})
        assert lineup.handler({"run": "recap"}, None) == {"status": "off season"}


class TestFetchLeagueViews:
    def test_matchup_scores_are_only_requested_for_the_recap(self, monkeypatch):
        urls = []
        monkeypatch.setattr(lineup, "fantasy_get", lambda path, f=None: urls.append(path) or {})
        monkeypatch.setattr(lineup, "secret", lambda name: "swid")
        lineup.fetch_league(2026, 7)
        lineup.fetch_league(2026, 7, matchups=True)
        assert "mMatchupScore" not in urls[0] and "mMatchupScore" in urls[1] and "scoringPeriodId=7" in urls[1]


class TestRecapReview:
    def test_double_header_leagues_map_weeks_to_matchup_periods(self):
        league = {
            "settings": {"scheduleSettings": {"matchupPeriods": {"16": [16, 17]}}},
            "teams": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            "schedule": [
                {"matchupPeriodId": 16, "home": {"teamId": 1, "totalPoints": 5}, "away": {"teamId": 2, "totalPoints": 3}}
            ],
        }
        assert lineup.matchup_result(league, {"id": 1}, 17) == (5, "B", 3)
        assert lineup.matchup_periods({}, 7) == {7}

    def test_missing_week_number_is_not_a_crash(self):
        assert lineup.recap_week({"events": []}) is None

    def test_missing_projections_are_left_out_not_shown_as_zero(self):
        r = [player(1, "QB", 0, slot=0, actual=20)]
        f = lineup.recap_facts(r, [("QB", 0)], None)
        assert f["projected"] is None
        assert "projected" not in "\n".join(lineup.recap_summary({"name": "T"}, 3, f))
        assert lineup.recap_rows(f)[0][3] == "-"

    def test_a_player_who_scored_counts_even_if_his_game_went_missing(self):
        r = [player(1, "QB", 5, slot=BENCH, game=False, actual=30), player(2, "QB", 20, slot=0, actual=10)]
        best = lineup.hindsight(r, [("QB", 0)])
        assert best["QB"]["name"] == "P1"

    def test_best_is_never_below_what_was_scored(self):
        r = [player(1, "QB", 20, slot=0, actual=10, game=False)]  # lineup slot with a missing game
        assert lineup.recap_facts(r, [("QB", 0)], None)["best"] >= 10


class TestRecapOnce:
    def test_a_week_is_recapped_once_and_test_runs_leave_no_trace(self, monkeypatch, fake_ssm):
        sent = []
        sb = {"season": {"type": 2, "year": 2026}, "week": {"number": 7}}
        monkeypatch.setattr(lineup, "parse_games", lambda s: [{"id": "g", "started": True}])
        monkeypatch.setattr(lineup, "final_winners", lambda s: {"g": "KC"})
        monkeypatch.setattr(lineup, "fetch_league", lambda *a, **k: ({"teams": [], "schedule": []}, "swid"))
        monkeypatch.setattr(lineup, "my_team", lambda *a: {"id": 1, "name": "T"})
        monkeypatch.setattr(lineup, "lineup_slots", lambda league: SLOTS)
        monkeypatch.setattr(lineup, "players", lambda *a: roster())
        monkeypatch.setattr(lineup, "deliver", lambda *a: sent.append(a))
        lineup.recap_run(2026, sb, test=True)
        assert not fake_ssm.values
        assert lineup.recap_run(2026, sb, test=False)["status"] == "sent"
        assert lineup.recap_run(2026, sb, test=False)["status"] == "already sent"
        assert len(sent) == 2  # the test email plus the one real one


class TestMatchupVerdict:
    def league(self, **match):
        sides = {"home": {"teamId": 1, "totalPoints": 0.0}, "away": {"teamId": 2, "totalPoints": 0.0}}
        return {"teams": [], "schedule": [{"matchupPeriodId": 7, **{**sides, **match}}]}

    def test_an_undecided_matchup_is_not_reported_even_with_partial_points(self):
        league = self.league(winner="UNDECIDED", home={"teamId": 1, "totalPoints": 12.4})
        assert lineup.matchup_result(league, {"id": 1}, 7) is None

    def test_a_decided_zero_to_zero_is_a_real_tie(self):
        assert lineup.matchup_result(self.league(winner="TIE"), {"id": 1}, 7) == (0.0, "your opponent", 0.0)

    def test_null_totals_are_not_reported(self):
        league = self.league(home={"teamId": 1, "totalPoints": None})
        assert lineup.matchup_result(league, {"id": 1}, 7) is None

    def test_a_later_row_for_a_doubled_week_is_used_when_the_first_is_not_tallied(self):
        scored = {
            "matchupPeriodId": 16,
            "home": {"teamId": 1, "totalPoints": 5.0},
            "away": {"teamId": 2, "totalPoints": 3.0},
            "winner": "HOME",
        }
        league = {"settings": {"scheduleSettings": {"matchupPeriods": {"15": [16], "16": [16]}}}, "teams": []}
        league["schedule"] = [{**scored, "matchupPeriodId": 15, "winner": "UNDECIDED"}, scored]
        assert lineup.matchup_result(league, {"id": 1}, 16) == (5.0, "your opponent", 3.0)
