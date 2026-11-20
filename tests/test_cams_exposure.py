import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
rasterio = pytest.importorskip("rasterio")

from geo_helpers import POPULATION, write_population, write_regions  # noqa: E402

from folego.transform.cams_exposure import (  # noqa: E402
    build_exposure,
    daily_cell_means,
    population_cell_weights,
    regional_daily_pm25,
)

LATS = (0.0, -0.75)
LONS = (0.0, 0.75)


def cams_file(tmp_path, values_by_time, *, lats=LATS, lons=LONS, transpose=False, units=None):
    times = pd.to_datetime(list(values_by_time))
    data = np.stack(
        [np.full((len(lats), len(lons)), v, dtype="float32") for v in values_by_time.values()]
    )
    field = xr.DataArray(
        data * 1e-9,
        dims=("valid_time", "latitude", "longitude"),
        coords={
            "valid_time": times,
            "latitude": np.asarray(lats, dtype="float32"),
            "longitude": np.asarray(lons, dtype="float32"),
        },
        attrs={"units": units or "kg m**-3"},
    )
    if transpose:
        field = field.transpose("longitude", "valid_time", "latitude")
    path = tmp_path / "cams.nc"
    xr.Dataset({"pm2p5": field}).to_netcdf(path)
    return path


def three_hourly(start, days, value):
    stamps = pd.date_range(start, periods=8 * days, freq="3h")
    return {str(t): value(t) for t in stamps}


def day_value(daily, day):
    return daily.sel(date=np.datetime64(day)).values


def test_daily_means_use_local_day_and_micrograms(tmp_path):
    # Local day D (UTC-3) spans 03:00 UTC of D to 00:00 UTC of D+1.
    values = three_hourly("2019-08-01 00:00", 3, lambda t: 10.0 if t.day == 2 else 40.0)
    daily = daily_cell_means(cams_file(tmp_path, values), utc_offset_hours=-3)
    assert day_value(daily, "2019-08-02") == pytest.approx(np.full((2, 2), (7 * 10 + 40) / 8))
    assert [str(d)[:10] for d in daily["date"].values] == ["2019-08-01", "2019-08-02"]
    assert daily.dtype == np.float64 and daily["latitude"].dtype == np.float64


def test_incomplete_local_days_are_dropped_not_averaged(tmp_path):
    values = three_hourly("2019-08-01 00:00", 1, lambda t: 20.0)
    assert daily_cell_means(cams_file(tmp_path, values)).sizes["date"] == 0


def test_dimension_order_and_longitude_convention_do_not_matter(tmp_path):
    values = three_hourly("2019-08-01 00:00", 2, lambda t: 30.0)
    reference = daily_cell_means(cams_file(tmp_path, values))
    other = tmp_path / "other"
    other.mkdir()
    shifted = daily_cell_means(cams_file(other, values, lons=(360.0, 0.75), transpose=True))
    assert shifted["longitude"].values.tolist() == [0.0, 0.75]
    np.testing.assert_allclose(shifted.values, reference.values)


def test_missing_values_and_unknown_units_fail(tmp_path):
    values = three_hourly("2019-08-01 00:00", 2, lambda t: np.nan if t.hour == 6 else 5.0)
    with pytest.raises(ValueError, match="Missing"):
        daily_cell_means(cams_file(tmp_path, values))
    other = tmp_path / "units"
    other.mkdir()
    good = three_hourly("2019-08-01 00:00", 2, lambda t: 5.0)
    with pytest.raises(ValueError, match="units"):
        daily_cell_means(cams_file(other, good, units="ppb"))


def test_population_is_split_by_cams_cell_and_region(tmp_path):
    weights = population_cell_weights(
        write_population(tmp_path, POPULATION),
        write_regions(tmp_path),
        latitudes=LATS,
        longitudes=LONS,
    )
    got = {
        (r["latitude"], r["longitude"], r["health_region_code"]): r["population"]
        for r in weights.iter_rows(named=True)
    }
    assert got == {
        (0.0, 0.0, "A"): 10.0,
        (0.0, 0.75, "B"): 100.0,
        (-0.75, 0.0, "A"): 26.0,
        (-0.75, 0.75, "B"): 110.0,
    }


