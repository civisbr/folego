from datetime import date, timedelta

from .metrics import peak_metrics, weekly_metrics


def rolling_origin_splits(dates, years=range(2019, 2025)):
    for year in years:
        origin = date(year, 7, 1)
        train = [d for d in dates if d <= origin - timedelta(days=62)]
        test = [d for d in dates if origin <= d <= date(year, 10, 31)]
        if train and test:
            yield year, train, test


def _weekly(frame, columns):
    import polars as pl

    weekly = (
        frame.sort("date")
        .group_by_dynamic("date", every="1w")
        .agg(*(pl.col(c).sum() for c in columns), pl.len().alias("days"))
    )
    return weekly.filter(pl.col("days") == 7)


def _peaks(observed, predicted) -> dict:
    predicted = predicted.to_numpy()
    # Counts as well as ratios, so folds can be pooled across regions.
    return {
        **peak_metrics(observed, predicted),
        "hits": int((observed & predicted).sum()),
        "predicted": int(predicted.sum()),
    }


def rolling_backtest(frame, fit_predict, years=range(2019, 2025), baseline=None):
    """Callback must train from scratch.

    Only archived forecast vintages make the scores operational; observed exposures,
    as the study uses today, make them optimistic, and reports must say so.

    Use a per-region/age/outcome frame with complete daily rows. Weekly totals
    are computed before scoring; partial edge weeks are excluded. ``baseline``, a
    callback with the same signature, scores the same weeks without the exposure.
    Peaks are test weeks above the 90th percentile of the training weeks, so the
    threshold never sees the test season.
    """
    import numpy as np
    import polars as pl

    reports = []
    for year, train_dates, test_dates in rolling_origin_splits(frame["date"].to_list(), years):
        train = frame.filter(pl.col("date").is_in(train_dates))
        test = frame.filter(pl.col("date").is_in(test_dates))
        scored = test.select("date", "admissions").with_columns(
            pl.Series("predicted", fit_predict(train, test.drop("admissions")))
        )
        if baseline is not None:
            scored = scored.with_columns(
                pl.Series("baseline", baseline(train, test.drop("admissions")))
            )
        columns = [c for c in ("admissions", "predicted", "baseline") if c in scored.columns]
        weekly = _weekly(scored, columns)
        report = {"year": year, "weeks": weekly.height}
        report.update(weekly_metrics(weekly["admissions"], weekly["predicted"]))
        threshold = float(
            np.percentile(
                _weekly(train.select("date", "admissions"), ["admissions"])["admissions"], 90
            )
        )
        observed_peak = (weekly["admissions"] > threshold).to_numpy()
        report["peak_threshold"] = threshold
        report["observed_peaks"] = int(observed_peak.sum())
        report["peaks"] = _peaks(observed_peak, weekly["predicted"] > threshold)
        if baseline is not None:
            report["baseline"] = {
                **weekly_metrics(weekly["admissions"], weekly["baseline"]),
                "peaks": _peaks(observed_peak, weekly["baseline"] > threshold),
            }
        reports.append(report)
    return reports
