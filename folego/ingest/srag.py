"""Hospitalised SRAG (SIVEP-Gripe) per health region and day of symptom onset.

Each yearly open-data file is downloaded, reduced to daily counts by residence region and
final classification, and deleted; the manifest keeps its URL and SHA-256. Only counts
leave this module: no record-level field is written.

Viral SRAG (influenza, COVID-19, other respiratory viruses) is the epidemic indicator
for the panel. Unspecified SRAG is kept apart because smoke itself can cause it, and
adjusting for it would remove part of the exposure effect.
"""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl

from .common import cached_download, sha256_file

DATASET_PAGES = (
    "https://dadosabertos.saude.gov.br/dataset/srag-2009-2012",
    "https://dadosabertos.saude.gov.br/dataset/srag-2013-2018",
    "https://dadosabertos.saude.gov.br/dataset/srag-2019-a-2026",
)
# Files are served only from these two hosts; anything else on the page is ignored.
FILE_PATTERN = re.compile(
    r"https://(?:d26692udehoye\.cloudfront\.net/SRAG/\d{4}-\d{4}/INFLUD(\d{2})\.csv"
    r"|s3\.sa-east-1\.amazonaws\.com/ckan\.saude\.gov\.br/SRAG/\d{4}/"
    r"INFLUD(\d{2})-(\d{2})-(\d{2})-(\d{4})\.parquet)(?=[\"'\s<])"
)
ALLOWED_HOSTS = {
    "dadosabertos.saude.gov.br",
    "d26692udehoye.cloudfront.net",
    "s3.sa-east-1.amazonaws.com",
}
CATEGORIES = {1: "influenza", 2: "other_virus", 3: "other_agent", 4: "unspecified", 5: "covid"}
VIRAL = ("influenza", "covid", "other_virus")
BRASILIA = "530010"
MAX_UNMAPPED_SHARE = 0.02
# Onset is mandatory in SIVEP; a large invalid share means the date format changed.
MAX_INVALID_ONSET_SHARE = 0.01
ONSET_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")


def _official_only(response: httpx.Response) -> None:
    # Checked on every hop: a redirect may not leave HTTPS or the official hosts.
    if response.url.scheme != "https" or response.url.host not in ALLOWED_HOSTS:
        raise ValueError(f"Unexpected SRAG host: {response.url.host}")


def discover_files(pages=DATASET_PAGES) -> dict[int, str]:
    """Newest published file per year: CSV up to 2018, Parquet from 2019."""
    found: dict[int, tuple[str, str]] = {}
    with httpx.Client(
        timeout=httpx.Timeout(60, connect=15),
        follow_redirects=True,
        event_hooks={"response": [_official_only]},
    ) as client:
        for page in pages:
            response = client.get(page)
            response.raise_for_status()
            for match in FILE_PATTERN.finditer(response.text):
                url = match[0]
                if match[1]:
                    year, version = 2000 + int(match[1]), "0"
                else:
                    year = 2000 + int(match[2])
                    version = f"{match[5]}{match[4]}{match[3]}"
                if year not in found or version > found[year][1]:
                    found[year] = (url, version)
    return {year: url for year, (url, _) in sorted(found.items())}


