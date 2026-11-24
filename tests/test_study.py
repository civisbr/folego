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
    sensitivity = respiratory["effect_by_trend_df_per_year"]
    assert set(sensitivity) == {"4", "10"}
    assert all(v["lower"] < np.exp(EFFECT) < v["upper"] for v in sensitivity.values())
    assert "7 df/year" in report["confounders"]["inference"]
    hac = respiratory["effect_hac"]
    # Same estimate; only the interval changes.
    assert hac["rr"] == pytest.approx(respiratory["effect"]["rr"])
    assert hac["lower"] < np.exp(EFFECT) < hac["upper"]
    lagged = respiratory["effect_with_lagged_outcome"]
    assert lagged["lower"] < np.exp(EFFECT) < lagged["upper"]
    assert "ljung_box_pvalue" in lagged["residuals"] and lagged["days"] > 0
    from folego.models.glm import ConfounderDesign

    for bad in ("lagged", ("trend",), ("lambda",), ("a b",)):
        with pytest.raises(ValueError, match="column names"):
            ConfounderDesign(extra=bad)


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


def test_sensitivity_exposure_column_replaces_pm25(tmp_path):
    path = synthetic_panel(tmp_path / "panel.parquet", years=3)
    panel = pl.read_parquet(path)
    panel.with_columns((pl.col("pm25") * 1.1).alias("pm25_merra2")).write_parquet(path)
    report = region_study(
        path,
        "11004",
        tmp_path / "s.json",
        outcomes=("respiratory",),
        age_groups=(None,),
        exposure="pm25_merra2",
    )
    assert report["exposure"] == "pm25_merra2" and "effect" in report["results"][0]
    with pytest.raises(ValueError, match="not in the panel"):
        region_study(path, "11004", tmp_path / "t.json", exposure="nope")


def test_meteorology_source_comes_from_the_panel_manifest(tmp_path):
    import json

    from folego.evaluate.study import meteorology_source

    panel = tmp_path / "panel.parquet"
    assert meteorology_source(panel) == "unknown"
    for text, expected in (
        ("Global Modeling ... MERRA-2 tavg1_2d_slv_Nx", "MERRA-2"),
        ("Hersbach et al., ERA5", "ERA5"),
    ):
        manifest = {"inputs": {"meteorology": [{"source": {"attribution": [text]}}]}}
        panel.with_suffix(".parquet.source.json").write_text(json.dumps(manifest))
        assert meteorology_source(panel) == expected
    from folego.evaluate.study import input_source

    merra = {"inputs": {"pm25": [{"source": {"attribution": "GMAO, MERRA-2 aer"}}]}}
    panel.with_suffix(".parquet.source.json").write_text(json.dumps(merra))
    assert input_source(panel, "pm25") == "MERRA-2"
    cams = {"inputs": {"pm25": [{"source": {"attribution": ["Atmosphere Monitoring Service"]}}]}}
    panel.with_suffix(".parquet.source.json").write_text(json.dumps(cams))
    assert input_source(panel, "pm25") == "CAMS"


def test_dense_trend_df_scales_with_training_span():
    import pandas as pd

    from folego.models.glm import ConfounderDesign

    days = pd.date_range("2018-01-01", "2019-12-31")
    step = np.arange(len(days))
    frame = pd.DataFrame(
        {
            "date": days,
            "temperature": 25 + np.sin(step),
            "humidity": 70 + 5 * np.cos(step),
            "holiday": 0,
        }
    )
    design = ConfounderDesign(trend_df_per_year=7, harmonics=0)
    width = design.transform(frame, fit=True).shape[1]
    assert design.trend_df == 14
    # Prediction reuses the fitted knots, so the width does not depend on the new span.
    assert design.transform(frame.iloc[:30]).shape[1] == width
    with pytest.raises(ValueError, match="positive"):
        ConfounderDesign(trend_df_per_year=0)


