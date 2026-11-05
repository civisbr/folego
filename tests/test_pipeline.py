"""The complete forecast runner must recover a synthetic signal without leakage."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from folego.models.crossbasis import CrossBasis
from folego.pipeline import run_forecast


def synthetic_panel():
    rng = np.random.default_rng(48)
    start = date(2020, 1, 1)
    n = 1100
    dates = [start + timedelta(days=i) for i in range(n + 5)]
    pm25 = rng.uniform(5, 100, n + 5)
    mu = np.exp(3 + 0.003 * (pm25 - 15))
    data = pl.DataFrame(
        {
            "date": dates,
            "health_region_code": ["11004"] * (n + 5),
            "age_group": ["60+"] * (n + 5),
            "outcome": ["respiratory"] * (n + 5),
            "pm25": pm25,
            "temperature": rng.uniform(15, 35, n + 5),
            "humidity": rng.uniform(30, 90, n + 5),
            "holiday": rng.binomial(1, 0.04, n + 5),
            "admissions": rng.poisson(mu),
            "mean_payment": [1500.0] * (n + 5),
        }
    )
    return data.head(n), data.tail(5), dates[n - 1]


def test_end_to_end_research_forecast_preserves_history_and_publication_lag():
    history, future, origin = synthetic_panel()
    first = run_forecast(history, future, issued_on=origin)
    assert first["mode"] == "research" and first["validated"] is False
    result = first["results"][0]
    assert result["excess"] > 0
    assert result["training_cutoff"] == (origin - timedelta(days=62)).isoformat()
    assert result["payment"] == pytest.approx(result["excess"] * 1500)
    # Deliberately corrupt outcomes unavailable at issuance: prediction must be identical.
    history = history.with_columns(
        pl.when(pl.col("date") > origin - timedelta(days=62))
        .then(1000000)
        .otherwise(pl.col("admissions"))
        .alias("admissions")
    )
    second = run_forecast(history, future, issued_on=origin)
    assert second == first


def test_missing_days_fail_instead_of_turning_lag_days_into_row_numbers():
    history, future, origin = synthetic_panel()
    with pytest.raises(ValueError, match="Missing daily"):
        run_forecast(history.filter(pl.col("date") != date(2021, 1, 1)), future, issued_on=origin)


def test_crossbasis_future_does_not_change_past():
    basis = CrossBasis()
    x = np.arange(30, dtype=float)
    changed = x.copy()
    changed[-1] = 1000
    assert np.allclose(basis.transform(x)[7:-1], basis.transform(changed)[7:-1])
