"""MERRA-2 surface PM2.5 over Brazil, for the CAMS sensitivity analysis.

GES DISC retired its own OPeNDAP; subsets come from Earthdata Cloud OPeNDAP (DAP4),
one day at a time, with only the five aerosol species and the Brazil window. Each
month is reduced to local-day (UTC−3) means on the MERRA-2 grid and the hourly data are
discarded; the manifest keeps every granule name and response hash.

PM2.5 follows Buchard et al. (2016): dust and sea salt below 2.5 µm, black carbon,
organic carbon × 1.4 (organic matter) and sulphate × 1.375 (ammonium sulphate).
The Earthdata password stays in ~/.netrc. URS returns the account's existing bearer
token (or creates one, valid about 60 days); it is held in memory and never written.
"""

import hashlib
import json
import netrc
import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .sih_national import software_provenance

COLLECTION = "C1276812830-GES_DISC"  # M2T1NXAER 5.12.4, tavg1_2d_aer_Nx
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
OPENDAP = "https://opendap.earthdata.nasa.gov/collections/{collection}/granules/{granule}"
TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
# Grid indices of lat −35..6 (0.5°) and lon −75..−31.875 (0.625°): one cell past −32
# keeps Fernando de Noronha inside the window.
LAT, LON = "[110:192]", "[168:237]"
SPECIES = {"DUSMASS25": 1.0, "SSSMASS25": 1.0, "BCSMASS": 1.0, "OCSMASS": 1.4, "SO4SMASS": 1.375}
ALLOWED_HOSTS = {"opendap.earthdata.nasa.gov", "urs.earthdata.nasa.gov"}
GRANULE = re.compile(r"M2T1NXAER\.5\.12\.4:MERRA2_\d{3}\.tavg1_2d_aer_Nx\.(\d{8})\.nc4")
# Everything that shapes a stored month; a cached month is reused only if they match.
MONTH_PARAMETERS = {
    "collection": COLLECTION,
    "lat": LAT,
    "lon": LON,
    "species": SPECIES,
    "utc_offset_hours": -3,
}


def bearer_token() -> str:
    try:
        credentials = netrc.netrc().authenticators("urs.earthdata.nasa.gov")
    except (OSError, netrc.NetrcParseError):
        # The parse error can quote a token of the file, possibly the password.
        raise RuntimeError("~/.netrc is missing or malformed") from None
    if credentials is None:
        raise RuntimeError("Earthdata login missing from ~/.netrc")
    login, _, password = credentials
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        response = client.post(TOKEN_URL, auth=(login, password))
    if response.status_code != 200:
        raise RuntimeError(f"Earthdata token request failed ({response.status_code})")
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Earthdata token response without access_token")
    return token


