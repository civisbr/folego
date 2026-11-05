from datetime import date, timedelta

from .metrics import weekly_metrics


def rolling_origin_splits(dates, years=range(2019, 2025)):
    for year in years:
        origin = date(year, 7, 1)
        train = [d for d in dates if d <= origin - timedelta(days=62)]
        test = [d for d in dates if origin <= d <= date(year, 10, 31)]
        if train and test:
            yield year, train, test


def rolling_backtest(frame, fit_predict, years=range(2019, 2025)):
    """Callback must train from scratch. Inputs must use archived forecast vintages.

    Use a per-region/age/outcome frame with complete daily rows. Weekly totals
    are computed before scoring; partial edge weeks are excluded.
    """
    import polars as pl

    reports = []
    for year, train_dates, test_dates in rolling_origin_splits(frame["date"].to_list(), years):
        train = frame.filter(pl.col("date").is_in(train_dates))
        test = frame.filter(pl.col("date").is_in(test_dates))
        prediction = fit_predict(train, test.drop("admissions"))
        scored = test.select("date", "admissions").with_columns(pl.Series("predicted", prediction))
        weekly = (
            scored.sort("date")
            .group_by_dynamic("date", every="1w")
            .agg(pl.col("admissions").sum(), pl.col("predicted").sum(), pl.len().alias("days"))
        )
        weekly = weekly.filter(pl.col("days") == 7)
        reports.append({"year": year, **weekly_metrics(weekly["admissions"], weekly["predicted"])})
    return reports
