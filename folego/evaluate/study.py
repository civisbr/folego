"""Regional association study on the research panel: effect, diagnostics, controls, backtest.

For each outcome (all ages, then each age group) the quasi-Poisson model of
models/glm.py is fitted with the PM2.5 cross-basis and the confounder design. The
report gives the cumulative relative risk for +10 µg/m³ over all lags, overdispersion,
residual autocorrelation, the joint exposure test (whose significance on the
negative-control outcome flags residual confounding) and a rolling-origin backtest.

Backtest inputs are observed exposures, not archived forecasts, so its errors are a
lower bound for an operational forecast.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from statsmodels.tools.sm_exceptions import PerfectSeparationError

from ..ingest.sih_national import software_provenance
from ..models.crossbasis import CrossBasis
from ..models.glm import ConfounderDesign, fit_count_model, predict_counts
from ..transform.cams_exposure import describe_input
from .backtest import rolling_backtest
from .negative_control import joint_exposure_test

NEGATIVE_CONTROL = "negative_control"
# Bumped whenever the model specification changes, so reports of different
# specifications are never compared by mistake. 1: 6 df trend + harmonics; 2: 7 df per
# calendar year; 3: 7 df per year of days present; 4: knots without support dropped;
# 5: effects with a log-RR standard error above MAX_LOG_RR_SE rejected; HAC and
# lagged-outcome sensitivities added; 6: effects from a numerically singular covariance
# rejected; 7: mean temperature of lags 1-7 and 8-21 and humidity of lags 1-7 in the
# inference design.
MODEL_SPEC_VERSION = 7
# A hinge needs days above its knot; a region that never reaches 75 µg/m³ would get an
# all-zero column and a rank-deficient design.
MIN_SHARE_ABOVE_KNOT = 0.01
# |log RR| + 1.96·SE beyond this (RR bounds past e^±20) is a degenerate fit, not an estimate.
MAX_LOG_RR_BOUND = 20.0
# A 95% CI spanning more than a factor of e^±5.9 says nothing about the effect.
MAX_LOG_RR_SE = 3.0
# Past 1/eps the covariance is numerically singular: sparse strata (~150 admissions in
# 14 years) "converged" with condition ~1e19-1e21 and exposure variances ~1e-16, which
# dominated inverse-variance pooling. Healthy strata measured 1e10-5e13; the value
# depends on confounder collinearity, so it is an empirical bound, kept per effect as
# covariance_condition. The exposure block alone, as a correlation matrix, does not
# separate the cases: the collapse is in the variances' scale.
MAX_COVARIANCE_CONDITION = 1 / np.finfo(float).eps
# Newey-West window for the autocorrelation-robust interval: the lag basis length.
HAC_LAGS = 7
MIN_TRAINING_DAYS = 365
TREND_DF_PER_YEAR = 7
# Effect estimates must not hinge on one smoothing choice; these bracket the default.
TREND_SENSITIVITY = (4, 10)


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
    # Rows are contiguous days, so shift(1) is the previous day. Same-day weather alone
    # left elderly injury and circulatory admissions "protected" by PM2.5 (see
    # docs/methodology.md). Full windows only: a partial mean would average different
    # days at the start of the series and around weather gaps.
    missing = {"temperature", "humidity"} - set(series.columns)
    if missing:
        raise ValueError(f"Panel lacks {', '.join(sorted(missing))}")
    temperature, humidity = pl.col("temperature").shift(1), pl.col("humidity").shift(1)
    return series.with_columns(
        temperature.rolling_mean(7).alias("temperature_lag1_7"),
        temperature.shift(7).rolling_mean(14).alias("temperature_lag8_21"),
        humidity.rolling_mean(7).alias("humidity_lag1_7"),
    )


def _design(series: pl.DataFrame, basis: CrossBasis, confounders: ConfounderDesign, *, fit: bool):
    exposure = basis.transform(series["pm25"].to_numpy())
    usable = np.isfinite(exposure).all(axis=1) & series["admissions"].is_not_null().to_numpy()
    missing = [c for c in confounders.columns if c not in series.columns]
    if missing:
        raise ValueError(f"Panel lacks {', '.join(missing)}")
    for column in confounders.columns:
        values = series[column]
        # A float NaN is not null; a rolling mean spreads one over its whole window.
        if values.dtype.is_float():
            values = values.fill_nan(None)
        usable &= values.is_not_null().to_numpy()
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
    full_covariance = np.asarray(model.cov_params())
    covariance = full_covariance[1 : width + 1, 1 : width + 1]
    log_rr = float(contrast @ beta)
    se = float(np.sqrt(contrast @ covariance @ contrast))
    # A sparse stratum can "converge" to huge coefficients; its RR is not an estimate.
    if not (np.isfinite(log_rr) and np.isfinite(se)):
        raise ValueError("Degenerate fit: non-finite effect or standard error")
    bound = abs(log_rr) + 1.96 * se
    if bound >= MAX_LOG_RR_BOUND:
        raise ValueError(f"Degenerate fit: |log RR| + 1.96·SE = {bound:.3g}")
    if se > MAX_LOG_RR_SE:
        raise ValueError(f"Degenerate fit: log-RR SE = {se:.3g}")
    # NaN outside the exposure block makes cond NaN or, depending on numpy, raises.
    try:
        condition = float(np.linalg.cond(full_covariance))
    except np.linalg.LinAlgError:
        condition = float("nan")
    if not condition <= MAX_COVARIANCE_CONDITION:
        raise ValueError(f"Degenerate fit: covariance condition number {condition:.3g}")
    return {
        "increase_ug_m3": increase,
        "reference_ug_m3": basis.reference,
        "rr": float(np.exp(log_rr)),
        "lower": float(np.exp(log_rr - 1.96 * se)),
        "upper": float(np.exp(log_rr + 1.96 * se)),
        "covariance_condition": condition,
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


def _inference_design(include_srag: bool, trend_df_per_year: float, extra=()) -> ConfounderDesign:
    # A 6 df trend plus harmonics left the dry-season drop in viral admissions, which
    # coincides with the fire season, to the exposure (protective RR, failed negative
    # control, Ljung-Box p≈0 in region 11004); a dense time spline absorbs it.
    return ConfounderDesign(
        include_srag=include_srag,
        trend_df_per_year=trend_df_per_year,
        harmonics=0,
        extra=extra,
        lagged_meteorology=True,
    )


def _effect(series, basis, include_srag, trend_df_per_year, *, extra=(), hac_lags=None):
    confounders = _inference_design(include_srag, trend_df_per_year, extra)
    rows, design, width = _design(series, basis, confounders, fit=True)
    model = fit_count_model(rows["admissions"].to_numpy(), design, hac_lags=hac_lags)
    return rows, model, width, confounders.trend_df


def _guarded(compute):
    try:
        return compute()
    except (ValueError, PerfectSeparationError) as error:
        return {"error": str(error)}


def _autocorrelation_sensitivity(series, basis, include_srag, trend_df_per_year, width):
    """Two checks on intervals that residual autocorrelation may make too narrow.

    HAC keeps the estimate and widens the interval; the lagged outcome (log of the
    previous day's admissions) changes the estimate too, and can absorb part of a real
    lagged effect, so it is a bound on sensitivity, not a preferred model.
    """

    def hac():
        rows, model = _effect(series, basis, include_srag, trend_df_per_year, hac_lags=HAC_LAGS)[:2]
        # Newey-West reads neighbouring rows as neighbouring days; a gap breaks that.
        if (rows["date"].diff().dt.total_days().drop_nulls() != 1).any():
            raise ValueError("Series has gaps inside the fitted span; HAC needs contiguous days")
        return {**cumulative_relative_risk(model, basis, width), "maxlags": HAC_LAGS}

    def lagged():
        # ``series`` is contiguous and sorted, so shift(1) is the previous day.
        frame = series.with_columns(pl.col("admissions").shift(1).log1p().alias("lagged_outcome"))
        rows, model = _effect(
            frame, basis, include_srag, trend_df_per_year, extra=("lagged_outcome",)
        )[:2]
        return {
            "days": rows.height,
            **cumulative_relative_risk(model, basis, width),
            "dispersion": float(model.scale),
            "residuals": residual_autocorrelation(model),
        }

    return {"effect_hac": _guarded(hac), "effect_with_lagged_outcome": _guarded(lagged)}


def fit_stratum(
    series: pl.DataFrame,
    basis: CrossBasis,
    *,
    trend_df_per_year: float = TREND_DF_PER_YEAR,
    sensitivity=TREND_SENSITIVITY,
) -> dict:
    include_srag = "srag" in series.columns
    rows, model, width, trend_df = _effect(series, basis, include_srag, trend_df_per_year)
    counts = rows["admissions"].to_numpy()
    # A degenerate effect still leaves dispersion and residuals worth reporting.
    effect = _guarded(lambda: cumulative_relative_risk(model, basis, width))
    unadjusted = None
    if include_srag:
        # SRAG can sit on the causal path; both estimates are reported side by side.
        try:
            plain = _effect(series.drop("srag"), basis, False, trend_df_per_year)[1]
            unadjusted = cumulative_relative_risk(plain, basis, width)
        except (ValueError, PerfectSeparationError) as error:
            unadjusted = {"error": str(error)}
    by_trend = {}
    for df in (d for d in sensitivity if d != trend_df_per_year):
        try:
            _, alternative, _, effective = _effect(series, basis, include_srag, df)
            by_trend[str(df)] = {
                **cumulative_relative_risk(alternative, basis, width),
                "trend_df": effective,
                "dispersion": float(alternative.scale),
                "exposure_test": joint_exposure_test(alternative, range(1, width + 1)),
            }
        except (ValueError, PerfectSeparationError) as error:
            by_trend[str(df)] = {"error": str(error)}
    return {
        "days": rows.height,
        "dates": [str(rows["date"].min()), str(rows["date"].max())],
        "admissions": int(counts.sum()),
        "mean_daily": float(counts.mean()),
        "srag_adjusted": include_srag,
        **({"effect_error": effect["error"]} if "error" in effect else {"effect": effect}),
        "effect_without_srag": unadjusted,
        **_autocorrelation_sensitivity(series, basis, include_srag, trend_df_per_year, width),
        "trend_df": trend_df,
        "effect_by_trend_df_per_year": by_trend,
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
        except (ValueError, PerfectSeparationError) as error:
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


def input_source(panel_path: Path, name: str) -> str:
    """Product behind a panel input ("CAMS", "ERA5", "MERRA-2" or "unknown")."""
    manifest = Path(panel_path).with_suffix(".parquet.source.json")
    if not manifest.exists():
        return "unknown"
    try:
        source = json.loads(manifest.read_text())["inputs"][name][0]["source"]
    except (KeyError, IndexError, TypeError, ValueError):
        return "unknown"
    attribution = source.get("attribution", []) if isinstance(source, dict) else []
    # Marts store either one attribution string or a list of them.
    if isinstance(attribution, list):
        attribution = " ".join(a for a in attribution if isinstance(a, str))
    elif not isinstance(attribution, str):
        attribution = ""
    if "MERRA-2" in attribution:
        return "MERRA-2"
    if "ERA5" in attribution:
        return "ERA5"
    if "Atmosphere Monitoring" in attribution:
        return "CAMS"
    return "unknown"


def meteorology_source(panel_path: Path) -> str:
    return input_source(panel_path, "meteorology")


def region_study(
    panel_path: Path,
    region: str,
    output: Path,
    *,
    outcomes=("respiratory", "pneumonia", "asthma", "circulatory", NEGATIVE_CONTROL),
    age_groups=(None, "0-4", "60+"),
    basis: CrossBasis | None = None,
    exposure: str = "pm25",
    trend_df_per_year: float = TREND_DF_PER_YEAR,
) -> dict:
    basis = basis or CrossBasis()
    requested_knots = basis.knots
    # A multi-region panel does not fit in memory several times over; read one region.
    panel = pl.scan_parquet(panel_path).filter(pl.col("health_region_code") == region).collect()
    if panel.is_empty():
        raise ValueError(f"Region {region} not in the panel")
    if exposure not in panel.columns:
        raise ValueError(f"Exposure column {exposure!r} not in the panel")
    if exposure != "pm25":
        # Sensitivity runs swap the exposure column; everything downstream reads "pm25".
        panel = panel.drop("pm25").rename({exposure: "pm25"})
    # NaN sorts above every number in polars and would count as support for every knot.
    levels = panel.group_by("date").agg(pl.col("pm25").first())["pm25"].drop_nulls().drop_nans()
    if levels.is_empty():
        raise ValueError(f"No {exposure} values for region {region}")
    supported = tuple(k for k in basis.knots if (levels > k).mean() >= MIN_SHARE_ABOVE_KNOT)
    if basis.reference in basis.knots and basis.reference not in supported:
        # The +10 µg/m³ contrast would sit almost wholly above the observed levels.
        raise ValueError(f"PM2.5 rarely exceeds {basis.reference} µg/m³ in region {region}")
    if supported != basis.knots:
        basis = dataclasses.replace(basis, knots=supported)
    results = []
    for outcome in outcomes:
        for age_group in age_groups:
            entry = {"outcome": outcome, "age_group": age_group or "all"}
            try:
                series = stratum_series(panel, region, outcome, age_group)
                entry.update(fit_stratum(series, basis, trend_df_per_year=trend_df_per_year))
                if age_group is None:
                    entry["backtest"] = backtest_stratum(series, basis)
            except (ValueError, PerfectSeparationError) as error:
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
        "pm25_source": input_source(panel_path, "pm25"),
        # The modelled column: a sensitivity run swaps in the MERRA-2 input.
        "exposure_source": input_source(
            panel_path, "merra2" if exposure == "pm25_merra2" else "pm25"
        ),
        "validated": False,
        "region": region,
        "basis": {
            "max_lag": basis.max_lag,
            "knots": list(basis.knots),
            "requested_knots": list(requested_knots),
            "min_share_above_knot": MIN_SHARE_ABOVE_KNOT,
            "max_log_rr_bound": MAX_LOG_RR_BOUND,
            "max_log_rr_se": MAX_LOG_RR_SE,
            "max_covariance_condition": MAX_COVARIANCE_CONDITION,
            "lag_degree": basis.lag_degree,
            "reference": basis.reference,
        },
        "model_spec_version": MODEL_SPEC_VERSION,
        "confounders": {
            "inference": (
                f"natural time spline, {trend_df_per_year} df/year, no harmonics;"
                " cubic B-splines (3 df) of mean temperature (lags 1-7, 8-21) and humidity"
                " (lags 1-7), full windows only"
            ),
            "trend_sensitivity_df_per_year": [
                d for d in TREND_SENSITIVITY if d != trend_df_per_year
            ],
            "autocorrelation_sensitivity": (
                f"Newey-West (HAC, {HAC_LAGS} lags) interval; log of previous-day admissions"
                " as a covariate"
            ),
            "backtest": (
                f"time spline {ConfounderDesign().trend_df} df"
                f" + {ConfounderDesign().harmonics} harmonics"
            ),
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