def month_granules(client: httpx.Client, year: int, month: int) -> dict[date, str]:
    """Granule id per day; the file prefix (400/401) changes for reprocessed months."""
    first = date(year, month, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    response = client.get(
        CMR,
        params={
            "collection_concept_id": COLLECTION,
            "temporal": f"{first}T00:00:00Z,{last}T23:59:59Z",
            "page_size": 100,
        },
    )
    response.raise_for_status()
    granules = {}
    for entry in response.json()["feed"]["entry"]:
        title = entry["title"]  # M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.20190815.nc4
        # The title becomes part of the OPeNDAP URL, which carries the token.
        match = GRANULE.fullmatch(title)
        if not match:
            raise ValueError(f"Unexpected MERRA-2 granule name: {title[:80]!r}")
        day = datetime.strptime(match[1], "%Y%m%d").date()
        # CMR also returns granules that merely touch the month's edges.
        if (day.year, day.month) != (year, month):
            continue
        if day in granules:
            raise ValueError(f"Two MERRA-2 granules for {day}")
        granules[day] = title
    return granules


def fetch_day(client: httpx.Client, granule: str) -> tuple[bytes, str]:
    constraint = ";".join(
        [f"/{name}[0:23]{LAT}{LON}" for name in SPECIES] + [f"/lat{LAT}", f"/lon{LON}", "/time"]
    )
    url = OPENDAP.format(collection=COLLECTION, granule=granule.replace(":", "%3A"))
    response = client.get(url + ".dap.nc4", params={"dap4.ce": constraint})
    if response.status_code != 200 or response.content[:4] != b"\x89HDF":
        raise RuntimeError(f"MERRA-2 subset failed for {granule} ({response.status_code})")
    return response.content, hashlib.sha256(response.content).hexdigest()


def hourly_pm25(payload: bytes):
    """µg/m³ PM2.5 (time, latitude, longitude) from one daily subset."""
    import xarray as xr

    # netCDF4 reads only from paths; the file lives just long enough to be loaded.
    with tempfile.NamedTemporaryFile(suffix=".nc4") as handle:
        handle.write(payload)
        handle.flush()
        with xr.open_dataset(handle.name, engine="netcdf4") as ds:
            for name in SPECIES:
                if ds[name].attrs.get("units") != "kg m-3":
                    raise ValueError(f"Unexpected MERRA-2 units for {name}")
            total = sum(ds[name] * factor for name, factor in SPECIES.items()) * 1e9
            field = total.rename({"lat": "latitude", "lon": "longitude", "time": "valid_time"})
            return field.load()


def extract_month(
    year: int,
    month: int,
    root: Path,
    *,
    client: httpx.Client,
    catalog: httpx.Client | None = None,
    fetch: Callable = fetch_day,
    workers: int = 4,
) -> dict:
    """Local-day mean grid for one month, saved as NetCDF with a manifest."""
    import xarray as xr

    from ..transform.cams_exposure import local_daily_means

    catalog = catalog if catalog is not None else client
    staging = root / "staging" / "merra2"
    output = staging / f"{year}{month:02d}.nc"
    manifest_path = output.with_suffix(".source.json")
    if output.exists() and manifest_path.exists():
        metadata = json.loads(manifest_path.read_text())
        # Months written before parameters were recorded used these same values.
        if metadata.get("parameters", MONTH_PARAMETERS) == MONTH_PARAMETERS and metadata.get(
            "netcdf_sha256", _sha256(output)
        ) == _sha256(output):
            return metadata
    granules = month_granules(catalog, year, month)
    following = (date(year, month, 1) + timedelta(days=32)).replace(day=1)
    days = sorted(granules)
    expected = (following - date(year, month, 1)).days
    if len(days) != expected:
        raise ValueError(f"MERRA-2 {year}-{month:02d}: {len(days)} of {expected} days")
    # The last local day ends at 03 UTC of the next month's first day.
    extra = month_granules(catalog, following.year, following.month).get(following)
    if extra is None:
        # Without it the month's last local day is incomplete; never cache a short month.
        raise ValueError(f"MERRA-2 {following} not published yet; end the range a month earlier")
    wanted = [granules[d] for d in days] + ([extra] if extra else [])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda g: fetch(client, g), wanted))
    fields = [hourly_pm25(payload) for payload, _ in results]
    hourly = xr.concat(fields, dim="valid_time")
    daily = local_daily_means(hourly, name="MERRA-2 PM2.5", steps_per_day=24)
    daily = daily.sel(date=(daily["date"].dt.month == month))
    if daily.sizes["date"] != expected:
        raise ValueError(f"MERRA-2 {year}-{month:02d}: {daily.sizes['date']} complete local days")
    staging.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(output.name + ".partial")
    try:
        daily.astype("float32").rename("pm25").to_netcdf(staged)
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    metadata = {
        "parameters": MONTH_PARAMETERS,
        "netcdf_sha256": _sha256(output),
        "year": year,
        "month": month,
        "days": int(daily.sizes["date"]),
        "granules": [
            {"name": g, "sha256": sha} for g, (_, sha) in zip(wanted, results, strict=True)
        ],
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(json.dumps(metadata, indent=2))
    return metadata


def months(start: date, end: date) -> list[tuple[int, int]]:
    result, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def _official_only(request: httpx.Request) -> None:
    # A request hook runs before anything is sent, so a redirect away from NASA hosts
    # is stopped before it leaves; httpx also drops Authorization across hosts.
    if request.url.scheme != "https" or request.url.host not in ALLOWED_HOSTS:
        raise ValueError(f"Unexpected MERRA-2 host: {request.url.host}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_merra2(
    start: date,
    end: date,
    population_raster: Path,
    regions_geojson: Path,
    output: Path,
    root: Path = Path("data"),
    *,
    progress: Callable[[str], None] = print,
):
    """Population-weighted daily MERRA-2 PM2.5 per health region."""
    import xarray as xr

    from ..transform.cams_exposure import (
        describe_input,
        population_cell_weights,
        regional_daily_means,
        write_with_manifest,
    )

    manifests = []
    with (
        httpx.Client(
            timeout=httpx.Timeout(300, connect=20),
            follow_redirects=True,
            headers={"Authorization": f"Bearer {bearer_token()}"},
            event_hooks={"request": [_official_only]},
            trust_env=False,
        ) as client,
        # CMR search is public: the token is not sent there.
        httpx.Client(timeout=httpx.Timeout(60, connect=20), trust_env=False) as catalog,
    ):
        for count, (year, month) in enumerate(months(start, end), start=1):
            manifests.append(extract_month(year, month, root, client=client, catalog=catalog))
            if count % 6 == 0:
                progress(f"MERRA-2: {count} meses")
    files = [root / "staging" / "merra2" / f"{y}{m:02d}.nc" for y, m in months(start, end)]
    grids = []
    for path in files:
        with xr.open_dataarray(path) as grid:
            grids.append(grid.load())
    daily = xr.concat(grids, dim="date").astype("float64")
    snapping = {}
    weights = population_cell_weights(
        population_raster,
        regions_geojson,
        latitudes=daily["latitude"].values,
        longitudes=daily["longitude"].values,
        max_snap_steps=1.0,
        report=snapping,
    )
    series = regional_daily_means(daily, weights, column="pm25_merra2", name="MERRA-2")
    write_with_manifest(
        output,
        series.write_parquet,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "rows": series.height,
            "dates": [str(series["date"].min()), str(series["date"].max())],
            "pm25": "DUST25 + SS25 + BC + 1.4 OC + 1.375 SO4 (Buchard et al. 2016), µg/m³",
            "local_day": "UTC-3, mean of 24 hourly averages",
            "snapped_beyond_grid": snapping,
            "parameters": MONTH_PARAMETERS,
            "months": [f"{m['year']}-{m['month']:02d}" for m in manifests],
            "software": software_provenance(),
            "granules_sha256": hashlib.sha256(
                json.dumps(
                    [[g["name"], g["sha256"]] for m in manifests for g in m["granules"]]
                ).encode()
            ).hexdigest(),
            "attribution": "Global Modeling and Assimilation Office (GMAO), MERRA-2 "
            "tavg1_2d_aer_Nx, DOI 10.5067/KLICLTZ8EM9D",
            "inputs": {
                "population": [describe_input(population_raster)],
                "regions": [describe_input(regions_geojson)],
            },
        },
    )
    return series
