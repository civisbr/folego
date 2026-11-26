import json
import statistics
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from test_study import synthetic_panel

from folego.evaluate.backtest_report import backtest_report
from folego.evaluate.study import region_forecast_backtest
from folego.models.crossbasis import CrossBasis


def _forecasts(panel_path, path, *, noise=0.0, scale=1.0, seed=3):
    """Forecasts issued every day: the observed PM2.5 of each target day, perturbed."""
    rng = np.random.default_rng(seed)
    observed = pl.read_parquet(panel_path).group_by("date").agg(pl.col("pm25").first()).sort("date")
    level = dict(zip(observed["date"].to_list(), observed["pm25"].to_list(), strict=True))
    rows = []
    for issue in observed["date"].to_list():
        for lead in range(5):
            day = issue + timedelta(days=lead)
            if day in level:
                value = (
                    level[day] * scale * float(rng.lognormal(0, noise))
                    if noise
                    else level[day] * scale
                )
                rows.append((issue, day, lead, "11004", value))
    frame = pl.DataFrame(
        rows,
        schema=["issue_date", "date", "lead_day", "health_region_code", "pm25_forecast"],
        orient="row",
    )
    frame.write_parquet(path)
    return path


def _cams_panel(path):
    panel = synthetic_panel(path)
    manifest = {"inputs": {"pm25": [{"source": {"attribution": ["Atmosphere Monitoring"]}}]}}
    panel.with_suffix(".parquet.source.json").write_text(json.dumps(manifest))
    return panel


def _run(tmp_path, **forecast_options):
    panel = _cams_panel(tmp_path / "panel.parquet")
    forecasts = _forecasts(panel, tmp_path / "forecasts.parquet", **forecast_options)
    output = tmp_path / "fbt" / "11004-forecast-backtest.json"
    report = region_forecast_backtest(
        panel,
        forecasts,
        "11004",
        output,
        outcomes=("respiratory", "negative_control"),
        basis=CrossBasis(knots=(15, 35)),
    )
    return report, output


def _good(report, outcome="respiratory"):
    folds = next(r for r in report["results"] if r["outcome"] == outcome)["folds"]
    return [f for f in folds if "error" not in f]


def test_perfect_forecast_matches_the_observed_oracle(tmp_path):
    report, output = _run(tmp_path)
    good = _good(report)
    assert good, report["results"]
    for fold in good:
        assert fold["forecast"]["mae"] == pytest.approx(fold["observed"]["mae"])
        assert [lead["lead_day"] for lead in fold["by_lead"]] == [0, 1, 2, 3, 4]
    # Every issue day of 1 Jul-31 Oct, except where admissions are not yet certified.
    assert {f["year"]: f["issue_days"] for f in good}[2021] == 123
    # The synthetic admissions follow PM2.5, so the oracle beats the model without it.
    assert statistics.median(f["observed"]["mae"] / f["baseline"]["mae"] for f in good) < 1
    error = {(e["season"], e["lead_day"]): e for e in report["exposure_error"]}
    assert error[("all", 0)]["bias"] == pytest.approx(0) and error[("all", 4)]["mae"] == 0
    saved = json.loads(output.read_text())
    assert saved["backtest_inputs"].startswith("archived CAMS")
    assert len(saved["inputs"]["forecasts"][0]["sha256"]) == 64


def test_noisy_forecast_costs_accuracy_and_is_summarized(tmp_path):
    report, output = _run(tmp_path, noise=0.6)
    good = _good(report)
    assert statistics.median(f["forecast"]["mae"] / f["observed"]["mae"] for f in good) > 1
    summary = backtest_report([output], tmp_path / "fbt" / "summary.json", forecast=True)
    respiratory = next(r for r in summary["results"] if r["outcome"] == "respiratory")
    assert respiratory["regions"] == 1 and respiratory["median_mae_ratio_forecast_observed"] > 1
    assert len(respiratory["by_lead"]) == 5
    assert len(summary["forecasts_sha256"]) == 1
    assert {e["season"] for e in summary["exposure_error"]} == {"all", "jul_oct"}


def test_biased_forecast_shows_in_exposure_error(tmp_path):
    report, _ = _run(tmp_path, scale=0.5)
    error = {(e["season"], e["lead_day"]): e for e in report["exposure_error"]}
    assert error[("all", 2)]["bias"] < 0
    assert error[("all", 2)]["correlation"] == pytest.approx(1)


