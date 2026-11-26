"""Operational backtest: PM2.5 as an archived CAMS forecast would have given it.

Each issue day d of the test season gets predictions for d..d+4 (lead days 0-4). Lag
days before d use the observed (reanalysis) series, lag days from d on use the forecast
issued on d. The same fitted model is also fed the observed exposure of those days
(an oracle that bounds what a perfect forecast could give) and a refit without PM2.5
(the baseline), so the report separates the value of PM2.5 from the cost of
forecasting it. Weather, holidays and SRAG stay observed, and the lag days before d use
the reanalysis, which operationally is only available months later (the near-real-time
analysis would stand in), so the scores are still optimistic in those terms.
"""

from datetime import date, timedelta

import numpy as np
import polars as pl

from ..models.crossbasis import CrossBasis
from ..models.glm import ConfounderDesign, fit_count_model, predict_counts
from .metrics import peak_metrics, weekly_metrics

LEAD_DAYS = 5
# The clinical series is published with a delay; same reserve as the observed backtest.
AVAILABILITY_DAYS = 62
VARIANTS = ("forecast", "observed", "baseline")


def _peaks(observed, predicted) -> dict:
    return {
        **peak_metrics(observed, predicted),
        "hits": int((observed & predicted).sum()),
        "predicted": int(predicted.sum()),
    }


def forecast_backtest_stratum(
    series: pl.DataFrame,
    forecasts: pl.DataFrame,
    basis: CrossBasis,
    years=range(2019, 2026),
    *,
    design,
) -> list[dict]:
    """One report per year; ``design(train, basis, confounders)`` is study._design.

    ``series`` is the contiguous daily stratum (study.stratum_series); ``forecasts`` has
    issue_date, date, lead_day and pm25_forecast for the same region.
    """
    from statsmodels.tools.sm_exceptions import PerfectSeparationError

    reports = []
    for year in years:
        try:
            reports.append(_fold(series, forecasts, basis, year, design))
        except (ValueError, PerfectSeparationError) as error:
            reports.append({"year": year, "error": str(error)})
    return reports


