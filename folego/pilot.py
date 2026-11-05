"""Reproducible first SIH pilot: nationwide files, one regional residence cohort.

Processing controls are comparable with TabNet. Admission-date observations are
separate and explicitly immature at the processing-window boundary.
"""

import hashlib
import json
import re
import shutil
import struct
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import polars as pl

from .ingest.dbf import projected_dbf_rows
from .ingest.sih import STATES, download_sih
from .ingest.tabnet import compare_monthly_counts, retrieve_reference
from .transform.outcomes import aggregate_outcomes

FIELDS = [
    "ANO_CMPT",
    "MES_CMPT",
    "N_AIH",
    "IDENT",
    "MUNIC_RES",
    "MUNIC_MOV",
    "DT_INTER",
    "DT_SAIDA",
    "DIAG_PRINC",
    "COD_IDADE",
    "IDADE",
    "VAL_TOT",
]
CLINICAL_FIELDS = [
    "N_AIH",
    "MUNIC_RES",
    "MUNIC_MOV",
    "DT_INTER",
    "DT_SAIDA",
    "DIAG_PRINC",
    "COD_IDADE",
    "IDADE",
    "VAL_TOT",
]
SCHEMA = {name: pl.String for name in FIELDS if name not in ("IDADE", "VAL_TOT")}
SCHEMA.update(IDADE=pl.Int64, VAL_TOT=pl.Float64)


def extract_partition(
    source: Path, output: Path, codes: list[str], state: str, year: int, month: int
):
    from pyreaddbc import dbc2dbf

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    expected = {
        "reader_version": 1,
        "source_sha256": digest,
        "municipality_codes": sorted(codes),
        "state": state,
        "year": year,
        "month": month,
    }
    manifest = output.with_suffix(".source.json")
    if output.exists() and manifest.exists():
        metadata = json.loads(manifest.read_text())
        if all(metadata.get(k) == v for k, v in expected.items()):
            if metadata.get("parquet_sha256") != hashlib.sha256(output.read_bytes()).hexdigest():
                raise ValueError("Corrupt pilot partition cache")
            return metadata
    output.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as stream:
        header = stream.read(32)
    if len(header) != 32:
        raise ValueError("Truncated DBC header")
    count, header_size, record_size = struct.unpack_from("<IHH", header, 4)
    if shutil.disk_usage(output.parent).free < header_size + count * record_size + 512 * 1024**2:
        raise ValueError("Insufficient disk reserve for DBC decompression")
    stats = {}
    with TemporaryDirectory(prefix="folego-pilot-") as directory:
        dbf = Path(directory) / "source.dbf"
        dbc2dbf(str(source), str(dbf))
        rows = list(projected_dbf_rows(dbf, FIELDS, codes, stats=stats))
        frame = pl.from_dicts(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)
    if frame.height:
        if (frame["ANO_CMPT"].cast(pl.Int64) != year).any() or (
            frame["MES_CMPT"].cast(pl.Int64) != month
        ).any():
            raise ValueError("SIH competence does not match source filename")
    frame = frame.with_columns(
        pl.lit(state).alias("source_state"), pl.lit(month).alias("source_month")
    )
    temporary = output.with_suffix(".tmp.parquet")
    frame.write_parquet(temporary)
    temporary.replace(output)
    metadata = {
        **expected,
        **stats,
        "parquet_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "source_url": f"ftp://ftp.datasus.gov.br/dissemin/publicos/SIHSUS/200801_/Dados/{source.name}",
        "source_bytes": source.stat().st_size,
    }
    manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def monthly_processing_counts(records: pl.DataFrame, year: int) -> pl.DataFrame:
    normal = records.filter(
        (pl.col("IDENT") == "1")
        & (pl.col("ANO_CMPT") == str(year))
        & pl.col("DIAG_PRINC").str.contains(r"^J\d{2}")
    )
    result = normal.group_by(pl.col("MES_CMPT").cast(pl.Int64).alias("month")).agg(
        pl.len().cast(pl.Int64).alias("admissions")
    )
    return (
        pl.DataFrame({"month": range(1, 13)})
        .join(result, on="month", how="left")
        .with_columns(pl.col("admissions").fill_null(0))
        .sort("month")
    )


def reconcile_episodes(records: pl.DataFrame):
    continuations = records.filter(pl.col("IDENT") == "5").height
    normal = records.filter(pl.col("IDENT") == "1").sort("ANO_CMPT", "source_month", "source_state")
    invalid_id = normal.filter(
        ~pl.col("N_AIH").str.contains(r"^\d{13}$") | pl.col("N_AIH").is_null()
    )
    normal = normal.filter(pl.col("N_AIH").str.contains(r"^\d{13}$"))
    unique = normal.unique(subset=CLINICAL_FIELDS, maintain_order=True)
    conflicting = unique.group_by("N_AIH").len().filter(pl.col("len") > 1)["N_AIH"]
    clean = unique.filter(~pl.col("N_AIH").is_in(conflicting.implode()))
    return clean, {
        "excluded_continuations": continuations,
        "excluded_other_aih_types": records.filter(~pl.col("IDENT").is_in(["1", "5"])).height,
        "invalid_identifiers": invalid_id.height,
        "identical_duplicate_rows_removed": normal.height - unique.height,
        "conflicting_identifiers": len(conflicting),
        "conflicting_rows_quarantined": normal.filter(
            pl.col("N_AIH").is_in(conflicting.implode())
        ).height,
    }


