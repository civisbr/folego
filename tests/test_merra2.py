from datetime import date

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")

from folego.ingest import merra2  # noqa: E402

LATS = (-0.5, 0.0)
LONS = (0.0, 0.625)


def granule_bytes(tmp_path, day: date, value_ug: float) -> bytes:
    times = pd.date_range(f"{day} 00:30", periods=24, freq="1h")
    shape = (24, len(LATS), len(LONS))
    data = {}
    for name, factor in merra2.SPECIES.items():
        # Each species carries an equal share of the total after its factor.
        mass = np.full(shape, value_ug / len(merra2.SPECIES) / factor * 1e-9, dtype="float32")
        data[name] = xr.DataArray(
            mass,
            dims=("time", "lat", "lon"),
            coords={"time": times, "lat": list(LATS), "lon": list(LONS)},
            attrs={"units": "kg m-3"},
        )
    path = tmp_path / f"{day}.nc4"
    xr.Dataset(data).to_netcdf(path, engine="netcdf4")
    return path.read_bytes()


def test_species_are_combined_with_mass_factors(tmp_path):
    field = merra2.hourly_pm25(granule_bytes(tmp_path, date(2019, 8, 15), 20.0))
    assert field.dims == ("valid_time", "latitude", "longitude")
    np.testing.assert_allclose(field.values, 20.0, rtol=1e-5)


def test_month_gives_local_day_means_and_uses_next_month_first_day(tmp_path, monkeypatch):
    days = pd.date_range("2019-02-01", "2019-03-01").date

    def granules(client, year, month, product=None):
        return {
            d: f"M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.{d:%Y%m%d}.nc4"
            for d in days
            if d.month == month
        }

    payloads = {d: granule_bytes(tmp_path, d, float(d.day)) for d in days}

    def fetch(client, granule, product):
        day = pd.Timestamp(granule.rsplit(".", 2)[-2]).date()
        return payloads[day], f"sha-{day}"

    monkeypatch.setattr(merra2, "month_granules", granules)
    manifest = merra2.extract_month(2019, 2, tmp_path / "data", client=None, fetch=fetch)
    assert manifest["days"] == 28 and len(manifest["granules"]) == 29
    daily = xr.open_dataarray(tmp_path / "data" / "staging" / "merra2" / "201902.nc")
    assert daily.sizes["date"] == 28
    # Local 28 Feb: 21 hours of day 28 and 3 hours of 1 Mar.
    expected = (21 * 28 + 3 * 1) / 24
    np.testing.assert_allclose(
        daily.sel(date=np.datetime64("2019-02-28")).values, expected, rtol=1e-5
    )


def test_missing_day_in_month_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        merra2,
        "month_granules",
        lambda client, year, month, product=None: {date(year, month, 1): "x"},
    )
    with pytest.raises(ValueError, match="1 of 28 days"):
        merra2.extract_month(2019, 2, tmp_path, client=None, fetch=lambda c, g, p: None)


def test_redirect_to_a_foreign_host_is_stopped_before_it_is_sent():
    import httpx

    sent = []

    def handler(request):
        sent.append(request.url.host)
        if request.url.host == "opendap.earthdata.nasa.gov":
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"Authorization": "Bearer secret"},
        event_hooks={"request": [merra2._official_only]},
    )
    with pytest.raises(ValueError, match="Unexpected"):
        client.get("https://opendap.earthdata.nasa.gov/x")
    assert sent == ["opendap.earthdata.nasa.gov"]


def test_month_without_next_day_published_is_not_cached(tmp_path, monkeypatch):
    days = pd.date_range("2019-02-01", "2019-02-28").date

    def granules(client, year, month, product=None):
        return {
            d: f"M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.{d:%Y%m%d}.nc4"
            for d in days
            if d.month == month
        }

    payloads = {d: granule_bytes(tmp_path, d, 5.0) for d in days}
    monkeypatch.setattr(merra2, "month_granules", granules)
    with pytest.raises(ValueError, match="not published yet"):
        merra2.extract_month(
            2019,
            2,
            tmp_path / "data",
            client=None,
            fetch=lambda c, g, p: (payloads[pd.Timestamp(g.rsplit(".", 2)[-2]).date()], "s"),
        )
    assert not (tmp_path / "data" / "staging" / "merra2" / "201902.nc").exists()


