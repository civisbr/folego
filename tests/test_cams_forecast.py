import json
import zipfile
from datetime import date

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("rasterio")


def _forecast_zip(
    path,
    issues,
    *,
    steps=range(3, 121, 3),
    name="data_sfc.nc",
    hour=0,
    integer_steps=False,
    longitudes=(0.0, 0.75),
):
    """ADS layout: pm2p5 over (forecast_period, forecast_reference_time, lat, lon)."""
    periods = np.array([np.timedelta64(h, "h") for h in steps], dtype="timedelta64[ns]")
    references = np.array(
        [np.datetime64(f"{d}T{hour:02d}:00") for d in issues], dtype="datetime64[ns]"
    )
    # Lead day k (local day issue + k at UTC-3) holds (k + 1) × 10 µg/m³.
    lead = np.array([(h - 3) // 24 for h in steps])
    values = np.broadcast_to(
        ((lead + 1) * 1e-8)[:, None, None, None], (len(steps), len(issues), 2, 2)
    ).copy()
    field = xr.DataArray(
        values,
        dims=("forecast_period", "forecast_reference_time", "latitude", "longitude"),
        coords={
            "forecast_period": periods,
            "forecast_reference_time": references,
            "latitude": [0.0, -0.75],
            "longitude": list(longitudes),
            "valid_time": (
                ("forecast_period", "forecast_reference_time"),
                periods[:, None] + references[None, :],
            ),
        },
        attrs={"units": "kg m**-3"},
    )
    if integer_steps:
        # How ADS files store the steps: plain integers in hours.
        field = field.assign_coords(
            forecast_period=("forecast_period", np.array(list(steps)), {"units": "hours"})
        )
    netcdf = path.with_suffix(".nc")
    field.to_dataset(name="pm2p5").to_netcdf(netcdf)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.write(netcdf, name)
    netcdf.unlink()
    return path


def _inputs(tmp_path):
    from geo_helpers import POPULATION, write_population, write_regions

    return write_population(tmp_path, POPULATION), write_regions(tmp_path)


def test_lead_days_follow_the_local_day_of_each_run(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01", "2019-08-02"])
    output = tmp_path / "out" / "forecast.parquet"
    series = build_forecast_exposure([archive], *_inputs(tmp_path), output)
    assert series.height == 2 * 5 * 2  # issues × lead days × regions
    first = series.filter(series["issue_date"] == date(2019, 8, 1)).sort("lead_day")
    a = first.filter(first["health_region_code"] == "A")
    assert a["lead_day"].to_list() == [0, 1, 2, 3, 4]
    assert a["date"].to_list() == [date(2019, 8, d) for d in range(1, 6)]
    assert a["pm25_forecast"].to_list() == pytest.approx([10, 20, 30, 40, 50])
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert manifest["issue_dates"] == ["2019-08-01", "2019-08-02"]
    assert manifest["inputs"]["cams_forecasts"][0]["sha256"]
    assert "forecasts" in manifest["attribution"][0]


def test_incomplete_last_day_is_dropped(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], steps=range(3, 118, 3))
    series = build_forecast_exposure([archive], *_inputs(tmp_path), tmp_path / "o.parquet")
    assert sorted(set(series["lead_day"].to_list())) == [0, 1, 2, 3]


def test_overlapping_files_are_refused(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    first = _forecast_zip(tmp_path / "a.zip", ["2019-08-01"])
    second = _forecast_zip(tmp_path / "b.zip", ["2019-08-01"])
    with pytest.raises(ValueError, match="Repeated"):
        build_forecast_exposure([first, second], *_inputs(tmp_path), tmp_path / "o.parquet")


def test_zip_member_paths_cannot_escape(tmp_path):
    from folego.transform.cams_forecast import _netcdf_members

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../escape.nc", b"x")
        bundle.writestr("notes.txt", b"y")
    out = tmp_path / "extract"
    out.mkdir()
    (member,) = _netcdf_members(archive, out)
    assert member.parent == out and not (tmp_path.parent / "escape.nc").exists()


def test_zip_without_netcdf_is_refused(tmp_path):
    from folego.transform.cams_forecast import _netcdf_members

    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("readme.txt", b"y")
    with pytest.raises(ValueError, match="No NetCDF"):
        _netcdf_members(archive, tmp_path)


def test_integer_hour_steps_are_decoded(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], integer_steps=True)
    series = build_forecast_exposure([archive], *_inputs(tmp_path), tmp_path / "o.parquet")
    a = series.filter(series["health_region_code"] == "A").sort("lead_day")
    assert a["lead_day"].to_list() == [0, 1, 2, 3, 4]
    assert a["pm25_forecast"].to_list() == pytest.approx([10, 20, 30, 40, 50])


def test_twelve_utc_run_is_refused(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], hour=12)
    with pytest.raises(ValueError, match="00 UTC"):
        build_forecast_exposure([archive], *_inputs(tmp_path), tmp_path / "o.parquet")


def test_too_many_or_oversized_members_are_refused(tmp_path, monkeypatch):
    from folego.transform import cams_forecast

    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for i in range(cams_forecast.MAX_MEMBERS + 1):
            bundle.writestr(f"{i}.nc", b"x")
    with pytest.raises(ValueError, match="Too many"):
        cams_forecast._netcdf_members(archive, tmp_path)
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w") as bundle:
        bundle.writestr("a.nc", b"x" * 100)
    monkeypatch.setattr(cams_forecast, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError, match="Oversized"):
        cams_forecast._netcdf_members(big, tmp_path)


def test_island_two_steps_past_the_grid_edge_is_snapped_and_reported(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    # Region B's eastern pixels sit 1.25 grid steps past the last column, as Fernando
    # de Noronha does on the 0.4° forecast grid.
    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], longitudes=(-0.75, 0.0))
    output = tmp_path / "o.parquet"
    series = build_forecast_exposure([archive], *_inputs(tmp_path), output)
    assert set(series["health_region_code"].to_list()) == {"A", "B"}
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    (grid,) = manifest["snapped_beyond_grid"]
    assert grid["snapped_regions"] == ["B"] and grid["first_issue"] == "2019-08-01"
    assert manifest["max_snap_steps"] == 2.0


def test_pixels_beyond_the_snap_limit_are_refused(tmp_path):
    from folego.transform.cams_forecast import build_forecast_exposure

    # Region B's eastern pixels now sit 2.25 steps past the last column.
    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], longitudes=(-1.5, -0.75))
    with pytest.raises(ValueError, match="outside the CAMS grid"):
        build_forecast_exposure([archive], *_inputs(tmp_path), tmp_path / "o.parquet")


def test_too_many_snapped_people_are_refused(tmp_path, monkeypatch):
    from folego.transform import cams_forecast

    monkeypatch.setattr(cams_forecast, "MAX_SNAPPED_POPULATION", 10)
    archive = _forecast_zip(tmp_path / "f.zip", ["2019-08-01"], longitudes=(-0.75, 0.0))
    with pytest.raises(ValueError, match="people snapped"):
        cams_forecast.build_forecast_exposure([archive], *_inputs(tmp_path), tmp_path / "o.parquet")
