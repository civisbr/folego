"""MERRA-2 surface PM2.5 and 2 m weather over Brazil.

PM2.5 feeds the CAMS sensitivity analysis; temperature and humidity stand in for ERA5
when the Copernicus queue is unavailable. GES DISC retired its own OPeNDAP; subsets
come from Earthdata Cloud OPeNDAP (DAP4), one day at a time, with only the needed
variables and the Brazil window. Each month is reduced to local-day (UTC−3) means on
the MERRA-2 grid and the hourly data are discarded; the manifest keeps every granule
name and response hash.

PM2.5 follows Buchard et al. (2016): dust and sea salt below 2.5 µm, black carbon,
organic carbon × 1.4 (organic matter) and sulphate × 1.375 (ammonium sulphate).
Relative humidity uses the same per-hour Magnus formula as the ERA5 series.
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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from .sih_national import software_provenance

CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
OPENDAP = "https://opendap.earthdata.nasa.gov/collections/{collection}/granules/{granule}"
TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
# Grid indices of lat −35..6 (0.5°) and lon −75..−31.875 (0.625°): one cell past −32
# keeps Fernando de Noronha inside the window.
LAT, LON = "[110:192]", "[168:237]"
SPECIES = {"DUSMASS25": 1.0, "SSSMASS25": 1.0, "BCSMASS": 1.0, "OCSMASS": 1.4, "SO4SMASS": 1.375}
ALLOWED_HOSTS = {"opendap.earthdata.nasa.gov", "urs.earthdata.nasa.gov"}
COLLECTION = "C1276812830-GES_DISC"  # M2T1NXAER 5.12.4, tavg1_2d_aer_Nx
# Everything that shapes a stored month; a cached month is reused only if they match.
MONTH_PARAMETERS = {
    "collection": COLLECTION,
    "lat": LAT,
    "lon": LON,
    "species": SPECIES,
    "utc_offset_hours": -3,
}
# Dew point above temperature by more than this only comes from a corrupt file.
DEWPOINT_ABORT_K = 5.0


@dataclass(frozen=True)
class Product:
    kind: str  # "aerosol" or "weather": selects the reduction and the output layout
    short_name: str
    stream: str
    collection: str
    variables: tuple[str, ...]
    staging: str
    parameters: dict = field(hash=False)
    doi: str = ""

    @property
    def granule(self) -> re.Pattern:
        return re.compile(
            rf"{re.escape(self.short_name)}\.5\.12\.4:MERRA2_\d{{3}}\."
            rf"{re.escape(self.stream)}\.(\d{{8}})\.nc4"
        )


AEROSOL = Product(
    kind="aerosol",
    short_name="M2T1NXAER",
    stream="tavg1_2d_aer_Nx",
    collection=COLLECTION,
    variables=tuple(SPECIES),
    staging="merra2",
    parameters=MONTH_PARAMETERS,
    doi="10.5067/KLICLTZ8EM9D",
)
WEATHER = Product(
    kind="weather",
    short_name="M2T1NXSLV",
    stream="tavg1_2d_slv_Nx",
    collection="C1276812863-GES_DISC",
    variables=("T2M", "T2MDEW"),
    staging="merra2-slv",
    parameters={
        "collection": "C1276812863-GES_DISC",
        "lat": LAT,
        "lon": LON,
        "variables": ["T2M", "T2MDEW"],
        "humidity": "Magnus (Alduchov & Eskridge 1996) per hour, dew point clamped",
        "magnus": [6.1094, 17.625, 243.04],
        "dewpoint_abort_k": DEWPOINT_ABORT_K,
        "utc_offset_hours": -3,
    },
    doi="10.5067/VJAFPLI1CSIV",
)


def _retry(request: Callable, *, attempts: int = 4, pause: float = 20.0):
    """Retry transport errors, 429 and 5xx; a year of daily requests meets some."""
    import sys
    import time

    for attempt in range(1, attempts + 1):
        try:
            response = request()
            if (response.status_code < 500 and response.status_code != 429) or attempt == attempts:
                return response
            reason = f"HTTP {response.status_code}"
        # TransportError also covers "server disconnected" protocol errors from OPeNDAP.
        except httpx.TransportError as error:
            if attempt == attempts:
                raise
            # The exception type only: its text may carry the request URL.
            reason = type(error).__name__
        wait = pause * attempt
        # Only the delay-seconds form of Retry-After is read; an HTTP date falls back.
        if reason == "HTTP 429" and re.fullmatch(
            r"[0-9]{1,6}", response.headers.get("retry-after", "")
        ):
            # Honour the server's back-off, but never hang a run for more than 5 minutes.
            wait = min(max(wait, float(response.headers["retry-after"])), 300.0)
        print(
            f"MERRA-2: {reason}, tentativa {attempt}/{attempts}, espera {wait:.0f} s",
            file=sys.stderr,
        )
        time.sleep(wait)
    raise AssertionError("unreachable")


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


def month_granules(
    client: httpx.Client, year: int, month: int, product: Product = AEROSOL
) -> dict[date, str]:
    """Granule id per day; the file prefix (400/401) changes for reprocessed months."""
    first = date(year, month, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    response = _retry(
        lambda: client.get(
            CMR,
            params={
                "collection_concept_id": product.collection,
                "temporal": f"{first}T00:00:00Z,{last}T23:59:59Z",
                "page_size": 100,
            },
        )
    )
    response.raise_for_status()
    granules = {}
    for entry in response.json()["feed"]["entry"]:
        title = entry["title"]  # M2T1NXAER.5.12.4:MERRA2_400.tavg1_2d_aer_Nx.20190815.nc4
        # The title becomes part of the OPeNDAP URL, which carries the token.
        match = product.granule.fullmatch(title)
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


def fetch_day(client: httpx.Client, granule: str, product: Product) -> tuple[bytes, str]:
    constraint = ";".join(
        [f"/{name}[0:23]{LAT}{LON}" for name in product.variables]
        + [f"/lat{LAT}", f"/lon{LON}", "/time"]
    )
    url = OPENDAP.format(collection=product.collection, granule=granule.replace(":", "%3A"))
    response = _retry(lambda: client.get(url + ".dap.nc4", params={"dap4.ce": constraint}))
    if response.status_code != 200 or response.content[:4] != b"\x89HDF":
        raise RuntimeError(f"MERRA-2 subset failed for {granule} ({response.status_code})")
    return response.content, hashlib.sha256(response.content).hexdigest()


def _open(payload: bytes, reduce: Callable):
    import xarray as xr

    # netCDF4 reads only from paths; the file lives just long enough to be loaded.
    with tempfile.NamedTemporaryFile(suffix=".nc4") as handle:
        handle.write(payload)
        handle.flush()
        with xr.open_dataset(handle.name, engine="netcdf4") as ds:
            fields = reduce(ds)
            names = {"lat": "latitude", "lon": "longitude", "time": "valid_time"}
            return {
                name: values.rename({k: v for k, v in names.items() if k in values.dims}).load()
                for name, values in fields.items()
            }


def hourly_pm25(payload: bytes):
    """µg/m³ PM2.5 (time, latitude, longitude) from one daily subset."""

    def reduce(ds):
        for name in SPECIES:
            if ds[name].attrs.get("units") != "kg m-3":
                raise ValueError(f"Unexpected MERRA-2 units for {name}")
        return {"pm25": sum(ds[name] * factor for name, factor in SPECIES.items()) * 1e9}

    return _open(payload, reduce)["pm25"]


def hourly_weather(payload: bytes) -> dict:
    """°C temperature, % relative humidity and the dew point clamp count for one day."""
    from ..transform.meteorology import relative_humidity

    def reduce(ds):
        for name in WEATHER.variables:
            if ds[name].attrs.get("units") != "K":
                raise ValueError(f"Unexpected MERRA-2 units for {name}")
        t = ds["T2M"] - 273.15
        d = ds["T2MDEW"] - 273.15
        excess = float((d - t).max())
        if excess > DEWPOINT_ABORT_K:
            raise ValueError("MERRA-2 dew point exceeds temperature by more than 5 K")
        # Per hour, so only hours inside the month's local days are counted later.
        clamped = (d > t).sum(dim=("lat", "lon"))
        humidity = relative_humidity(t, d.where(d <= t, t))
        return {"temperature": t, "humidity": humidity, "clamped": clamped}

    return _open(payload, reduce)


def extract_month(
    year: int,
    month: int,
    root: Path,
    *,
    client: httpx.Client,
    catalog: httpx.Client | None = None,
    fetch: Callable = fetch_day,
    workers: int = 4,
    product: Product = AEROSOL,
) -> dict:
    """Local-day mean grid(s) for one month, saved as NetCDF with a manifest."""
    import xarray as xr

    from ..transform.cams_exposure import local_daily_means

    catalog = catalog if catalog is not None else client
    staging = root / "staging" / product.staging
    output = staging / f"{year}{month:02d}.nc"
    manifest_path = output.with_suffix(".source.json")
    if output.exists() and manifest_path.exists():
        metadata = json.loads(manifest_path.read_text())
        # Only PM2.5 months predate recorded parameters; they used these same values.
        default = product.parameters if product.kind == "aerosol" else None
        if metadata.get("parameters", default) == product.parameters and metadata.get(
            "netcdf_sha256", _sha256(output)
        ) == _sha256(output):
            return metadata
    granules = month_granules(catalog, year, month, product)
    following = (date(year, month, 1) + timedelta(days=32)).replace(day=1)
    days = sorted(granules)
    expected = (following - date(year, month, 1)).days
    if len(days) != expected:
        raise ValueError(f"MERRA-2 {year}-{month:02d}: {len(days)} of {expected} days")
    # The last local day ends at 03 UTC of the next month's first day.
    extra = month_granules(catalog, following.year, following.month, product).get(following)
    if extra is None:
        # Without it the month's last local day is incomplete; never cache a short month.
        raise ValueError(f"MERRA-2 {following} not published yet; end the range a month earlier")
    wanted = [granules[d] for d in days] + [extra]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda g: fetch(client, g, product), wanted))
    if product.kind == "aerosol":
        hourly = {"pm25": [hourly_pm25(payload) for payload, _ in results]}
    else:
        hourly = {"temperature": [], "humidity": [], "clamped": []}
        for payload, _ in results:
            fields = hourly_weather(payload)
            for name in hourly:
                hourly[name].append(fields[name])
    clamped = None
    if "clamped" in hourly:
        steps = xr.concat(hourly.pop("clamped"), dim="valid_time")
        local = (steps["valid_time"] - np.timedelta64(3, "h")).dt
        clamped = int(steps.where((local.month == month), drop=True).sum())
    daily = {}
    for name, parts in hourly.items():
        means = local_daily_means(
            xr.concat(parts, dim="valid_time"), name=f"MERRA-2 {name}", steps_per_day=24
        )
        daily[name] = means.sel(date=(means["date"].dt.month == month)).astype("float32")
        if daily[name].sizes["date"] != expected:
            raise ValueError(
                f"MERRA-2 {year}-{month:02d}: {daily[name].sizes['date']} complete local days"
            )
    staging.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(output.name + ".partial")
    try:
        if product.kind == "aerosol":
            # One named array, as written before the weather product existed.
            daily["pm25"].rename("pm25").to_netcdf(staged)
        else:
            xr.Dataset(daily).to_netcdf(staged)
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    metadata = {
        "parameters": product.parameters,
        "netcdf_sha256": _sha256(output),
        "year": year,
        "month": month,
        "days": expected,
        "granules": [
            {"name": g, "sha256": sha} for g, (_, sha) in zip(wanted, results, strict=True)
        ],
        "retrieved_at": datetime.now(UTC).isoformat(),
        # Not part of the cache check: a commit that does not touch the reduction must
        # not refetch years of data. Change ``parameters`` when the reduction changes.
        "software": software_provenance(),
    }
    if clamped is not None:
        metadata["dewpoint_clamped_steps"] = clamped
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


def _download(start, end, root, product, progress) -> list[dict]:
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
            manifests.append(
                extract_month(year, month, root, client=client, catalog=catalog, product=product)
            )
            if count % 6 == 0:
                progress(f"MERRA-2 {product.short_name}: {count} meses")
    return manifests


def _load(root: Path, product: Product, start: date, end: date, names: tuple[str, ...]):
    import xarray as xr

    grids = {name: [] for name in names}
    for year, month in months(start, end):
        with xr.open_dataset(root / "staging" / product.staging / f"{year}{month:02d}.nc") as ds:
            for name in names:
                grids[name].append(ds[name].load())
    return {name: xr.concat(parts, dim="date").astype("float64") for name, parts in grids.items()}


def _regional(daily, population_raster, regions_geojson):
    from ..transform.cams_exposure import population_cell_weights

    snapping = {}
    weights = population_cell_weights(
        population_raster,
        regions_geojson,
        latitudes=daily["latitude"].values,
        longitudes=daily["longitude"].values,
        max_snap_steps=1.0,
        report=snapping,
    )
    return weights, snapping


def _manifest(series, manifests, product, snapping, population_raster, regions_geojson, **extra):
    from ..transform.cams_exposure import describe_input

    return {
        "created_at": datetime.now(UTC).isoformat(),
        "rows": series.height,
        "dates": [str(series["date"].min()), str(series["date"].max())],
        **extra,
        "local_day": "UTC-3, mean of 24 hourly averages",
        "snapped_beyond_grid": snapping,
        "parameters": product.parameters,
        "months": [f"{m['year']}-{m['month']:02d}" for m in manifests],
        "software": software_provenance(),
        "granules_sha256": hashlib.sha256(
            json.dumps(
                [[g["name"], g["sha256"]] for m in manifests for g in m["granules"]]
            ).encode()
        ).hexdigest(),
        "attribution": "Global Modeling and Assimilation Office (GMAO), MERRA-2 "
        f"{product.stream}, DOI {product.doi}",
        "inputs": {
            "population": [describe_input(population_raster)],
            "regions": [describe_input(regions_geojson)],
        },
    }


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
    from ..transform.cams_exposure import regional_daily_means, write_with_manifest

    manifests = _download(start, end, root, AEROSOL, progress)
    daily = _load(root, AEROSOL, start, end, ("pm25",))["pm25"]
    weights, snapping = _regional(daily, population_raster, regions_geojson)
    series = regional_daily_means(daily, weights, column="pm25_merra2", name="MERRA-2")
    write_with_manifest(
        output,
        series.write_parquet,
        _manifest(
            series,
            manifests,
            AEROSOL,
            snapping,
            population_raster,
            regions_geojson,
            pm25="DUST25 + SS25 + BC + 1.4 OC + 1.375 SO4 (Buchard et al. 2016), µg/m³",
        ),
    )
    return series


def build_merra2_weather(
    start: date,
    end: date,
    population_raster: Path,
    regions_geojson: Path,
    output: Path,
    root: Path = Path("data"),
    *,
    progress: Callable[[str], None] = print,
):
    """Population-weighted daily temperature and humidity per region, ERA5 layout."""
    from ..transform.cams_exposure import regional_daily_means, write_with_manifest

    manifests = _download(start, end, root, WEATHER, progress)
    grids = _load(root, WEATHER, start, end, ("temperature", "humidity"))
    temperature, humidity = grids["temperature"], grids["humidity"]
    weights, snapping = _regional(temperature, population_raster, regions_geojson)
    series = regional_daily_means(temperature, weights, column="temperature", name="MERRA-2").join(
        regional_daily_means(humidity, weights, column="humidity", name="MERRA-2").drop(
            "population"
        ),
        on=["health_region_code", "date"],
        how="inner",
        validate="1:1",
    )
    write_with_manifest(
        output,
        series.write_parquet,
        _manifest(
            series,
            manifests,
            WEATHER,
            snapping,
            population_raster,
            regions_geojson,
            temperature="T2M, °C",
            humidity="Magnus per hour from T2M and T2MDEW, then daily mean, %",
            dewpoint_clamped_steps=sum(m.get("dewpoint_clamped_steps", 0) for m in manifests),
        ),
    )
    return series
