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

    def granules(client, year, month):
        return {
            d: f"M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.{d:%Y%m%d}.nc4"
            for d in days
            if d.month == month
        }

    payloads = {d: granule_bytes(tmp_path, d, float(d.day)) for d in days}

    def fetch(client, granule):
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
        lambda client, year, month: {date(year, month, 1): "x"},
    )
    with pytest.raises(ValueError, match="1 of 28 days"):
        merra2.extract_month(2019, 2, tmp_path, client=None, fetch=lambda c, g: None)


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

    def granules(client, year, month):
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
            fetch=lambda c, g: (payloads[pd.Timestamp(g.rsplit(".", 2)[-2]).date()], "s"),
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
