"""Fantasy lineup logic: slot assignment, close calls, AI validation, moves and pickups."""

from datetime import UTC, datetime, timedelta

import pytest

import lineup
from lineup import BENCH, IR

SLOTS = [("QB", 0), ("RB1", 2), ("RB2", 2), ("WR1", 4), ("FLEX", 23), ("D/ST", 16)]
FLEX_OK = {23, 2, 4, 6}


def player(pid, pos, proj, slot=BENCH, status="ACTIVE", game=True, locked=False, eligible=None):
    elig = {"QB": {0, 7}, "RB": {2, 3, 23, 7}, "WR": {4, 3, 5, 23, 7}, "D/ST": {16}}[pos] if eligible is None else eligible
    kickoff = datetime(2026, 10, 25, 12, tzinfo=UTC)
    g = {"kickoff_ct": kickoff, "started": locked, "line": "-3", "over_under": 44} if game else None
    return {
        "id": pid,
        "name": f"P{pid}",
        "pos": pos,
        "eligible": elig,
        "slot": slot,
        "status": status,
        "proj": proj,
        "game": g,
        "team": "KC" if game else "",
        "opp": "vs LV" if game else "BYE",
        "locked": locked,
    }


def names(lu):
    return {label: (p["name"] if p else None) for label, p in lu.items()}


class TestBestLineup:
    def test_highest_projections_fill_slots_with_flex_last(self):
        roster = [
            player(1, "QB", 20),
            player(2, "RB", 15),
            player(3, "RB", 14),
            player(4, "RB", 13),
            player(5, "WR", 12),
            player(6, "WR", 16),
            player(7, "D/ST", 8),
        ]
        lu = lineup.best_lineup(roster, SLOTS)
        assert names(lu) == {"QB": "P1", "RB1": "P2", "RB2": "P3", "WR1": "P6", "FLEX": "P4", "D/ST": "P7"}

    def test_out_and_bye_players_never_start(self):
        roster = [
            player(1, "QB", 20),
            player(2, "QB", 10),
            player(3, "D/ST", 8),
            player(4, "RB", 30, status="OUT"),
            player(5, "RB", 30, game=False),
            player(6, "RB", 5),
        ]
        lu = lineup.best_lineup(roster, SLOTS)
        assert lu["QB"]["name"] == "P1" and lu["RB1"]["name"] == "P6"
        assert lu["RB2"] is None  # nobody healthy left

    def test_questionable_still_plays_but_doubtful_does_not(self):
        roster = [player(1, "QB", 20, status="DOUBTFUL"), player(2, "QB", 5, status="QUESTIONABLE")]
        assert lineup.best_lineup(roster, SLOTS)["QB"]["name"] == "P2"

    def test_locked_starter_stays_and_locked_bencher_cannot_enter(self):
        roster = [
            player(1, "QB", 5, slot=0, locked=True),
            player(2, "QB", 30),
            player(3, "WR", 40, slot=BENCH, locked=True),
            player(4, "WR", 9),
        ]
        lu = lineup.best_lineup(roster, SLOTS)
        assert lu["QB"]["name"] == "P1"
        assert lu["WR1"]["name"] == "P4"

    def test_restrictive_slot_filled_before_flex(self):
        # Only one WR; WR1 must take him, not FLEX.
        roster = [player(1, "WR", 10), player(2, "RB", 9), player(3, "RB", 8), player(4, "RB", 7)]
        lu = lineup.best_lineup(roster, SLOTS)
        assert lu["WR1"]["name"] == "P1" and lu["FLEX"]["name"] == "P4"


class TestCloseCalls:
    def test_flags_injury_tag_and_nearby_bench_player(self):
        roster = [player(1, "QB", 20, status="QUESTIONABLE", slot=0), player(2, "QB", 19), player(3, "QB", 5)]
        lu = {"QB": roster[0]}
        calls = lineup.close_calls(roster, lu, [("QB", 0)])
        assert any("questionable" in c for c in calls)
        assert any("P2" in c for c in calls) and not any("P3" in c for c in calls)

    def test_nothing_to_flag_for_healthy_clear_starter(self):
        roster = [player(1, "QB", 30, slot=0), player(2, "QB", 5)]
        assert lineup.close_calls(roster, {"QB": roster[0]}, [("QB", 0)]) == []