def test_catalog_keeps_only_days_of_the_month_and_valid_names():
    import httpx

    entries = [
        {"title": "M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.20190131.nc4"},
        {"title": "M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.20190201.nc4"},
    ]

    def handler(request):
        return httpx.Response(200, json={"feed": {"entry": entries}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert list(merra2.month_granules(client, 2019, 2)) == [date(2019, 2, 1)]
    entries.append({"title": "M2T1NXAER.5.12.4:../../x?.20190202.nc4"})
    with pytest.raises(ValueError, match="Unexpected MERRA-2 granule"):
        merra2.month_granules(client, 2019, 2)


def weather_bytes(tmp_path, day: date, temperature_k, dewpoint_k) -> bytes:
    times = pd.date_range(f"{day} 00:30", periods=24, freq="1h")
    shape = (24, len(LATS), len(LONS))

    def array(value):
        return xr.DataArray(
            np.full(shape, value, dtype="float32"),
            dims=("time", "lat", "lon"),
            coords={"time": times, "lat": list(LATS), "lon": list(LONS)},
            attrs={"units": "K"},
        )

    path = tmp_path / f"slv-{day}.nc4"
    xr.Dataset({"T2M": array(temperature_k), "T2MDEW": array(dewpoint_k)}).to_netcdf(
        path, engine="netcdf4"
    )
    return path.read_bytes()


def test_weather_month_matches_era5_humidity_and_clamps_dew_point(tmp_path, monkeypatch):
    from folego.transform.meteorology import relative_humidity

    days = pd.date_range("2019-02-01", "2019-03-01").date
    product = merra2.WEATHER

    def granules(client, year, month, product=product):
        return {
            d: f"M2T1NXSLV.5.12.4:MERRA2_400.tavg1_2d_slv_Nx.{d:%Y%m%d}.nc4"
            for d in days
            if d.month == month
        }

    # Day 5 has fog: dew point 0.2 K above temperature.
    payloads = {
        d: weather_bytes(tmp_path, d, 303.15, 293.15 if d.day != 5 else 303.35) for d in days
    }

    def fetch(client, granule, product):
        return payloads[pd.Timestamp(granule.rsplit(".", 2)[-2]).date()], "s"

    monkeypatch.setattr(merra2, "month_granules", granules)
    manifest = merra2.extract_month(
        2019, 2, tmp_path / "data", client=None, fetch=fetch, product=product
    )
    with xr.open_dataset(tmp_path / "data" / "staging" / "merra2-slv" / "201902.nc") as ds:
        np.testing.assert_allclose(ds["temperature"].values, 30.0, atol=1e-4)
        expected = relative_humidity(np.array([30.0]), np.array([20.0]))[0]
        day1 = ds["humidity"].sel(date=np.datetime64("2019-02-01")).values
        np.testing.assert_allclose(day1, expected, rtol=1e-5)
        assert ds["humidity"].max() <= 100.0
        # Local 4 Feb takes 3 fog hours (00–03 UTC of the 5th) and local 5 Feb 21.
        day4 = float(ds["humidity"].sel(date=np.datetime64("2019-02-04")).values[0, 0])
        assert day4 == pytest.approx((21 * expected + 3 * 100.0) / 24, rel=1e-5)
    # All 24 fog hours fall inside February's local days; none is counted twice.
    assert manifest["dewpoint_clamped_steps"] == 24 * len(LATS) * len(LONS)


def test_large_dew_point_excess_aborts(tmp_path):
    with pytest.raises(ValueError, match="5 K"):
        merra2.hourly_weather(weather_bytes(tmp_path, date(2019, 2, 1), 290.0, 300.0))


def test_months_written_by_the_first_version_are_reused(tmp_path, monkeypatch):
    staging = tmp_path / "data" / "staging" / "merra2"
    staging.mkdir(parents=True)
    grid = xr.DataArray(
        np.full((28, 2, 2), 7.0, dtype="float32"),
        dims=("date", "latitude", "longitude"),
        coords={
            "date": pd.date_range("2019-02-01", periods=28),
            "latitude": list(LATS),
            "longitude": list(LONS),
        },
    )
    grid.rename("pm25").to_netcdf(staging / "201902.nc")
    (staging / "201902.source.json").write_text('{"year": 2019, "month": 2, "days": 28}')

    def refuse(*args, **kwargs):
        raise AssertionError("cached month fetched again")

    monkeypatch.setattr(merra2, "month_granules", refuse)
    merra2.extract_month(2019, 2, tmp_path / "data", client=None)
    loaded = merra2._load(
        tmp_path / "data", merra2.AEROSOL, date(2019, 2, 1), date(2019, 2, 1), ("pm25",)
    )
    assert float(loaded["pm25"].mean()) == pytest.approx(7.0)
    # A weather month without recorded parameters is not trusted.
    slv = tmp_path / "data" / "staging" / "merra2-slv"
    slv.mkdir(parents=True)
    grid.to_dataset(name="temperature").to_netcdf(slv / "201902.nc")
    (slv / "201902.source.json").write_text('{"year": 2019, "month": 2}')
    with pytest.raises(AssertionError, match="fetched again"):
        merra2.extract_month(2019, 2, tmp_path / "data", client=None, product=merra2.WEATHER)


def test_transient_failures_are_retried():
    import httpx

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200)

    assert merra2._retry(flaky, pause=0).status_code == 200
    assert len(calls) == 3


def test_retries_end_with_the_last_answer_or_error_and_skip_client_errors():
    import httpx

    assert merra2._retry(lambda: httpx.Response(503), attempts=2, pause=0).status_code == 503
    calls = []

    def missing():
        calls.append(1)
        return httpx.Response(404)

    assert merra2._retry(missing, pause=0).status_code == 404 and len(calls) == 1

    def disconnected():
        raise httpx.RemoteProtocolError("Server disconnected")

    with pytest.raises(httpx.RemoteProtocolError):
        merra2._retry(disconnected, attempts=2, pause=0)
