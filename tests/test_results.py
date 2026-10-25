"""Results tracking: record what was emailed, score it once the games are final, report it next week."""

import pytest
from conftest import FakeSsm

import app


def pick(gid, winner, points):
    return app.Pick(game_id=gid, winner=winner, confidence=points, reasoning="r")


def final(eid, winner=None, completed=True):
    comp = [{"team": {"abbreviation": a}, "winner": a == winner} for a in ("KC", "LV")]
    return {"id": eid, "status": {"type": {"completed": completed}}, "competitions": [{"competitors": comp}]}


@pytest.fixture
def store(monkeypatch):
    fake = FakeSsm()
    monkeypatch.setattr(app, "ssm", fake)
    return fake


def board(monkeypatch, *events):
    asked = []
    monkeypatch.setattr(app, "fetch", lambda path: asked.append(path) or {"events": list(events)})
    return asked


class TestRecordPicks:
    def test_thursday_and_sunday_add_to_the_same_week(self, store):
        app.record_picks(2026, 7, [pick("g1", "KC", 3)], ai=False)
        app.record_picks(2026, 7, [pick("g2", "LV", 2)], ai=True)
        rec = app.load_param("/nfl-pickem/picks/2026-w7", None)
        assert rec["picks"] == {
            "g1": {"winner": "KC", "confidence": 3, "ai": False},
            "g2": {"winner": "LV", "confidence": 2, "ai": True},
        }


class TestFinalWinners:
    def test_only_finished_games_and_ties_are_none(self):
        sb = {"events": [final("1", "KC"), final("2", None), final("3", "LV", completed=False)]}
        assert app.final_winners(sb) == {"1": "KC", "2": None}


class TestScoreWeek:
    def saved(self, store):
        app.record_picks(2026, 6, [pick("1", "KC", 3), pick("2", "KC", 2), pick("3", "KC", 1)], ai=False)

    def test_scores_correct_picks_by_confidence_points(self, store, monkeypatch):
        self.saved(store)
        asked = board(monkeypatch, final("1", "KC"), final("2", "LV"), final("3", "KC"))
        app.score_week(2026, 6)
        assert app.load_param("/nfl-pickem/results/2026", {})["6"] == {
            "correct": 2,
            "total": 3,
            "points": 4,
            "possible": 6,
            "ai": {"correct": 0, "total": 0},
        }
        assert "week=6" in asked[0] and "seasontype=2" in asked[0] and "dates=2026" in asked[0]

    def test_a_tie_counts_as_wrong(self, store, monkeypatch):
        self.saved(store)
        board(monkeypatch, final("1", None), final("2", None), final("3", None))
        app.score_week(2026, 6)
        assert app.load_param("/nfl-pickem/results/2026", {})["6"]["correct"] == 0

    def test_waits_until_every_picked_game_is_final(self, store, monkeypatch):
        self.saved(store)
        board(monkeypatch, final("1", "KC"), final("2", "KC"), final("3", "KC", completed=False))
        app.score_week(2026, 6)
        assert app.load_param("/nfl-pickem/results/2026", {}) == {}

    def test_mixed_week_counts_ai_picks_separately(self, store, monkeypatch):
        app.record_picks(2026, 6, [pick("1", "KC", 3)], ai=True)
        app.record_picks(2026, 6, [pick("2", "KC", 2)], ai=False)
        board(monkeypatch, final("1", "KC"), final("2", "LV"))
        app.score_week(2026, 6)
        week = app.load_param("/nfl-pickem/results/2026", {})["6"]
        assert (week["correct"], week["total"], week["ai"]) == (1, 2, {"correct": 1, "total": 1})

    def test_scores_once_and_ignores_weeks_without_picks(self, store, monkeypatch):
        asked = board(monkeypatch)
        app.score_week(2026, 5)  # nothing recorded
        assert not asked
        self.saved(store)
        board(monkeypatch, final("1", "KC"), final("2", "KC"), final("3", "KC"))
        app.score_week(2026, 6)
        asked = board(monkeypatch)
        app.score_week(2026, 6)  # already scored: no fetch
        assert not asked


class TestRecordLine:
    def put(self, store, **weeks):
        app.save_param("/nfl-pickem/results/2026", {w.removeprefix("w"): r for w, r in weeks.items()})

    def r(self, correct, total, points, possible, ai=(0, 0)):
        return {
            "correct": correct,
            "total": total,
            "points": points,
            "possible": possible,
            "ai": {"correct": ai[0], "total": ai[1]},
        }

    def test_empty_when_the_week_was_not_scored(self, store):
        assert app.record_line(2026, 6) == ""

    def test_single_week(self, store):
        self.put(store, w6=self.r(11, 14, 87, 105))
        assert app.record_line(2026, 6) == "Week 6: 11/14 correct, 87/105 points."

    def test_adds_season_total_and_ai_vs_odds_when_both_exist(self, store):
        self.put(store, w5=self.r(10, 14, 70, 105, ai=(10, 14)), w6=self.r(11, 14, 87, 105))
        assert app.record_line(2026, 6) == (
            "Week 6: 11/14 correct, 87/105 points. Season (2 scored weeks): 21/28 correct, 157/210 points. "
            "Odds picks: 11/14 correct; AI picks: 10/14 correct."
        )

    def test_last_week_record_scores_then_reports_and_never_raises(self, store, monkeypatch):
        app.record_picks(2026, 6, [pick("1", "KC", 1)], ai=False)
        board(monkeypatch, final("1", "KC"))
        assert app.last_week_record(2026, 7) == "Week 6: 1/1 correct, 1/1 points."
        monkeypatch.setattr(app, "score_week", lambda *a: (_ for _ in ()).throw(OSError("espn down")))
        assert app.last_week_record(2026, 7) == ""

    def test_test_runs_report_but_do_not_score(self, store, monkeypatch):
        app.record_picks(2026, 6, [pick("1", "KC", 1)], ai=False)
        asked = board(monkeypatch, final("1", "KC"))
        assert app.last_week_record(2026, 7, score=False) == "" and not asked


def test_email_shows_the_record_first(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "deliver", lambda *a: sent.append(a))
    g = {"id": "g1", "home": "KC", "away": "LV", "line": "KC -3"}
    from datetime import UTC, datetime

    g["kickoff_ct"] = datetime(2026, 10, 25, 12, tzinfo=UTC)
    app.send_email("S", [pick("g1", "KC", 1)], [g], [], "", record="Week 6: 1/1 <ok>")
    _, body, text = sent[0]
    assert "<b>Week 6: 1/1 &lt;ok&gt;</b>" in body and text.startswith("Week 6: 1/1 <ok>")
