"""Regional association study on the research panel: effect, diagnostics, controls, backtest.

For each outcome (all ages, then each age group) the quasi-Poisson model of
models/glm.py is fitted with the PM2.5 cross-basis and the confounder design. The
report gives the cumulative relative risk for +10 µg/m³ over all lags, overdispersion,
residual autocorrelation, the joint exposure test (whose significance on the
negative-control outcome flags residual confounding) and a rolling-origin backtest.

Backtest inputs are observed exposures, not archived forecasts, so its errors are a
lower bound for an operational forecast.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from ..ingest.sih_national import software_provenance
from ..models.crossbasis import CrossBasis
from ..models.glm import ConfounderDesign, fit_count_model, predict_counts
from ..transform.cams_exposure import describe_input
from .backtest import rolling_backtest
from .negative_control import joint_exposure_test

NEGATIVE_CONTROL = "negative_control"
MIN_TRAINING_DAYS = 365


def stratum_series(panel: pl.DataFrame, region: str, outcome: str, age_group: str | None):
    rows = panel.filter((pl.col("health_region_code") == region) & (pl.col("outcome") == outcome))
    if age_group is not None:
        rows = rows.filter(pl.col("age_group") == age_group)
    if rows.is_empty():
        raise ValueError("No panel rows for this outcome and age group")
    exposures = [
        c for c in ("pm25", "temperature", "humidity", "holiday", "srag") if c in rows.columns
    ]
    series = (
        rows.group_by("date")
        .agg(
            *[pl.col(c).first() for c in exposures],
            # Summing nulls would turn an unavailable day into zero admissions.
            pl.when(pl.col("admissions").is_not_null().all())
            .then(pl.col("admissions").sum())
            .alias("admissions"),
        )
        .sort("date")
    )
    dates = series["date"].to_list()
    if any((b - a).days != 1 for a, b in zip(dates, dates[1:], strict=False)):
        raise ValueError("Panel has missing days; the lag basis needs contiguous rows")
    return series


def _design(series: pl.DataFrame, basis: CrossBasis, confounders: ConfounderDesign, *, fit: bool):
    exposure = basis.transform(series["pm25"].to_numpy())
    usable = np.isfinite(exposure).all(axis=1) & series["admissions"].is_not_null().to_numpy()
    rows = series.filter(pl.Series(usable))
    if fit and rows.height < MIN_TRAINING_DAYS:
        raise ValueError(f"Only {rows.height} usable days; need {MIN_TRAINING_DAYS}")
    z = confounders.transform(rows, fit=fit)
    return rows, np.column_stack([exposure[usable], z]), exposure.shape[1]


def cumulative_relative_risk(model, basis: CrossBasis, width: int, *, increase=10.0):
    """RR of a sustained +``increase`` µg/m³ above the reference at every lag."""
    level = np.full(basis.max_lag + 1, basis.reference + increase)
    reference = np.full(basis.max_lag + 1, basis.reference)
    contrast = basis.transform(level)[-1] - basis.transform(reference)[-1]
    beta = np.asarray(model.params)[1 : width + 1]
    covariance = np.asarray(model.cov_params())[1 : width + 1, 1 : width + 1]
    log_rr = float(contrast @ beta)
    se = float(np.sqrt(contrast @ covariance @ contrast))
    return {
        "increase_ug_m3": increase,
        "reference_ug_m3": basis.reference,
        "rr": float(np.exp(log_rr)),
        "lower": float(np.exp(log_rr - 1.96 * se)),
        "upper": float(np.exp(log_rr + 1.96 * se)),
    }


def residual_autocorrelation(model, max_lag=7) -> dict:
    from statsmodels.stats.diagnostic import acorr_ljungbox

    residuals = np.asarray(model.resid_pearson)
    centred = residuals - residuals.mean()
    acf = [
        float(np.dot(centred[:-k], centred[k:]) / np.dot(centred, centred))
        for k in range(1, max_lag + 1)
    ]
    test = acorr_ljungbox(residuals, lags=[max_lag], return_df=True)
    return {"acf": acf, "ljung_box_pvalue": float(test["lb_pvalue"].iloc[0])}


def fit_stratum(series: pl.DataFrame, basis: CrossBasis) -> dict:
    include_srag = "srag" in series.columns
    confounders = ConfounderDesign(include_srag=include_srag)
    rows, design, width = _design(series, basis, confounders, fit=True)
    counts = rows["admissions"].to_numpy()
    model = fit_count_model(counts, design)
    unadjusted = None
    if include_srag:
        # SRAG can sit on the causal path; both estimates are reported side by side.
        plain = ConfounderDesign(include_srag=False)
        plain_rows, plain_design, _ = _design(series.drop("srag"), basis, plain, fit=True)
        unadjusted = cumulative_relative_risk(
            fit_count_model(plain_rows["admissions"].to_numpy(), plain_design), basis, width
        )
    return {
        "days": rows.height,
        "dates": [str(rows["date"].min()), str(rows["date"].max())],
        "admissions": int(counts.sum()),
        "mean_daily": float(counts.mean()),
        "srag_adjusted": include_srag,
        "effect": cumulative_relative_risk(model, basis, width),
        "effect_without_srag": unadjusted,
        "exposure_test": joint_exposure_test(model, range(1, width + 1)),
        "dispersion": float(model.scale),
        "residuals": residual_autocorrelation(model),
    }


def backtest_stratum(series: pl.DataFrame, basis: CrossBasis, years=range(2019, 2025)):
    """Rolling origins: train on data up to origin − 62 days, predict Jul–Oct.

    Each fold is scored on its own, so one fold without enough history does not hide
    the others.
    """
    full = series.filter(pl.col("admissions").is_not_null())
    reports = []
    for year in years:

        def fit_predict(train: pl.DataFrame, test: pl.DataFrame):
            confounders = ConfounderDesign(include_srag="srag" in train.columns)
            rows, design, _ = _design(train, basis, confounders, fit=True)
            model = fit_count_model(rows["admissions"].to_numpy(), design)
            # The lag basis needs the days before the test window; ``series`` is checked
            # to be contiguous, so its row order is the day order.
            start, stop = test["date"].min(), test["date"].max()
            context = series.filter(
                pl.col("date").is_between(start - timedelta(days=basis.max_lag), stop)
            )
            exposure = basis.transform(context["pm25"].to_numpy())
            keep = (context["date"] >= start).to_numpy()
            if keep.sum() != test.height or not np.isfinite(exposure[keep]).all():
                raise ValueError("Backtest window misaligned with its lag context")
            z = confounders.transform(context.filter(pl.Series(keep)))
            return predict_counts(model, np.column_stack([exposure[keep], z]))

        try:
            reports.extend(rolling_backtest(full, fit_predict, [year]))
        except ValueError as error:
            reports.append({"year": year, "error": str(error)})
    return reports


def _finite(value):
    """JSON has no NaN/inf; a degenerate diagnostic becomes null instead of losing the report."""
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def meteorology_source(panel_path: Path) -> str:
    """ "ERA5", "MERRA-2" or "unknown", read from the panel's input manifests."""
    manifest = Path(panel_path).with_suffix(".parquet.source.json")
    if not manifest.exists():
        return "unknown"
    inputs = json.loads(manifest.read_text()).get("inputs", {}).get("meteorology") or []
    source = (inputs[0].get("source") or {}) if inputs else {}
    attribution = " ".join(source.get("attribution", []))
    if "MERRA-2" in attribution:
        return "MERRA-2"
    if "ERA5" in attribution:
        return "ERA5"
    return "unknown"


