"""Daily fire detections per health region from the FIRMS VIIRS archive.

The FIRMS area API returns at most five days per request, so the archive is walked in
five-day chunks over Brazil. Each chunk is reduced in memory to counts and fire
radiative power per health region and local day (UTC−3) and only that aggregate is
stored, with the SHA-256 of the response. The MAP_KEY is read from a 600 file or the
environment and never written to disk, logs or error messages.
"""

import hashlib
import io
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from .sih_national import software_provenance

API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
SOURCE = "VIIRS_SNPP_SP"
# West, south, east, north; east −32 keeps Fernando de Noronha.
BRAZIL = "-75,-35,-32,6"
CHUNK_DAYS = 5
KEY_FILE = Path("~/.config/folego/firms-key")
EXPECTED_HEADER = ("latitude", "longitude", "acq_date", "acq_time", "confidence", "frp", "type")
# Type 0 is a presumed vegetation fire; volcanoes, static industrial sources and
# offshore detections are not smoke from burning.
VEGETATION = 0
# Everything that shapes a stored chunk; a cached chunk is reused only if they match.
CHUNK_PARAMETERS = {
    "source": SOURCE,
    "area": BRAZIL,
    "days": CHUNK_DAYS,
    "filter": "type 0, confidence n/h",
    "utc_offset_hours": -3,
}
AVAILABILITY = "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{key}/{source}"
# httpx logs request URLs at INFO, and the FIRMS URL carries the key.
logging.getLogger("httpx").setLevel(logging.WARNING)


def map_key() -> str:
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        path = KEY_FILE.expanduser()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise RuntimeError(
                f"FIRMS MAP_KEY not found; save it in {KEY_FILE} (mode 600)"
            ) from None
        except OSError:
            raise RuntimeError(f"{KEY_FILE} must be a regular file, not a link") from None
        with os.fdopen(descriptor) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError(f"{KEY_FILE} must be a regular file owned by you")
            if info.st_mode & 0o077:
                raise RuntimeError(f"{KEY_FILE} must be readable by its owner only (chmod 600)")
            key = handle.read().strip()
    if not re.fullmatch(r"[A-Za-z0-9]{32}", key):
        raise RuntimeError("FIRMS MAP_KEY must be 32 ASCII letters or digits")
    return key


def archive_end(client: httpx.Client, key: str, *, source=SOURCE) -> date:
    """Last UTC day the standard-processing archive holds; later days are not zeros."""
    try:
        response = client.get(AVAILABILITY.format(key=key, source=source))
        response.raise_for_status()
    except httpx.HTTPError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        raise RuntimeError(
            f"FIRMS availability failed ({type(error).__name__}, {status})"
        ) from None
    rows = pl.read_csv(io.BytesIO(response.content), infer_schema_length=0)
    match = rows.filter(pl.col("data_id") == source)
    if match.height != 1:
        raise RuntimeError(f"FIRMS availability does not list {source}")
    return date.fromisoformat(match["max_date"].item())


def fetch_chunk(
    client: httpx.Client, key: str, first: date, *, source=SOURCE, attempts=6, pause=60.0
) -> bytes:
    url = f"{API}/{key}/{source}/{BRAZIL}/{CHUNK_DAYS}/{first.isoformat()}"
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            # httpx puts the URL, and so the key, in its messages.
            status = getattr(getattr(error, "response", None), "status_code", None)
            failure = f"FIRMS request failed ({type(error).__name__}, {status})"
        else:
            # Anything but the CSV header (rate limit, invalid key, HTML) is an error;
            # the service's own text never contains the key.
            if response.content.startswith(b"latitude,"):
                return response.content
            failure = "FIRMS answered: " + response.text.strip()[:80].replace(key, "[key]")
        if attempt < attempts:
            # 400 is also how FIRMS signals the 10-minute transaction quota.
            time.sleep(pause * attempt)
    raise RuntimeError(failure) from None