def test_issue_without_all_leads_is_skipped(tmp_path):
    panel = _cams_panel(tmp_path / "panel.parquet")
    path = _forecasts(panel, tmp_path / "forecasts.parquet")
    frame = pl.read_parquet(path)
    # Drop lead day 4 of every July issue of 2020: those issue days cannot be scored.
    frame = frame.filter(
        ~(
            (pl.col("lead_day") == 4)
            & (pl.col("issue_date").dt.year() == 2020)
            & (pl.col("issue_date").dt.month() == 7)
        )
    )
    frame.write_parquet(path)
    report = region_forecast_backtest(
        panel,
        path,
        "11004",
        tmp_path / "out.json",
        outcomes=("respiratory",),
        basis=CrossBasis(knots=(15, 35)),
    )
    folds = {f["year"]: f for f in _good(report)}
    assert folds[2020]["issue_days"] == folds[2021]["issue_days"] - 31


def test_region_missing_from_forecasts_fails(tmp_path):
    panel = _cams_panel(tmp_path / "panel.parquet")
    path = _forecasts(panel, tmp_path / "forecasts.parquet")
    pl.read_parquet(path).with_columns(pl.lit("11001").alias("health_region_code")).write_parquet(
        path
    )
    with pytest.raises(ValueError, match="not in the forecasts"):
        region_forecast_backtest(panel, path, "11004", tmp_path / "out.json")


def test_summary_refuses_reports_on_different_forecasts(tmp_path):
    from folego.evaluate.backtest_report import summarize_forecast

    report, _ = _run(tmp_path)
    other = json.loads(json.dumps(report))
    other["region"] = "11001"
    other["inputs"]["forecasts"][0]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="forecasts_sha256"):
        summarize_forecast([report, other])
    other["inputs"]["forecasts"] = []
    with pytest.raises(ValueError, match="forecasts sha256"):
        summarize_forecast([report, other])


def test_forecast_variant_never_sees_observed_pm25_from_the_issue_day_on(tmp_path):
    """Changing observed PM2.5 of target days moves only the oracle, never the forecast."""
    panel = _cams_panel(tmp_path / "panel.parquet")
    forecasts = _forecasts(panel, tmp_path / "forecasts.parquet", noise=0.3)
    options = {"outcomes": ("respiratory",), "basis": CrossBasis(knots=(15, 35))}
    before = region_forecast_backtest(panel, forecasts, "11004", tmp_path / "a.json", **options)
    # Only the last day of the 2021 season moves: the lag of no earlier issue reaches it,
    # and it lies past the training window of every fold.
    last = date(2021, 11, 4)
    frame = pl.read_parquet(panel)
    frame.with_columns(
        pl.when(pl.col("date") == last).then(pl.col("pm25") * 5).otherwise(pl.col("pm25"))
    ).write_parquet(panel)
    after = region_forecast_backtest(panel, forecasts, "11004", tmp_path / "b.json", **options)
    fold = {f["year"]: f for f in _good(before)}[2021]
    changed = {f["year"]: f for f in _good(after)}[2021]
    assert changed["forecast"]["mae"] == pytest.approx(fold["forecast"]["mae"])
    assert changed["observed"]["mae"] != pytest.approx(fold["observed"]["mae"])


def test_non_cams_panel_is_refused(tmp_path):
    panel = synthetic_panel(tmp_path / "panel.parquet")
    path = _forecasts(panel, tmp_path / "forecasts.parquet")
    with pytest.raises(ValueError, match="from CAMS"):
        region_forecast_backtest(panel, path, "11004", tmp_path / "out.json")


def test_repeated_issue_and_lead_fails_the_fold(tmp_path):
    panel = _cams_panel(tmp_path / "panel.parquet")
    path = _forecasts(panel, tmp_path / "forecasts.parquet")
    frame = pl.read_parquet(path)
    pl.concat([frame, frame.filter(pl.col("issue_date") == date(2021, 8, 1))]).write_parquet(path)
    report = region_forecast_backtest(
        panel,
        path,
        "11004",
        tmp_path / "o.json",
        outcomes=("respiratory",),
        basis=CrossBasis(knots=(15, 35)),
    )
    folds = {f["year"]: f for f in report["results"][0]["folds"]}
    assert "Repeated" in folds[2021]["error"] and "error" not in folds[2020]


def test_plain_summary_refuses_forecast_reports(tmp_path):
    report, output = _run(tmp_path)
    with pytest.raises(ValueError, match="--forecast"):
        backtest_report([output], tmp_path / "summary.json")
