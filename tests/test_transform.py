from datetime import date

import polars as pl
import pytest

from folego.transform.codes import age_group, diagnosis_group, municipality_code
from folego.transform.exposure import population_weighted_exposure
from folego.transform.outcomes import aggregate_outcomes


def test_codes_and_sih_ages():
    assert municipality_code("3550308") == municipality_code("355030") == "355030"
    for invalid in ["000000", None, "abc", "123", "3550309"]:
        with pytest.raises(ValueError):
            municipality_code(invalid)
    assert age_group(4, 4) == "0-4"
    assert age_group(4, 5) == "5-14"
    assert age_group(4, 60) == "60+"
    assert age_group(3, 11) == "0-4"  # months
    assert age_group(5, 1) == "60+"  # over 100
    assert age_group(0, 99) is None
    assert age_group(4, 999) is None
    assert diagnosis_group("J45.0") == "asthma"
    assert diagnosis_group("J189") == "pneumonia"
    assert diagnosis_group("J001") == "respiratory"
    assert diagnosis_group("I219") == "circulatory"
    assert diagnosis_group("S720") == "negative_control"
    assert diagnosis_group("K001") is None


def test_daily_counts_and_costs_by_residence():
    frame = pl.DataFrame(
        {
            "MUNIC_RES": ["3550308"] * 3,
            "DT_INTER": ["20240101"] * 3,
            "DIAG_PRINC": ["J450", "J189", "S720"],
            "COD_IDADE": [4] * 3,
            "IDADE": [70] * 3,
            "VAL_TOT": [100.0, 200.0, 300.0],
        }
    )
    lookup = pl.DataFrame({"municipality_code": ["355030"], "health_region_code": ["35001"]})
    result = aggregate_outcomes(frame, lookup)
    resp = result.filter(pl.col("outcome") == "respiratory")
    assert resp["admissions"].sum() == 2
    assert resp["payment"].sum() == 300
    assert resp["date"][0] == date(2024, 1, 1)
    assert result.filter(pl.col("outcome") == "negative_control")["admissions"].sum() == 1


def test_population_weights_and_missing_pixels():
    frame = pl.DataFrame(
        {
            "date": ["2024-01-01"] * 2,
            "health_region_code": ["1"] * 2,
            "pm25": [10.0, 50.0],
            "population": [90.0, 10.0],
        }
    )
    assert population_weighted_exposure(frame)["pm25"][0] == 14
    with pytest.raises(ValueError):
        population_weighted_exposure(frame.with_columns(pl.lit(0.0).alias("population")))
    with pytest.raises(ValueError):
        population_weighted_exposure(
            frame.with_columns(pl.lit(None).cast(pl.Float64).alias("pm25"))
        )