def read_srag(path: Path) -> pl.DataFrame:
    """Onset date, residence and classification of hospitalised SRAG, from either layout."""
    if path.suffix == ".parquet":
        names = pl.read_parquet_schema(path)
        residence = "CO_MUN_RES" if "CO_MUN_RES" in names else "ID_MN_RESI"
        frame = pl.read_parquet(path, columns=["DT_SIN_PRI", residence, "CLASSI_FIN", "HOSPITAL"])
        onset = (
            _parse_onset(pl.col("DT_SIN_PRI"))
            if names["DT_SIN_PRI"] == pl.String
            else pl.col("DT_SIN_PRI").cast(pl.Date)
        )
    else:
        residence = "ID_MN_RESI"
        frame = pl.read_csv(
            path,
            separator=";",
            columns=["DT_SIN_PRI", residence, "CLASSI_FIN", "HOSPITAL"],
            infer_schema=False,
            encoding="utf8-lossy",
            truncate_ragged_lines=True,
        )
        onset = _parse_onset(pl.col("DT_SIN_PRI"))
    code = pl.col(residence).cast(pl.String).str.strip_chars()
    return frame.select(
        onset.alias("date"),
        # SIVEP, like the SIH, codes Distrito Federal residents by administrative region.
        pl.when(code.str.contains(r"^53\d{4,5}$"))
        .then(pl.lit(BRASILIA))
        .when(code.str.contains(r"^\d{6,7}$"))
        .then(code.str.slice(0, 6))
        .alias("municipality_code"),
        _code(pl.col("CLASSI_FIN"))
        .replace_strict(CATEGORIES, default="unclassified", return_dtype=pl.String)
        .alias("category"),
        (_code(pl.col("HOSPITAL")) == 1).fill_null(False).alias("hospitalised"),
    )


def _code(column: pl.Expr) -> pl.Expr:
    """Coded answer as an integer whether stored as "1", "1.0" or 1.0."""
    return (
        column.cast(pl.String)
        .str.strip_chars()
        .cast(pl.Float64, strict=False)
        .cast(pl.Int32, strict=False)
    )


def _parse_onset(column: pl.Expr) -> pl.Expr:
    # Explicit formats: inference from the first value turns the other format into null.
    text = column.str.strip_chars().str.slice(0, 10)
    return pl.coalesce([text.str.to_date(fmt, strict=False) for fmt in ONSET_FORMATS])


def extract_year(
    year: int, url: str, lookup: pl.DataFrame, root: Path = Path("data"), *, keep_source=False
) -> dict:
    staging = root / "staging" / "srag"
    output = staging / f"{year}.parquet"
    manifest_path = output.with_suffix(".source.json")
    raw = root / "raw" / "srag" / url.rsplit("/", 1)[1]
    lookup_sha256 = hashlib.sha256(
        lookup.sort("municipality_code").write_csv().encode()
    ).hexdigest()
    if output.exists() and manifest_path.exists():
        metadata = json.loads(manifest_path.read_text())
        if (
            metadata.get("source_url") == url
            and metadata.get("lookup_sha256") == lookup_sha256
            and metadata.get("parquet_sha256") == sha256_file(output)
        ):
            if not keep_source:
                _delete(raw)
            return metadata
    try:
        return _extract(year, url, lookup, lookup_sha256, raw, output, manifest_path, keep_source)
    finally:
        # Microdata never outlive the run, also when extraction fails.
        if not keep_source:
            _delete(raw)


def _delete(source: Path) -> None:
    source.unlink(missing_ok=True)
    source.with_suffix(source.suffix + ".source.json").unlink(missing_ok=True)