class TestValidate:
    def sheet(self, **picks):
        return lineup.LineupSheet(lineup=[lineup.SlotPick(slot=s, player_id=i) for s, i in picks.items()], reasons=[], summary="")

    ROSTER = [
        player(1, "QB", 20, slot=0),
        player(2, "QB", 18),
        player(3, "D/ST", 8, slot=16),
        player(4, "QB", 9, status="OUT"),
        player(5, "QB", 1, game=False),
    ]
    SLOTS2 = [("QB", 0), ("D/ST", 16)]

    def proposed(self):
        return lineup.best_lineup(self.ROSTER, self.SLOTS2)

    def test_accepts_a_valid_swap(self):
        out = lineup.validate(self.sheet(QB=2, **{"D/ST": 3}), self.ROSTER, self.SLOTS2, self.proposed())
        assert out["QB"]["name"] == "P2"

    @pytest.mark.parametrize(
        "picks",
        [
            {"QB": 1},  # slot missing
            {"QB": 3, "D/ST": 3},  # same player twice
            {"QB": 3, "D/ST": 1},  # ineligible slot
            {"QB": 99, "D/ST": 3},  # unknown player
            {"QB": 4, "D/ST": 3},  # injured out
            {"QB": 5, "D/ST": 3},  # bye
        ],
    )
    def test_rejects_invalid_sheets(self, picks):
        with pytest.raises(ValueError):
            lineup.validate(self.sheet(**picks), self.ROSTER, self.SLOTS2, self.proposed())

    def test_rejects_moving_a_locked_player(self):
        roster = [player(1, "QB", 20, slot=0, locked=True), player(2, "QB", 18)]
        slots = [("QB", 0)]
        with pytest.raises(ValueError, match="already started"):
            lineup.validate(self.sheet(QB=2), roster, slots, lineup.best_lineup(roster, slots))


class TestMoves:
    def test_start_and_bench_changes_with_thursday_first(self):
        thu = datetime(2026, 10, 22, 19, tzinfo=UTC)
        roster = [
            player(1, "QB", 20, slot=BENCH),
            player(2, "QB", 10, slot=0),
            player(3, "RB", 9, slot=BENCH),
            player(4, "RB", 3, slot=2, locked=True),
        ]
        roster[0]["game"]["kickoff_ct"] = thu
        lu = {"QB": roster[0], "RB1": roster[2]}
        out = lineup.moves(roster, lu, "thursday")
        assert out[0] == "Start P1 at QB (plays Thursday, do this now)"
        assert "Start P3 at RB1" in out and "Bench P2" in out
        assert not any("P4" in m for m in out)  # locked players can't move

    def test_no_changes_when_lineup_matches(self):
        roster = [player(1, "QB", 20, slot=0), player(2, "QB", 5)]
        assert lineup.moves(roster, {"QB": roster[0]}, "sunday") == []

    def test_ir_player_is_not_a_starter(self):
        roster = [player(1, "QB", 20, slot=IR)]
        assert lineup.moves(roster, {"QB": roster[0]}, "sunday") == ["Start P1 at QB"]


class TestPlayersFromEspn:
    def test_reads_projection_status_and_game(self):
        game = {"home_id": 12, "away_id": 13, "home": "KC", "away": "LV", "started": False}
        team = {
            "roster": {
                "entries": [
                    {
                        "playerId": 7,
                        "lineupSlotId": 0,
                        "playerPoolEntry": {
                            "player": {
                                "fullName": "Pat",
                                "defaultPositionId": 1,
                                "eligibleSlots": [0, 7],
                                "proTeamId": 12,
                                "injuryStatus": "QUESTIONABLE",
                                "stats": [
                                    {"scoringPeriodId": 7, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 21.5},
                                    {"scoringPeriodId": 6, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 99},
                                ],
                            }
                        },
                    },
                    {
                        "playerId": 8,
                        "lineupSlotId": IR,
                        "playerPoolEntry": {
                            "player": {"fullName": "Hurt", "defaultPositionId": 2, "eligibleSlots": [2], "proTeamId": 99}
                        },
                    },
                ]
            }
        }
        a, b = lineup.players(team, [game], 7)
        assert (a["name"], a["pos"], a["proj"], a["status"], a["opp"], a["team"]) == (
            "Pat",
            "QB",
            21.5,
            "QUESTIONABLE",
            "vs LV",
            "KC",
        )
        assert (b["status"], b["opp"], b["proj"]) == ("INJURY_RESERVE", "BYE", 0.0)