def region_study(
    panel_path: Path,
    region: str,
    output: Path,
    *,
    outcomes=("respiratory", "pneumonia", "asthma", "circulatory", NEGATIVE_CONTROL),
    age_groups=(None, "0-4", "60+"),
    basis: CrossBasis | None = None,
    exposure: str = "pm25",
) -> dict:
    basis = basis or CrossBasis()
    panel = pl.read_parquet(panel_path).filter(pl.col("health_region_code") == region)
    if panel.is_empty():
        raise ValueError(f"Region {region} not in the panel")
    if exposure not in panel.columns:
        raise ValueError(f"Exposure column {exposure!r} not in the panel")
    if exposure != "pm25":
        # Sensitivity runs swap the exposure column; everything downstream reads "pm25".
        panel = panel.drop("pm25").rename({exposure: "pm25"})
    results = []
    for outcome in outcomes:
        for age_group in age_groups:
            entry = {"outcome": outcome, "age_group": age_group or "all"}
            try:
                series = stratum_series(panel, region, outcome, age_group)
                entry.update(fit_stratum(series, basis))
                if age_group is None:
                    entry["backtest"] = backtest_stratum(series, basis)
            except ValueError as error:
                entry["error"] = str(error)
            results.append(entry)
    control = next(
        (
            r
            for r in results
            if r["outcome"] == NEGATIVE_CONTROL and r["age_group"] == "all" and "effect" in r
        ),
        None,
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "software": software_provenance(),
        "inputs": {"panel": [describe_input(panel_path)]},
        "mode": "research",
        "exposure": exposure,
        "meteorology": meteorology_source(panel_path),
        "validated": False,
        "region": region,
        "basis": {
            "max_lag": basis.max_lag,
            "knots": list(basis.knots),
            "lag_degree": basis.lag_degree,
            "reference": basis.reference,
        },
        "negative_control_flag": None
        if control is None
        else control["exposure_test"]["requires_investigation"],
        "backtest_inputs": "observed exposures (not archived forecasts): optimistic",
        "results": results,
    }
    report = _finite(report)
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    # Every run is kept; the fixed name points at the latest one.
    history = output.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
    (history / f"{output.stem}-{stamp}.json").write_text(text)
    output.write_text(text)
    return report