def test_lagged_meteorology_uses_previous_full_windows(tmp_path):
    from folego.evaluate.study import stratum_series

    panel = pl.read_parquet(synthetic_panel(tmp_path / "panel.parquet", years=1))
    series = stratum_series(panel, "11004", "respiratory", "60+")
    temperature = series["temperature"].to_numpy()
    assert series["temperature_lag1_7"][21] == pytest.approx(temperature[14:21].mean())
    assert series["temperature_lag8_21"][21] == pytest.approx(temperature[0:14].mean())
    # Full windows only: day 21 is the first with lags 8-21, day 7 with lags 1-7.
    assert series["temperature_lag8_21"][20] is None
    assert series["humidity_lag1_7"][6] is None and series["humidity_lag1_7"][7] is not None
    with pytest.raises(ValueError, match="lacks humidity$"):
        stratum_series(panel.drop("humidity"), "11004", "respiratory", "60+")
    with pytest.raises(ValueError, match="lacks temperature$"):
        stratum_series(panel.drop("temperature"), "11004", "respiratory", "60+")


def test_design_drops_rows_without_lagged_meteorology(tmp_path):
    from folego.evaluate.study import _design, _inference_design, stratum_series
    from folego.models.crossbasis import CrossBasis

    panel = pl.read_parquet(synthetic_panel(tmp_path / "panel.parquet", years=2))
    series = stratum_series(panel, "11004", "respiratory", "60+")
    # One NaN day (not null) spoils every window that contains it.
    series = series.with_columns(
        pl.when(pl.int_range(pl.len()) == 400)
        .then(float("nan"))
        .otherwise(pl.col("temperature_lag1_7"))
        .alias("temperature_lag1_7")
    )
    rows, design, _ = _design(series, CrossBasis(), _inference_design(False, 7), fit=True)
    assert rows["date"].min() == series["date"][21]
    assert series["date"][400] not in rows["date"].to_list()
    assert np.isfinite(design).all()
    # A missing same-day value drops that day (and the windows holding it), not the stratum.
    raw = panel.with_columns(
        pl.when(pl.col("date") == series["date"][500])
        .then(None)
        .otherwise(pl.col("temperature"))
        .alias("temperature")
    )
    series = stratum_series(raw, "11004", "respiratory", "60+")
    rows = _design(series, CrossBasis(), _inference_design(False, 7), fit=True)[0]
    assert not set(series["date"][500:522]) & set(rows["date"])
    assert {series["date"][499], series["date"][522]} <= set(rows["date"])


def test_confounder_design_errors_are_value_errors():
    import pandas as pd

    from folego.models.glm import ConfounderDesign

    frame = pd.DataFrame(
        {"date": pd.date_range("2018-01-01", periods=60), "humidity": 70.0, "holiday": 0}
    )
    frame["temperature"] = np.where(np.arange(60) == 3, np.nan, 25.0)
    with pytest.raises(ValueError, match="Confounder design"):
        ConfounderDesign(trend_df_per_year=7, harmonics=0).transform(frame, fit=True)


def test_lagged_meteorology_terms_and_names():
    import pandas as pd

    from folego.models.glm import ConfounderDesign

    days = pd.date_range("2018-01-01", "2019-12-31")
    step = np.arange(len(days))
    frame = pd.DataFrame({"date": days, "temperature": 25 + np.sin(step), "humidity": 70.0})
    frame = frame.assign(holiday=0, humidity=70 + 5 * np.cos(step))
    for column in ConfounderDesign.LAGGED_METEOROLOGY:
        frame[column] = frame["humidity" if column.startswith("humidity") else "temperature"]
    lagged = ConfounderDesign(trend_df_per_year=7, harmonics=0, lagged_meteorology=True)
    plain = ConfounderDesign(trend_df_per_year=7, harmonics=0)
    assert lagged.columns == plain.columns + ConfounderDesign.LAGGED_METEOROLOGY
    width = plain.transform(frame, fit=True).shape[1]
    assert lagged.transform(frame, fit=True).shape[1] == width + 9
    for name in ("temperature_lag1_7", "bs", "cr", "np"):
        with pytest.raises(ValueError, match="extra"):
            ConfounderDesign(extra=(name,))


def test_knots_without_support_are_dropped(tmp_path):
    path = synthetic_panel(tmp_path / "panel.parquet", years=3)
    # Cap exposure below the top knot (75 µg/m³) so its hinge would be all zeros.
    pl.read_parquet(path).with_columns(pl.col("pm25").clip(upper_bound=60)).write_parquet(path)
    report = region_study(
        path, "11004", tmp_path / "s.json", outcomes=("respiratory",), age_groups=(None,)
    )
    assert 75.0 not in report["basis"]["knots"] and 15.0 in report["basis"]["knots"]
    assert "effect" in report["results"][0]
    assert report["model_spec_version"] == 7 and report["basis"]["requested_knots"][-1] == 75.0