def run_pilot(config_path: Path, root=Path("data"), *, with_tabnet=True, progress=print):
    config = json.loads(config_path.read_text())
    name = config["name"]
    year = int(config["processing_year"])
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported pilot config schema")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not 2012 <= year <= 2099:
        raise ValueError("Invalid pilot name/year")
    states = STATES if config["file_states"] == "all" else config["file_states"]
    if len(states) != len(set(states)) or not set(states).issubset(STATES):
        raise ValueError("Invalid file states")
    if with_tabnet and set(states) != set(STATES):
        raise ValueError("National file coverage required for residence TabNet validation")
    territory = Path(config["territory_snapshot"])
    lookup = pl.read_parquet(territory)
    municipalities = lookup.filter(pl.col("health_region_code") == config["health_region_code"])
    if municipalities.is_empty():
        raise ValueError("Pilot region missing from territorial snapshot")
    codes = municipalities["municipality_code"].sort().to_list()
    if (
        municipalities["state"].n_unique() != 1
        or municipalities["state"][0] != config["reference_state"]
    ):
        raise ValueError("TabNet reference state differs from pilot residence state")
    staging = root / "staging" / "pilots" / name
    manifests = []
    paths = []
    for state in states:
        for month in range(1, 13):
            source = download_sih(state, year, month, root)
            output = staging / "partitions" / state / f"{year}{month:02d}.parquet"
            manifests.append(extract_partition(source, output, codes, state, year, month))
            paths.append(output)
            if len(paths) % 18 == 0:
                progress(f"{len(paths)}/{len(states) * 12} partitions projected")
    records = pl.concat([pl.read_parquet(p) for p in paths])
    counts = monthly_processing_counts(records, year)
    counts.write_parquet(staging / "processing-monthly.parquet")
    episodes, reconciliation = reconcile_episodes(records)
    # Keep all quarantined rows locally, never their identifiers in public reports.
    records.filter(~pl.col("N_AIH").is_in(episodes["N_AIH"].implode())).write_parquet(
        staging / "excluded-rows.parquet"
    )
    clinical = episodes.with_columns(
        pl.col("DT_INTER").str.to_date("%Y%m%d", strict=False).alias("_date"),
        pl.col("COD_IDADE").cast(pl.Int64, strict=False).alias("_age_unit"),
    )
    invalid_date = clinical["_date"].null_count()
    invalid_age_unit = clinical["_age_unit"].null_count()
    clinical = clinical.filter(pl.col("_date").is_not_null() & pl.col("_age_unit").is_not_null())
    outcomes = aggregate_outcomes(clinical, lookup)
    outcomes.write_parquet(staging / "daily-observed.parquet")
    # No zero-fill: a processing-year extract does not certify a mature admission cohort.
    normal_resp = records.filter(
        (pl.col("IDENT") == "1") & pl.col("DIAG_PRINC").str.contains(r"^J\d{2}")
    )
    source_report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "name": name,
        "mode": "observed_research",
        "model_ready": False,
        "health_region_code": config["health_region_code"],
        "health_region": municipalities["health_region"][0],
        "processing_year": year,
        "municipalities": municipalities.select(
            "municipality_code", "municipality", "state"
        ).to_dicts(),
        "territory_sha256": hashlib.sha256(territory.read_bytes()).hexdigest(),
        "territory_historical_consistency": "pending",
        "files_expected": len(states) * 12,
        "files_processed": len(paths),
        "file_states": states,
        "national_file_coverage": set(states) == set(STATES),
        "source_bytes": sum(m["source_bytes"] for m in manifests),
        "scanned_records": sum(m["scanned_records"] for m in manifests),
        "selected_residence_records": records.height,
        "normal_respiratory_processing_records": normal_resp.height,
        "normal_respiratory_outside_residence_state": normal_resp.filter(
            ~pl.col("MUNIC_MOV").str.starts_with(municipalities["ibge_code"][0][:2])
            & pl.col("MUNIC_MOV").str.contains(r"^\d{6}$")
            & (pl.col("MUNIC_MOV") != "000000")
        ).height,
        "reconciliation": reconciliation,
        "invalid_admission_dates": invalid_date,
        "invalid_age_units": invalid_age_unit,
        "daily_observed_rows": outcomes.height,
        "clinical_cohort_complete": False,
        "zero_filled": False,
        "payment_basis": "VAL_TOT nominal; not inflation-adjusted; no smoke attribution",
        "tabnet": {"status": "not_requested"},
        "source_manifest": manifests,
    }
    if with_tabnet:
        if set(states) != set(STATES):
            raise ValueError("National file coverage required for residence TabNet validation")
        try:
            reference, reference_source = retrieve_reference(
                year, config["reference_state"], codes, root
            )
            source_report["tabnet"] = {
                "status": "compared",
                "source": reference_source,
                **compare_monthly_counts(counts, reference),
            }
            reference.write_parquet(staging / "tabnet-monthly.parquet")
        except httpx.HTTPError as error:
            source_report["tabnet"] = {
                "status": "unavailable",
                "error_type": type(error).__name__,
                "accepted": False,
            }
        except ValueError as error:
            source_report["tabnet"] = {
                "status": "invalid_response",
                "error": str(error),
                "accepted": False,
            }
    marts = root / "marts" / "pilots" / name
    marts.mkdir(parents=True, exist_ok=True)
    (marts / "report.json").write_text(
        json.dumps(source_report, ensure_ascii=False, allow_nan=False, indent=2)
    )
    progress(f"Pilot report: {marts / 'report.json'}")
    return source_report
