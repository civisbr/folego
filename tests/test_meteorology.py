import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")

from folego.transform.meteorology import daily_meteorology, relative_humidity  # noqa: E402

LATS = (0.0, -0.75)
LONS = (0.0, 0.75)


def era5_file(path, start, steps, temperature, dewpoint):
    times = pd.date_range(start, periods=steps, freq="3h")
    shape = (steps, len(LATS), len(LONS))

    def field(values, name):
        data = np.broadcast_to(np.asarray(values, dtype="float32")[:, None, None], shape)
        return xr.DataArray(
            data.copy(),
            dims=("valid_time", "latitude", "longitude"),
            coords={"valid_time": times, "latitude": list(LATS), "longitude": list(LONS)},
            attrs={"units": "K"},
            name=name,
        )

    xr.Dataset(
        {"t2m": field(temperature(times), "t2m"), "d2m": field(dewpoint(times), "d2m")}
    ).to_netcdf(path)
    return path


def test_relative_humidity_reference_values():
    assert relative_humidity(np.array([25.0]), np.array([25.0]))[0] == pytest.approx(100.0)
    # Magnus (Alduchov & Eskridge 1996): 30 °C air with a 20 °C dew point is ~55 %.
    assert relative_humidity(np.array([30.0]), np.array([20.0]))[0] == pytest.approx(55.1, abs=0.2)


def test_year_boundary_day_is_completed_from_the_next_file(tmp_path):
    constant = lambda value: lambda times: np.full(len(times), value)  # noqa: E731
    first = era5_file(
        tmp_path / "2019.nc", "2019-12-30 00:00", 16, constant(300.15), constant(290.15)
    )
    second = era5_file(
        tmp_path / "2020.nc", "2020-01-01 00:00", 8, constant(300.15), constant(290.15)
    )
    daily = daily_meteorology([second, first])
    dates = [str(d)[:10] for d in daily["temperature"]["date"].values]
    # Local 31 Dec needs 00 UTC of 1 Jan, stored in the next year's file.
    assert dates == ["2019-12-30", "2019-12-31"]
    np.testing.assert_allclose(daily["temperature"].values, 27.0, atol=1e-4)


def test_humidity_is_averaged_per_step_not_from_daily_means(tmp_path):
    # Hot dry afternoons and cool humid nights: RH of the mean differs from the mean RH.
    temperature = lambda times: np.where(times.hour % 6 == 0, 308.15, 293.15)  # noqa: E731
    dewpoint = lambda times: np.full(len(times), 291.15)  # noqa: E731
    path = era5_file(tmp_path / "era5.nc", "2019-08-01 00:00", 24, temperature, dewpoint)
    daily = daily_meteorology([path])
    hot = relative_humidity(np.array([35.0]), np.array([18.0]))[0]
    cool = relative_humidity(np.array([20.0]), np.array([18.0]))[0]
    expected = np.mean(
        [
            hot if t.hour % 6 == 0 else cool
            for t in pd.date_range("2019-08-01 03:00", periods=8, freq="3h")
        ]
    )
    day = daily["humidity"].sel(date=np.datetime64("2019-08-01"))
    np.testing.assert_allclose(day.values, expected, rtol=1e-5)
    from_means = relative_humidity(np.array([27.5]), np.array([18.0]))[0]
    assert abs(expected - from_means) > 1


def test_small_dewpoint_excess_is_clamped_and_counted_large_one_fails(tmp_path):
    slightly = era5_file(
        tmp_path / "fog.nc",
        "2019-08-01 00:00",
        16,
        lambda times: np.full(len(times), 290.0),
        lambda times: np.where(times.hour == 3, 290.3, 289.0),
    )
    daily = daily_meteorology([slightly])
    assert daily["humidity"].max() <= 100.0
    assert daily["dewpoint_clamped"]["steps"] == 2 * len(LATS) * len(LONS)
    assert daily["dewpoint_clamped"]["max_excess_k"] == pytest.approx(0.3, abs=1e-3)
    other = tmp_path / "bad"
    other.mkdir()
    corrupt = era5_file(
        other / "bad.nc",
        "2019-08-01 00:00",
        16,
        lambda times: np.full(len(times), 290.0),
        lambda times: np.full(len(times), 300.0),
    )
    with pytest.raises(ValueError, match="5 K"):
        daily_meteorology([corrupt])


def test_overlapping_or_missing_years_fail(tmp_path):
    def constant(value):
        return lambda times: np.full(len(times), value)

    first = era5_file(tmp_path / "a.nc", "2019-12-30 00:00", 16, constant(300.0), constant(290.0))
    overlap = era5_file(tmp_path / "b.nc", "2019-12-31 00:00", 16, constant(300.0), constant(290.0))
    with pytest.raises(ValueError, match="Repeated|twice"):
        daily_meteorology([first, overlap])
    late = era5_file(tmp_path / "c.nc", "2020-01-05 00:00", 16, constant(300.0), constant(290.0))
    with pytest.raises(ValueError, match="contiguous"):
        daily_meteorology([first, late])


def test_build_meteorology_joins_both_variables_per_region(tmp_path):
    import json

    from geo_helpers import POPULATION, write_population, write_regions

    from folego.transform.meteorology import build_meteorology

    path = era5_file(
        tmp_path / "era5.nc",
        "2019-08-01 00:00",
        16,
        lambda times: np.full(len(times), 303.15),
        lambda times: np.full(len(times), 293.15),
    )
    output = tmp_path / "out" / "meteo.parquet"
    series = build_meteorology(
        [path], write_population(tmp_path, POPULATION), write_regions(tmp_path), output
    )
    assert series.columns == ["date", "health_region_code", "population", "temperature", "humidity"]
    assert series.height == 2
    assert series["temperature"].to_list() == pytest.approx([30.0, 30.0], abs=1e-4)
    assert series["humidity"].to_list() == pytest.approx([55.1, 55.1], abs=0.2)
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert manifest["attribution"][0].startswith("Contains modified Copernicus")
    assert "{year}" not in manifest["attribution"][0]