def test_degenerate_fit_is_rejected():
    from types import SimpleNamespace

    from folego.evaluate.study import cumulative_relative_risk
    from folego.models.crossbasis import CrossBasis

    basis = CrossBasis()
    width = basis.transform(np.full(20, 20.0)).shape[1]
    model = SimpleNamespace(
        params=np.r_[0.0, np.full(width, 50.0)], cov_params=lambda: np.eye(width + 1)
    )
    with pytest.raises(ValueError, match="Degenerate"):
        cumulative_relative_risk(model, basis, width)


def test_nan_exposure_is_not_support_and_clean_regions_are_refused(tmp_path):
    path = synthetic_panel(tmp_path / "panel.parquet", years=2)
    panel = pl.read_parquet(path)
    nan_spikes = pl.when(pl.col("date").dt.day() == 1).then(float("nan")).otherwise(pl.col("pm25"))
    panel.with_columns(nan_spikes.clip(upper_bound=60).alias("pm25")).write_parquet(path)
    report = region_study(
        path, "11004", tmp_path / "s.json", outcomes=("respiratory",), age_groups=(None,)
    )
    # NaN days (about 3%) must not count as days above 75 µg/m³.
    assert 75.0 not in report["basis"]["knots"]
    panel.with_columns(pl.col("pm25").clip(upper_bound=12)).write_parquet(path)
    with pytest.raises(ValueError, match="rarely exceeds"):
        region_study(path, "11004", tmp_path / "s.json")
    panel.with_columns(pl.lit(None, pl.Float64).alias("pm25")).write_parquet(path)
    with pytest.raises(ValueError, match="No pm25"):
        region_study(path, "11004", tmp_path / "s.json")


def test_imprecise_effect_is_rejected():
    from types import SimpleNamespace

    from folego.evaluate.study import cumulative_relative_risk
    from folego.models.crossbasis import CrossBasis

    basis = CrossBasis()
    width = basis.transform(np.full(20, 20.0)).shape[1]
    # A zero effect with SE ≈ 5: inside the RR bound (1.96·SE ≈ 10 < 20), above SE 3.
    model = SimpleNamespace(params=np.zeros(width + 1), cov_params=lambda: np.eye(width + 1) * 0.2)
    with pytest.raises(ValueError, match="Degenerate fit: log-RR SE"):
        cumulative_relative_risk(model, basis, width)


def test_singular_covariance_is_rejected():
    from types import SimpleNamespace

    from folego.evaluate.study import cumulative_relative_risk
    from folego.models.crossbasis import CrossBasis

    basis = CrossBasis()
    width = basis.transform(np.full(20, 20.0)).shape[1]
    # Tiny exposure variances next to a huge trend one: falsely precise, not an estimate.
    covariance = np.diag([1e-16] * (width + 1) + [1e4])
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: covariance)
    with pytest.raises(ValueError, match="condition number 1e\\+20"):
        cumulative_relative_risk(model, basis, width)
    broken = np.diag([1e-4] * (width + 1) + [np.nan])
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: broken)
    with pytest.raises(ValueError, match="condition number nan"):
        cumulative_relative_risk(model, basis, width)
    # Either side of 1/eps ≈ 4.5e15.
    edge = np.diag([1e-4] * (width + 1) + [1e11])
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: edge)
    effect = cumulative_relative_risk(model, basis, width)
    assert effect["covariance_condition"] == pytest.approx(1e15)
    beyond = np.diag([1e-4] * (width + 1) + [1e12])
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: beyond)
    with pytest.raises(ValueError, match="condition number 1e\\+16"):
        cumulative_relative_risk(model, basis, width)
    healthy = np.diag([1e-4] * (width + 1) + [1e4])
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: healthy)
    effect = cumulative_relative_risk(model, basis, width)
    assert effect["rr"] == 1.0 and effect["covariance_condition"] == pytest.approx(1e8)