def _fold(series, forecasts, basis, year, design) -> dict:
    origin, season_end = date(year, 7, 1), date(year, 10, 31)
    train = series.filter(
        (pl.col("date") <= origin - timedelta(days=AVAILABILITY_DAYS))
        & pl.col("admissions").is_not_null()
    )
    confounders = ConfounderDesign(include_srag="srag" in series.columns)
    rows, matrix, width = design(train, basis, confounders)
    counts = rows["admissions"].to_numpy()
    with_pm = fit_count_model(counts, matrix)
    without_pm = fit_count_model(counts, matrix[:, width:])

    observed = dict(zip(series["date"].to_list(), series["pm25"].to_list(), strict=True))
    issued = forecasts.filter(pl.col("issue_date").is_between(origin, season_end))
    if issued.select(pl.struct("issue_date", "lead_day").is_duplicated().any()).item():
        raise ValueError("Repeated forecast issue date and lead day")
    by_issue = {
        key[0]: dict(
            zip(group["lead_day"].to_list(), group["pm25_forecast"].to_list(), strict=True)
        )
        for key, group in issued.group_by("issue_date")
    }
    targets = series.filter(
        pl.col("date").is_between(origin, season_end + timedelta(days=LEAD_DAYS - 1))
    )
    # The formula drops rows with missing values, which would misalign z with the dates.
    targets = targets.filter(pl.Series(_usable(targets, confounders)))
    if targets.is_empty():
        raise ValueError("No test day with complete confounders")
    z_rows = np.asarray(confounders.transform(targets))
    z_by_date = dict(zip(targets["date"].to_list(), range(targets.height), strict=True))
    admissions = dict(zip(targets["date"].to_list(), targets["admissions"].to_list(), strict=True))

    exposures = {variant: [] for variant in ("forecast", "observed")}
    z, truth, leads, issues = [], [], [], []
    for issue in sorted(by_issue):
        leads_of_issue = by_issue[issue]
        if sorted(leads_of_issue) != list(range(LEAD_DAYS)):
            continue
        days = [issue + timedelta(days=k) for k in range(LEAD_DAYS)]
        history = [issue - timedelta(days=j) for j in range(basis.max_lag, 0, -1)]
        past = [observed.get(d) for d in history]
        actual = [observed.get(d) for d in days]
        forecast = [leads_of_issue[k] for k in range(LEAD_DAYS)]
        values = [*past, *actual, *forecast]
        if any(v is None or not np.isfinite(v) or v < 0 for v in values):
            continue
        if any(d not in z_by_date for d in days):
            continue
        if any(admissions[d] is None for d in days):
            continue
        for variant, future in (("forecast", forecast), ("observed", actual)):
            window = basis.transform(np.array([*past, *future]))
            exposures[variant].append(window[basis.max_lag :])
        z.extend(z_rows[z_by_date[d]] for d in days)
        truth.extend(admissions[d] for d in days)
        leads.extend(range(LEAD_DAYS))
        issues.extend([issue] * LEAD_DAYS)
    if not issues:
        raise ValueError("No issue day with a complete forecast and observed rows")

    z = np.vstack(z)
    predicted = {
        variant: predict_counts(with_pm, np.column_stack([np.vstack(exposures[variant]), z]))
        for variant in ("forecast", "observed")
    }
    predicted["baseline"] = predict_counts(without_pm, z)
    truth, leads = np.asarray(truth, float), np.asarray(leads)
    issue_count = len(issues) // LEAD_DAYS

    report = {"year": year, "issue_days": issue_count}
    report["by_lead"] = [
        {
            "lead_day": k,
            **{f"mae_{v}": float(np.mean(abs(truth - predicted[v])[leads == k])) for v in VARIANTS},
        }
        for k in range(LEAD_DAYS)
    ]
    # Sums over each issue's five days, the horizon a user would act on.
    totals = truth.reshape(issue_count, LEAD_DAYS).sum(axis=1)
    sums = {v: predicted[v].reshape(issue_count, LEAD_DAYS).sum(axis=1) for v in VARIANTS}
    for variant in VARIANTS:
        report[variant] = weekly_metrics(totals, sums[variant])
    history_totals = train.select(pl.col("admissions").rolling_sum(LEAD_DAYS))[
        "admissions"
    ].drop_nulls()
    threshold = float(np.percentile(history_totals.to_numpy(), 90))
    observed_peak = totals > threshold
    report["peak_threshold"] = threshold
    report["observed_peaks"] = int(observed_peak.sum())
    for variant in VARIANTS:
        report[variant]["peaks"] = _peaks(observed_peak, sums[variant] > threshold)
    return report


def _usable(frame: pl.DataFrame, confounders: ConfounderDesign) -> np.ndarray:
    usable = np.ones(frame.height, dtype=bool)
    for column in confounders.columns:
        values = frame[column]
        if values.dtype.is_float():
            values = values.fill_nan(None)
        usable &= values.is_not_null().to_numpy()
    return usable


def exposure_error(observed: pl.DataFrame, forecasts: pl.DataFrame) -> list[dict]:
    """Forecast minus observed PM2.5 per lead day, all seasons and Jul-Oct only."""
    joined = forecasts.join(
        observed.select("date", "pm25").drop_nulls().filter(pl.col("pm25").is_not_nan()),
        on="date",
    ).with_columns((pl.col("pm25_forecast") - pl.col("pm25")).alias("error"))
    rows = []
    for season, frame in (
        ("all", joined),
        ("jul_oct", joined.filter(pl.col("date").dt.month().is_between(7, 10))),
    ):
        for (lead,), group in sorted(frame.group_by("lead_day"), key=lambda g: g[0][0]):
            rows.append(
                {
                    "season": season,
                    "lead_day": int(lead),
                    "days": group.height,
                    "mean_observed": float(group["pm25"].mean()),
                    "bias": float(group["error"].mean()),
                    "mae": float(group["error"].abs().mean()),
                    "correlation": float(np.corrcoef(group["pm25"], group["pm25_forecast"])[0, 1])
                    if group.height > 2
                    else None,
                }
            )
    return rows
