from datetime import date

import numpy as np
import polars as pl
import pytest

from folego.evaluate.study import region_study

EFFECT = 0.08  # log RR per 10 µg/m³ at lag 0, as in the model test


def synthetic_panel(path, *, seed=7, years=7):
    rng = np.random.default_rng(seed)
    dates = pl.date_range(date(2018, 1, 1), date(2018 + years - 1, 12, 31), eager=True)
    n = len(dates)
    doy = np.array([d.timetuple().tm_yday for d in dates])
    pm25 = 12 + 40 * np.clip(np.sin(2 * np.pi * (doy - 180) / 365), 0, None) ** 4
    pm25 = pm25 * rng.lognormal(0, 0.3, n)
    temperature = 26 + 3 * np.sin(2 * np.pi * doy / 365) + rng.normal(0, 1, n)
    humidity = np.clip(
        75 - 15 * np.sin(2 * np.pi * (doy - 150) / 365) + rng.normal(0, 4, n), 5, 100
    )
    base = 40 * (1 + 0.2 * np.cos(2 * np.pi * doy / 365))
    rows = []
    for outcome, effect in (("respiratory", EFFECT), ("negative_control", 0.0)):
        mean = base * np.exp(effect * (pm25 - 15) / 10)
        counts = rng.poisson(mean)
        rows.append(
            pl.DataFrame(
                {
                    "date": dates,
                    "health_region_code": "11004",
                    "age_group": "60+",
                    "outcome": outcome,
                    "pm25": pm25,
                    "temperature": temperature,
                    "humidity": humidity,
                    "holiday": np.array(
                        [int((d.month, d.day) in {(1, 1), (9, 7), (12, 25)}) for d in dates]
                    ),
                    "admissions": counts,
                }
            )
        )
    panel = pl.concat(rows).with_columns(
        # The last months are outside the certified admission window.
        pl.when(pl.col("date") > date(2018 + years - 1, 9, 30))
        .then(None)
        .otherwise(pl.col("admissions"))
        .alias("admissions")
    )
    panel.write_parquet(path)
    return path


def test_study_recovers_effect_and_passes_negative_control(tmp_path):
    report = region_study(
        synthetic_panel(tmp_path / "panel.parquet"),
        "11004",
        tmp_path / "study.json",
        outcomes=("respiratory", "negative_control"),
        age_groups=(None, "0-4"),
    )
    by = {(r["outcome"], r["age_group"]): r for r in report["results"]}
    respiratory = by[("respiratory", "all")]
    # A sustained +10 µg/m³ at lag 0 only: RR ≈ exp(0.08) ≈ 1.083.
    assert respiratory["effect"]["lower"] < np.exp(EFFECT) < respiratory["effect"]["upper"]
    assert respiratory["effect"]["lower"] > 1
    assert report["negative_control_flag"] is False
    assert (
        by[("negative_control", "all")]["effect"]["lower"]
        < 1
        < by[("negative_control", "all")]["effect"]["upper"]
    )
    assert "error" in by[("respiratory", "0-4")]
    assert respiratory["dispersion"] == pytest.approx(1, abs=0.2)
    backtest = respiratory["backtest"]
    assert [b["year"] for b in backtest] == [2019, 2020, 2021, 2022, 2023, 2024]
    assert all(np.isfinite(b["mae"]) for b in backtest)
    assert len(respiratory["residuals"]["acf"]) == 7
    assert (tmp_path / "study.json").exists()


def test_missing_region_fails(tmp_path):
    with pytest.raises(ValueError, match="not in the panel"):
        region_study(synthetic_panel(tmp_path / "p.parquet", years=2), "99999", tmp_path / "s.json")


def test_srag_adjusted_and_unadjusted_effects_and_per_fold_errors(tmp_path):
    path = synthetic_panel(tmp_path / "panel.parquet")
    panel = pl.read_parquet(path)
    rng = np.random.default_rng(3)
    srag = pl.DataFrame({"date": panel["date"].unique().sort()}).with_columns(
        pl.Series("srag", rng.normal(1, 0.3, panel["date"].n_unique()))
    )
    # Starting in mid-2018 leaves the 2019 fold without a year of training.
    panel.join(srag, on="date").filter(pl.col("date") >= date(2018, 6, 1)).write_parquet(path)
    report = region_study(
        path, "11004", tmp_path / "study.json", outcomes=("respiratory",), age_groups=(None,)
    )
    result = report["results"][0]
    assert result["srag_adjusted"] is True
    assert result["effect_without_srag"]["rr"] > 1
    folds = {f["year"]: f for f in result["backtest"]}
    assert "error" in folds[2019] and "mae" in folds[2020]
    assert len(list((tmp_path / "history").glob("study-*.json"))) == 1
    assert report["inputs"]["panel"][0]["sha256"]