def _extract(year, url, lookup, lookup_sha256, raw, output, manifest_path, keep_source) -> dict:
    staging = output.parent
    source = cached_download(url, raw)
    provenance = json.loads(source.with_suffix(source.suffix + ".source.json").read_text())
    records = read_srag(source)
    hospitalised = records.filter(pl.col("hospitalised"))
    mapped = hospitalised.join(lookup, on="municipality_code", how="left")
    stats = {
        "records": records.height,
        "hospitalised": hospitalised.height,
        "invalid_onset": int(mapped["date"].null_count()),
        "unmapped_residence": int(mapped["health_region_code"].null_count()),
    }
    if not hospitalised.height:
        raise ValueError(f"SRAG {year}: no hospitalised case; check the HOSPITAL coding")
    if stats["unmapped_residence"] > MAX_UNMAPPED_SHARE * hospitalised.height:
        raise ValueError(f"SRAG {year}: {stats['unmapped_residence']} unmapped residences")
    if stats["invalid_onset"] > MAX_INVALID_ONSET_SHARE * hospitalised.height:
        raise ValueError(f"SRAG {year}: {stats['invalid_onset']} invalid onset dates")
    counts = (
        mapped.filter(pl.col("date").is_not_null() & pl.col("health_region_code").is_not_null())
        .group_by("date", "health_region_code", "category")
        .agg(pl.len().cast(pl.Int64).alias("cases"))
        .sort("date", "health_region_code", "category")
    )
    staging.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(output.name + ".partial")
    staged_manifest = manifest_path.with_name(manifest_path.name + ".partial")
    try:
        counts.write_parquet(staged)
        metadata = {
            "year": year,
            **stats,
            "rows": counts.height,
            "parquet_sha256": sha256_file(staged),
            "source_url": url,
            "source_retrieved_at": provenance["retrieved_at"],
            "source_sha256": provenance["sha256"],
            "source_deleted": not keep_source,
            "lookup_sha256": lookup_sha256,
            "extracted_at": datetime.now(UTC).isoformat(),
        }
        staged_manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        os.replace(staged, output)
        os.replace(staged_manifest, manifest_path)
    finally:
        staged.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)
    return metadata


def build_srag(
    start_year: int,
    end_year: int,
    lookup_path: Path,
    output: Path,
    root: Path = Path("data"),
    *,
    keep_source=False,
    files: dict[int, str] | None = None,
) -> pl.DataFrame:
    """Daily hospitalised SRAG per region and category, zero-filled over full years."""
    from ..transform.cams_exposure import describe_input, write_with_manifest

    files = files if files is not None else discover_files()
    years = range(start_year, end_year + 1)
    if missing := [y for y in years if y not in files]:
        raise ValueError(f"No SRAG file published for {missing}")
    lookup = pl.read_parquet(lookup_path).select("municipality_code", "health_region_code")
    # Files are by notification year: onsets at the edges also sit in the neighbouring
    # files, which are read when published and then cut back to the requested years.
    read = [y for y in range(start_year - 1, end_year + 2) if y in files]
    manifests = [extract_year(y, files[y], lookup, root, keep_source=keep_source) for y in read]
    first, last = date(start_year, 1, 1), date(end_year, 12, 31)
    counts = (
        pl.concat([pl.read_parquet(root / "staging" / "srag" / f"{y}.parquet") for y in read])
        .filter(pl.col("date").is_between(first, last))
        .group_by("date", "health_region_code", "category")
        .agg(pl.col("cases").sum())
        .pivot(on="category", index=["date", "health_region_code"], values="cases")
    )
    grid = pl.DataFrame({"date": pl.date_range(first, last, eager=True)}).join(
        lookup.select(pl.col("health_region_code").unique()), how="cross"
    )
    categories = [*CATEGORIES.values(), "unclassified"]
    series = (
        grid.join(counts, on=["date", "health_region_code"], how="left")
        .with_columns(
            pl.col(c).fill_null(0) if c in counts.columns else pl.lit(0, pl.Int64).alias(c)
            for c in categories
        )
        .with_columns(pl.sum_horizontal(VIRAL).alias("viral"))
        .select("date", "health_region_code", *categories, "viral")
        .sort("health_region_code", "date")
    )
    write_with_manifest(
        output,
        series.write_parquet,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "rows": series.height,
            "dates": [first.isoformat(), last.isoformat()],
            "basis": "hospitalised SRAG (HOSPITAL=1) by symptom onset and residence region",
            "categories": "CLASSI_FIN 1 influenza, 2 other virus, 3 other agent, "
            "4 unspecified, 5 COVID-19; viral = influenza + covid + other_virus",
            "attribution": "Ministério da Saúde, SIVEP-Gripe, dados abertos de SRAG",
            "years": manifests,
            "inputs": {"lookup": [describe_input(lookup_path)]},
        },
    )
    return series
