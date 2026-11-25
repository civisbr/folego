import json

import pytest

from folego.evaluate.backtest_report import backtest_report, summarize


def _fold(year, mae, baseline_mae, hits=1, predicted=2, observed=2):
    peaks = {"precision": None, "recall": None, "hits": hits, "predicted": predicted}
    return {
        "year": year,
        "weeks": 17,
        "mae": mae,
        "mape": mae * 2,
        "observed_peaks": observed,
        "peaks": peaks,
        "baseline": {"mae": baseline_mae, "mape": baseline_mae * 2, "peaks": {**peaks, "hits": 0}},
    }


def _report(region, folds, **overrides):
    report = {
        "region": region,
        "exposure": "pm25",
        "meteorology": "ERA5",
        "exposure_source": "CAMS",
        "design": "rolling",
        "backtest_inputs": "observed",
        "inputs": {"panel": [{"sha256": "abc"}]},
        "basis": {"max_lag": 7, "reference": 15.0, "knots": [15.0, 35.0]},
        "software": {"git_commit": "c0ffee0", "git_dirty": False},
        "results": [{"outcome": "respiratory", "age_group": "all", "folds": folds}],
    }
    report.update(overrides)
    return report


def test_summary_pools_peaks_and_compares_with_baseline():
    reports = [
        _report("11001", [_fold(2019, 8, 10), _fold(2020, 12, 10)]),
        _report(
            "11002",
            [
                _fold(2019, 5, 10),
                {"year": 2020, "error": "short"},
                {**_fold(2021, 1, 1), "mae": None},
            ],
            basis={"max_lag": 7, "reference": 15.0, "knots": [15.0]},
        ),
        _report(
            "11003",
            [],
            results=[{"outcome": "respiratory", "age_group": "all", "error": "no rows"}],
        ),
    ]
    (entry,) = summarize(reports)["results"]
    assert entry["regions"] == 2 and entry["folds"] == 3
    assert entry["fold_errors"] == 2 and entry["stratum_errors"] == 1
    assert entry["median_mae_ratio"] == pytest.approx(0.8)
    assert entry["share_folds_pm25_better"] == pytest.approx(2 / 3)
    assert entry["peaks"] == {"precision": 0.5, "recall": 0.5, "hits": 3}
    assert entry["peaks_baseline"]["hits"] == 0
    assert [y["year"] for y in entry["by_year"]] == [2019, 2020]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"meteorology": "MERRA-2"}, "meteorology"),
        ({"inputs": {"panel": [{"sha256": "other"}]}}, "panel_sha256"),
        ({"basis": {"max_lag": 14, "reference": 15.0, "knots": [15.0]}}, "basis"),
        ({"exposure_source": "MERRA-2"}, "exposure_source"),
        ({"design": "other"}, "design"),
    ],
)
def test_incompatible_backtests_are_refused(override, message):
    with pytest.raises(ValueError, match=message):
        summarize([_report("11001", []), _report("11002", [], **override)])


def test_duplicate_region_is_refused():
    with pytest.raises(ValueError, match="once"):
        summarize([_report("11001", []), _report("11001", [])])


def test_report_records_inputs_and_refuses_overwrite(tmp_path):
    path = tmp_path / "11001-backtest.json"
    path.write_text(json.dumps(_report("11001", [_fold(2019, 8, 10)])))
    report = backtest_report([path], tmp_path / "out" / "summary.json")
    assert report["inputs"][0]["sha256"] and report["any_region_dirty"] is False
    assert (tmp_path / "out" / "history").is_dir()
    with pytest.raises(ValueError, match="overwrite"):
        backtest_report([path], path)


def test_dirty_region_is_flagged():
    report = summarize(
        [_report("11001", [], software={"git_commit": "c0ffee0", "git_dirty": True})]
    )
    assert report["any_region_dirty"] is True
    assert report["dirty_check_includes_untracked"] is False


def test_rolling_backtest_threshold_uses_complete_training_weeks_only():
    from datetime import date, timedelta

    import polars as pl

    from folego.evaluate.backtest import rolling_backtest

    start = date(2018, 1, 1)
    days = [start + timedelta(days=i) for i in range((date(2019, 10, 31) - start).days + 1)]
    # Training weeks hold 7 admissions a day; the test season doubles them.
    counts = [14 if d >= date(2019, 7, 1) else 1 for d in days]
    frame = pl.DataFrame({"date": days, "admissions": counts})

    def perfect(train, test):
        assert train["date"].max() < test["date"].min()
        return [14.0] * test.height

    def flat(train, test):
        return [1.0] * test.height

    (fold,) = rolling_backtest(frame, perfect, [2019], baseline=flat)
    assert fold["peak_threshold"] == 7
    assert fold["observed_peaks"] == fold["weeks"]
    assert fold["peaks"]["recall"] == 1 and fold["baseline"]["peaks"]["hits"] == 0
    assert fold["mae"] == 0 and fold["baseline"]["mae"] == 7 * 13