def test_degenerate_effect_keeps_stratum_diagnostics(tmp_path, monkeypatch):
    from folego.evaluate import study

    def degenerate(*args, **kwargs):
        raise ValueError("Degenerate fit: test")

    monkeypatch.setattr(study, "cumulative_relative_risk", degenerate)
    panel = pl.read_parquet(synthetic_panel(tmp_path / "panel.parquet", years=3))
    series = study.stratum_series(panel, "11004", "respiratory", None)
    result = study.fit_stratum(series, study.CrossBasis(), sensitivity=())
    assert "effect" not in result and result["effect_error"].startswith("Degenerate")
    assert result["dispersion"] > 0 and "ljung_box_pvalue" in result["residuals"]
    assert "error" in result["effect_hac"]


def test_common_knots_are_kept_and_named(tmp_path):
    from folego.cli import study_suffix
    from folego.models.crossbasis import CrossBasis

    path = synthetic_panel(tmp_path / "panel.parquet", years=3)
    report = region_study(
        path,
        "11004",
        tmp_path / "s.json",
        outcomes=("respiratory",),
        age_groups=(None,),
        basis=CrossBasis(knots=(15.0, 35.0)),
    )
    assert report["basis"]["knots"] == report["basis"]["requested_knots"] == [15.0, 35.0]
    assert study_suffix("ERA5", "CAMS", "pm25") == ""
    assert study_suffix("MERRA-2", "CAMS", "pm25", (15.0, 35.0)) == "-met_merra2-knots15_35"
    assert study_suffix("MERRA-2", "MERRA-2", "pm25") == "-met_merra2-pm25main_merra2"
    assert study_suffix("ERA5", "CAMS", "pm25_merra2") == "-pm25_merra2"
    assert study_suffix("unknown", "CAMS", "pm25", CrossBasis().knots) == "-unknown_source"


def test_covariance_condition_failure_is_rejected(monkeypatch):
    from types import SimpleNamespace

    from folego.evaluate.study import cumulative_relative_risk
    from folego.models.crossbasis import CrossBasis

    def broken(_):
        raise np.linalg.LinAlgError("SVD did not converge")

    basis = CrossBasis()
    width = basis.transform(np.full(20, 20.0)).shape[1]
    covariance = np.diag([1e-4] * (width + 2))
    model = SimpleNamespace(params=np.zeros(width + 2), cov_params=lambda: covariance)
    monkeypatch.setattr(np.linalg, "cond", broken)
    with pytest.raises(ValueError, match="condition number nan"):
        cumulative_relative_risk(model, basis, width)


def test_missing_confounder_column_is_a_stratum_error(tmp_path):
    from folego.evaluate.study import _design, _inference_design, stratum_series
    from folego.models.crossbasis import CrossBasis

    panel = pl.read_parquet(synthetic_panel(tmp_path / "panel.parquet", years=2))
    series = stratum_series(panel.drop("holiday"), "11004", "respiratory", "60+")
    with pytest.raises(ValueError, match="Panel lacks holiday$"):
        _design(series, CrossBasis(), _inference_design(False, 7), fit=True)


def test_confounder_design_error_is_one_line():
    import pandas as pd

    from folego.models.glm import ConfounderDesign

    frame = pd.DataFrame(
        {"date": pd.date_range("2018-01-01", periods=60), "humidity": 70.0, "holiday": 0}
    )
    frame["temperature"] = np.where(np.arange(60) == 3, np.nan, 25.0)
    with pytest.raises(ValueError) as caught:
        ConfounderDesign(trend_df_per_year=7, harmonics=0).transform(frame, fit=True)
    assert "\n" not in str(caught.value) and "^" not in str(caught.value)


def test_missing_weather_fails_only_its_backtest_fold(tmp_path):
    from folego.evaluate.study import backtest_stratum, stratum_series
    from folego.models.crossbasis import CrossBasis

    panel = pl.read_parquet(synthetic_panel(tmp_path / "panel.parquet", years=4))
    panel = panel.with_columns(
        pl.when(pl.col("date") == date(2020, 8, 15))
        .then(None)
        .otherwise(pl.col("temperature"))
        .alias("temperature")
    )
    series = stratum_series(panel, "11004", "respiratory", "60+")
    folds = backtest_stratum(series, CrossBasis(knots=(15, 35)), years=range(2020, 2022))
    by_year = {f["year"]: f for f in folds}
    assert "Confounder design" in by_year[2020]["error"]
    assert "error" not in by_year[2021]