def test_populated_pixels_beyond_the_grid_fail_instead_of_snapping(tmp_path):
    # Only lon 0 and -0.75 exist: the east block (centres 0.56, 0.94) is 0.75-1.25 steps away.
    with pytest.raises(ValueError, match="outside the CAMS grid"):
        population_cell_weights(
            write_population(tmp_path, POPULATION),
            write_regions(tmp_path),
            latitudes=LATS,
            longitudes=(-0.75, 0.0),
        )


def test_snapping_up_to_the_limit_is_reported(tmp_path):
    report = {}
    weights = population_cell_weights(
        write_population(tmp_path, POPULATION),
        write_regions(tmp_path),
        latitudes=LATS,
        longitudes=(-0.75, 0.0),
        max_snap_steps=1.25,
        report=report,
    )
    assert report == {"snapped_pixels": 6, "snapped_population": 210.0, "snapped_regions": ["B"]}
    assert weights["population"].sum() == 246.0


def test_region_without_population_fails(tmp_path):
    regions = write_regions(tmp_path, {"A": (-0.375, 0.375), "B": (0.375, 1.125), "C": (5, 6)})
    with pytest.raises(ValueError, match="no populated pixel"):
        population_cell_weights(
            write_population(tmp_path, POPULATION), regions, latitudes=LATS, longitudes=LONS
        )


def test_projected_population_raster_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="EPSG:4326"):
        population_cell_weights(
            write_population(tmp_path, POPULATION, crs="EPSG:3857"),
            write_regions(tmp_path),
            latitudes=LATS,
            longitudes=LONS,
        )


def test_regional_series_weights_cells_by_population(tmp_path):
    values = three_hourly("2019-08-01 00:00", 2, lambda t: 7.0)
    daily = daily_cell_means(cams_file(tmp_path, values)).copy()
    daily.loc[{"latitude": 0.0, "longitude": 0.0}] = 100.0
    daily.loc[{"latitude": -0.75, "longitude": 0.0}] = 10.0
    weights = population_cell_weights(
        write_population(tmp_path, POPULATION),
        write_regions(tmp_path),
        latitudes=LATS,
        longitudes=LONS,
    )
    series = regional_daily_pm25(daily, weights)
    a = series.filter(series["health_region_code"] == "A")
    assert a["pm25"].to_list() == pytest.approx([(100 * 10 + 10 * 26) / 36])
    assert a["population"].to_list() == [36.0]
    b = series.filter(series["health_region_code"] == "B")
    assert b["pm25"].to_list() == pytest.approx([7.0])


def test_build_exposure_records_inputs_and_attribution(tmp_path):
    values = three_hourly("2019-08-01 00:00", 2, lambda t: 12.0)
    output = tmp_path / "out" / "pm25.parquet"
    series = build_exposure(
        cams_file(tmp_path, values),
        write_population(tmp_path, POPULATION),
        write_regions(tmp_path),
        output,
        attribution=("WorldPop test",),
    )
    assert series["date"].to_list() == [date(2019, 8, 1)] * 2
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert set(manifest["inputs"]) == {"cams", "population", "regions"}
    assert all(len(item[0]["sha256"]) == 64 for item in manifest["inputs"].values())
    assert manifest["attribution"][-1] == "WorldPop test"
    assert manifest["rows"] == 2


def test_repeated_analysis_times_fail(tmp_path):
    values = three_hourly("2019-08-01 00:00", 2, lambda t: 5.0)
    path = cams_file(tmp_path, values)
    with xr.open_dataset(path) as ds:
        doubled = xr.concat([ds, ds.isel(valid_time=[3])], dim="valid_time").load()
    other = tmp_path / "doubled.nc"
    doubled.to_netcdf(other)
    with pytest.raises(ValueError, match="Repeated"):
        daily_cell_means(other)