def assign_regions(points: pl.DataFrame, regions_geojson: Path) -> pl.DataFrame:
    """Health region of each detection; points outside every polygon get null."""
    import numpy as np
    import shapely
    from shapely.geometry import shape

    features = json.loads(Path(regions_geojson).read_text())["features"]
    polygons = [shape(f["geometry"]) for f in features]
    codes = np.array([str(f["properties"]["code"]) for f in features], dtype=object)
    tree = shapely.STRtree(polygons)
    geometry = shapely.points(points["longitude"].to_numpy(), points["latitude"].to_numpy())
    point_index, polygon_index = tree.query(geometry, predicate="within")
    region = np.full(points.height, None, dtype=object)
    # Overlapping polygons in the simplified web geometry could match a point twice;
    # the first match keeps it once. Points in gaps are counted as outside.
    first = np.unique(point_index, return_index=True)[1]
    region[point_index[first]] = codes[polygon_index[first]]
    return points.with_columns(pl.Series("health_region_code", region.tolist(), dtype=pl.String))


def reduce_chunk(payload: bytes, regions_geojson: Path, *, utc_offset_hours=-3) -> tuple:
    frame = pl.read_csv(io.BytesIO(payload), infer_schema_length=0)
    if missing := set(EXPECTED_HEADER) - set(frame.columns):
        raise ValueError(f"FIRMS CSV without columns {sorted(missing)}")
    detections = frame.select(
        pl.col("latitude").cast(pl.Float64),
        pl.col("longitude").cast(pl.Float64),
        (
            pl.col("acq_date").str.to_datetime("%Y-%m-%d")
            + pl.duration(
                hours=pl.col("acq_time").cast(pl.Int32) // 100 + utc_offset_hours,
                minutes=pl.col("acq_time").cast(pl.Int32) % 100,
            )
        )
        .dt.date()
        .alias("date"),
        pl.col("confidence").str.strip_chars().str.to_lowercase(),
        pl.col("frp").cast(pl.Float64),
        pl.col("type").cast(pl.Int32),
    )
    stats = {
        "detections": detections.height,
        "non_vegetation": int((detections["type"] != VEGETATION).sum()),
        "low_confidence": int((detections["confidence"] == "l").sum()),
    }
    # Low-confidence VIIRS detections are mostly false alarms (sun glint, hot soil).
    kept = detections.filter((pl.col("type") == VEGETATION) & (pl.col("confidence") != "l"))
    assigned = (
        assign_regions(kept, regions_geojson)
        if kept.height
        else kept.with_columns(pl.lit(None, pl.String).alias("health_region_code"))
    )
    stats["outside_brazil_regions"] = int(assigned["health_region_code"].null_count())
    counts = (
        assigned.filter(pl.col("health_region_code").is_not_null())
        .group_by("date", "health_region_code")
        .agg(pl.len().cast(pl.Int64).alias("fire_count"), pl.col("frp").sum().alias("frp"))
    )
    return counts, stats


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chunk_starts(start: date, end: date) -> list[date]:
    days = (end - start).days + 1
    return [
        start + timedelta(days=CHUNK_DAYS * i) for i in range((days + CHUNK_DAYS - 1) // CHUNK_DAYS)
    ]


def build_fires(
    start: date,
    end: date,
    regions_geojson: Path,
    output: Path,
    root: Path = Path("data"),
    *,
    progress: Callable[[str], None] = print,
) -> pl.DataFrame:
    """Daily fire count and FRP per region for every local day in [start, end].

    Local day D runs from 03 UTC of D to 03 UTC of D+1, so the UTC days fetched go one
    past ``end``; days the archive has not processed yet are refused, not zero-filled.
    """
    from ..transform.cams_exposure import describe_input, write_with_manifest

    key = map_key()
    staging = root / "staging" / "firms" / SOURCE
    staging.mkdir(parents=True, exist_ok=True)
    regions_sha = describe_input(regions_geojson)["sha256"]
    starts = chunk_starts(start, end + timedelta(days=1))
    manifests = []
    with httpx.Client(timeout=httpx.Timeout(180, connect=20), trust_env=False) as client:
        available = archive_end(client, key)
        if starts[-1] + timedelta(days=CHUNK_DAYS - 1) > available:
            raise RuntimeError(
                f"{SOURCE} is processed up to {available}; use --end before "
                f"{available - timedelta(days=CHUNK_DAYS)}"
            )
        for count, first in enumerate(starts, start=1):
            path = staging / f"{first.isoformat()}.parquet"
            meta_path = path.with_suffix(".source.json")
            if path.exists() and meta_path.exists():
                metadata = json.loads(meta_path.read_text())
                # Chunks written before parameters were recorded used these same values.
                parameters = metadata.get("parameters", CHUNK_PARAMETERS)
                if (
                    metadata.get("regions_sha256") == regions_sha
                    and parameters == CHUNK_PARAMETERS
                    and metadata.get("parquet_sha256", sha256_of(path)) == sha256_of(path)
                ):
                    manifests.append(metadata)
                    continue
            payload = fetch_chunk(client, key, first)
            counts, stats = reduce_chunk(payload, regions_geojson)
            staged = path.with_name(path.name + ".partial")
            try:
                counts.write_parquet(staged)
                os.replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)
            metadata = {
                "parameters": CHUNK_PARAMETERS,
                "first_utc_day": first.isoformat(),
                "parquet_sha256": sha256_of(path),
                "response_sha256": hashlib.sha256(payload).hexdigest(),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "regions_sha256": regions_sha,
                **stats,
            }
            meta_path.write_text(json.dumps(metadata, indent=2))
            manifests.append(metadata)
            if count % 50 == 0:
                progress(f"{count}/{len(starts)} blocos de 5 dias")
    first_day, last_day = start, end
    files = [staging / f"{s.isoformat()}.parquet" for s in starts]
    counts = (
        pl.concat([pl.read_parquet(f) for f in files])
        .filter(pl.col("date").is_between(first_day, last_day))
        .group_by("date", "health_region_code")
        .agg(pl.col("fire_count").sum(), pl.col("frp").sum())
    )
    codes = [
        str(f["properties"]["code"]) for f in json.loads(regions_geojson.read_text())["features"]
    ]
    grid = pl.DataFrame({"date": pl.date_range(first_day, last_day, eager=True)}).join(
        pl.DataFrame({"health_region_code": codes}), how="cross"
    )
    series = (
        grid.join(counts, on=["date", "health_region_code"], how="left")
        .with_columns(pl.col("fire_count").fill_null(0), pl.col("frp").fill_null(0.0))
        .sort("health_region_code", "date")
    )
    totals = {
        k: sum(m[k] for m in manifests)
        for k in ("detections", "non_vegetation", "low_confidence", "outside_brazil_regions")
    }
    write_with_manifest(
        output,
        series.write_parquet,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "rows": series.height,
            "dates": [first_day.isoformat(), last_day.isoformat()],
            "source": f"NASA FIRMS {SOURCE} (VIIRS 375 m, standard processing)",
            "archive_available_until": available.isoformat(),
            "parameters": CHUNK_PARAMETERS,
            "software": software_provenance(),
            "totals_scope": "all fetched UTC days, including up to 5 after the end date",
            "filter": "type 0 (vegetation fire), confidence nominal or high",
            "local_day": "UTC-3",
            "totals": totals,
            "chunks_sha256": hashlib.sha256(
                json.dumps(sorted(m["response_sha256"] for m in manifests)).encode()
            ).hexdigest(),
            # Cite the product DOI from FIRMS' citation page before publishing.
            "attribution": "NASA FIRMS (LANCE/EOSDIS), VIIRS S-NPP 375 m standard processing",
            "inputs": {"regions": [describe_input(regions_geojson)]},
        },
    )
    return series
