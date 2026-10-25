"""Weekly housekeeping of the per-week SSM records."""

import app

WEEKLY = ("/nfl-pickem/locked", "/nfl-pickem/picks", "/nfl-pickem/alerts")


def seed(fake_ssm, *weeks):
    for season, week in weeks:
        for prefix in WEEKLY:
            fake_ssm.values[f"{prefix}/{season}-w{week}"] = "x"


class TestPruneState:
    def test_keeps_this_week_and_last_and_deletes_older(self, fake_ssm):
        seed(fake_ssm, (2026, 4), (2026, 5), (2026, 6), (2026, 7))
        app.prune_state(2026, 7)
        assert sorted({n.rsplit("/", 1)[1] for n in fake_ssm.values}) == ["2026-w6", "2026-w7"]

    def test_deletes_earlier_seasons_whatever_their_week(self, fake_ssm):
        seed(fake_ssm, (2025, 18), (2026, 1))
        app.prune_state(2026, 1)
        assert all("2025" not in n for n in fake_ssm.values) and len(fake_ssm.values) == 3

    def test_leaves_everything_else_alone(self, fake_ssm):
        keep = {
            "/nfl-pickem/results/2026": "r",
            "/nfl-pickem/heartbeat/nfl-pickem": "h",
            "/nfl-pickem/cookie-alert": "c",
            "/nfl-pickem/espn-s2": "secret",
            "/nfl-pickem/locked/notaweek": "odd",
        }
        fake_ssm.values.update(keep)
        seed(fake_ssm, (2026, 1))
        app.prune_state(2026, 9)
        assert fake_ssm.values == keep

    def test_deletes_in_batches_of_ten(self, fake_ssm):
        seed(fake_ssm, *[(2024, w) for w in range(1, 19)])  # 54 old records
        app.prune_state(2026, 5)
        assert not fake_ssm.values

    def test_nothing_to_do_in_week_one(self, fake_ssm):
        seed(fake_ssm, (2026, 1))
        app.prune_state(2026, 1)
        assert len(fake_ssm.values) == 3

    def test_one_prefix_failing_does_not_stop_the_others(self, fake_ssm, monkeypatch):
        seed(fake_ssm, (2026, 1))
        real = fake_ssm.delete_parameters

        def flaky(Names):
            if Names[0].startswith("/nfl-pickem/locked"):
                raise OSError("denied")
            real(Names)

        monkeypatch.setattr(fake_ssm, "delete_parameters", flaky)
        app.prune_state(2026, 9)
        assert sorted(fake_ssm.values) == ["/nfl-pickem/locked/2026-w1"]

    def test_names_that_only_look_like_weeks_are_left_alone(self, fake_ssm):
        fake_ssm.values["/nfl-pickem/picks/2026-w1-copy"] = "x"
        fake_ssm.values["/nfl-pickem/picks/x2026-w1"] = "x"
        app.prune_state(2026, 9)
        assert len(fake_ssm.values) == 2