class TestEspnStats:
    STATS = [
        {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0, "appliedTotal": 999},
        {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0, "appliedTotal": 120},
        {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 7, "appliedTotal": 14},
    ]

    def test_season_stat_ignores_other_seasons(self):
        p = {"stats": self.STATS}
        assert lineup.season_stat(p, 2026, 1, 0)["appliedTotal"] == 120
        assert lineup.season_stat(p, 2026, 1, 1, 7)["appliedTotal"] == 14
        assert lineup.season_stat(p, 2024, 1, 0) == {}

    def test_outlook_pulls_projection_numbers(self):
        p = {
            "id": 5,
            "fullName": "Wide",
            "defaultPositionId": 3,
            "proTeamId": 12,
            "stats": self.STATS,
            "ownership": {"percentOwned": 40.5},
        }
        o = lineup.outlook(p, 2026, 7, {12: ("KC", 10)}, started={12})
        assert (o["team"], o["bye"], o["ros"], o["week"], o["owned"], o["locked"]) == ("KC", 10, 120, 14, 40.5, True)


class TestProjectionPickups:
    def out(self, pid, pos, ros, status="ACTIVE"):
        return {"id": pid, "name": f"P{pid}", "pos": pos, "ros": ros, "status": status, "news": []}

    def test_swaps_same_position_when_gain_clears_threshold(self):
        pool = [self.out(1, "WR", 100), self.out(2, "RB", 60), self.out(3, "WR", 90, status="OUT")]
        mine = [self.out(10, "WR", 80), self.out(11, "RB", 50)]
        moves = lineup.projection_pickups(pool, mine)
        assert [(a["id"], d["id"]) for a, d, _ in moves] == [(1, 10)]  # RB gain of 10 < 15; OUT skipped

    def test_each_drop_used_once_and_capped(self):
        pool = [self.out(i, "WR", 200 - i) for i in range(1, 6)]
        mine = [self.out(10 + i, "WR", 10 + i) for i in range(5)]
        moves = lineup.projection_pickups(pool, mine)
        assert len(moves) == lineup.MAX_PICKUPS
        assert len({d["id"] for _, d, _ in moves}) == len(moves)


class TestAiPickupsValidation:
    def run(self, monkeypatch, pickups):
        monkeypatch.setattr(lineup, "claude_tool_call", lambda *a: {"pickups": pickups, "summary": "s"})
        base = {
            "pos": "WR",
            "team": "KC",
            "bye": 9,
            "status": "ACTIVE",
            "week": 1.0,
            "avg": 1.0,
            "ros": 1.0,
            "ros_avg": 1.0,
            "owned": 1.0,
            "availability": "FREEAGENT",
            "news": [],
        }
        add = {**base, "id": 1, "name": "Add One"}
        drop = {**base, "id": 2, "name": "Drop Two"}
        return lineup.ai_pickups([add], [drop], [drop], [("QB", 0)], 7)

    def pick(self, **kw):
        return {
            "add_player_id": 1,
            "add_player_name": "Add One",
            "drop_player_id": 2,
            "drop_player_name": "Drop Two",
            "reason": "r",
            **kw,
        }

    def test_valid_pickup(self, monkeypatch):
        moves, summary = self.run(monkeypatch, [self.pick()])
        assert moves[0][2] == "r" and summary == "s"

    @pytest.mark.parametrize("bad", [{"add_player_id": 9}, {"drop_player_id": 9}, {"add_player_name": "Someone Else"}])
    def test_rejects_unknown_ids_and_mismatched_names(self, monkeypatch, bad):
        with pytest.raises(ValueError):
            self.run(monkeypatch, [self.pick(**bad)])


class TestAlertGate:
    """The alert run only does work shortly before a kickoff."""

    def handler_result(self, monkeypatch, kickoff_in, **event):
        now = datetime.now(lineup.CENTRAL)
        g = {"kickoff_ct": now + kickoff_in, "started": False}
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(lineup, "parse_games", lambda sb: [g])

        def stop(*a):
            raise RuntimeError("reached the league fetch")

        monkeypatch.setattr(lineup, "fetch_league", stop)
        try:
            return lineup.handler({"run": "alert", **event}, None)
        except RuntimeError as exc:
            return str(exc)

    def test_skips_when_no_game_is_close(self, monkeypatch):
        assert self.handler_result(monkeypatch, timedelta(hours=5))["status"] == "no game soon"

    def test_skips_when_only_started_games(self, monkeypatch):
        assert self.handler_result(monkeypatch, timedelta(minutes=-30))["status"] == "no game soon"

    def test_proceeds_when_a_game_is_within_two_hours(self, monkeypatch):
        assert self.handler_result(monkeypatch, timedelta(minutes=90)) == "reached the league fetch"

    def test_test_events_bypass_the_gate(self, monkeypatch):
        assert self.handler_result(monkeypatch, timedelta(hours=5), test=True) == "reached the league fetch"

    def test_off_season_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 3}})
        assert lineup.handler({"run": "sunday"}, None) == {"status": "off season"}


class TestAlertRun:
    NOW = datetime(2026, 10, 25, 14, tzinfo=lineup.CENTRAL)

    def test_kicks_soon_window(self):
        def g(minutes, started=False):
            return {"kickoff_ct": self.NOW + timedelta(minutes=minutes), "started": started}

        assert lineup.kicks_soon(g(90), self.NOW)
        assert not lineup.kicks_soon(g(180), self.NOW)
        assert not lineup.kicks_soon(g(-5), self.NOW)
        assert not lineup.kicks_soon(g(30, started=True), self.NOW)
        assert not lineup.kicks_soon(None, self.NOW)

    def test_alert_only_reports_players_in_games_starting_soon(self):
        soon = player(1, "QB", 20, slot=BENCH)
        later = player(2, "RB", 20, slot=BENCH)
        soon["game"]["kickoff_ct"] = self.NOW + timedelta(minutes=60)
        later["game"]["kickoff_ct"] = self.NOW + timedelta(hours=6)
        lu = {"QB": soon, "RB1": later}
        assert lineup.moves([soon, later], lu, "alert", self.NOW) == ["Start P1 at QB"]
        assert len(lineup.moves([soon, later], lu, "sunday")) == 2

    def test_unknown_run_is_rejected_before_any_work(self):
        with pytest.raises(ValueError, match="unknown run"):
            lineup.handler({"run": "sundya"}, None)


class TestWaiverRun:
    def run(self, monkeypatch, pickups, test=False):
        sent = []
        monkeypatch.setattr(lineup, "suggest_pickups", lambda *a: (pickups, "", "note"))
        monkeypatch.setattr(lineup, "deliver", lambda *a: sent.append(a))
        return lineup.waiver_run(2026, 7, {"name": "Team"}, [], test=test), sent

    def test_silent_without_pickups(self, monkeypatch):
        result, sent = self.run(monkeypatch, [])
        assert result["status"] == "no pickups" and not sent

    def test_emails_pickups_escaped(self, monkeypatch):
        add = {"name": "A<b>", "pos": "WR", "team": "KC", "ros_avg": 9.0, "availability": "FREEAGENT"}
        drop = {"name": "D", "ros_avg": 3.0}
        result, sent = self.run(monkeypatch, [(add, drop, "why & more")])
        subject, body, _ = sent[0]
        assert result["status"] == "sent" and "Waiver Pickups" in subject
        assert "A&lt;b&gt;" in body and "why &amp; more" in body

    def test_test_event_sends_even_when_empty(self, monkeypatch):
        result, sent = self.run(monkeypatch, [], test=True)
        assert result["status"] == "sent" and sent

    def test_handler_looks_ahead_when_espn_shows_the_finished_week(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(lineup, "fetch", lambda path: {"season": {"type": 2, "year": 2026}, "week": {"number": 7}})
        monkeypatch.setattr(lineup, "parse_games", lambda sb: [{"started": True}])
        monkeypatch.setattr(lineup, "fetch_league", lambda season, week: seen.setdefault("week", week) and ({}, "swid"))
        monkeypatch.setattr(lineup, "my_team", lambda *a: {})
        monkeypatch.setattr(lineup, "lineup_slots", lambda *a: [])
        monkeypatch.setattr(lineup, "waiver_run", lambda *a: {"status": "ok"})
        lineup.handler({"run": "waivers"}, None)
        assert seen["week"] == 8
