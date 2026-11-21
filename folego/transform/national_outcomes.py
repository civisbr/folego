"""Daily admissions per health region from the nationwide SIH extraction.

Records are de-duplicated as in the pilot: normal AIHs (IDENT 1) only, identical repeats
across competences kept once, and identifiers with conflicting clinical content
quarantined. Work is split into buckets of N_AIH so a repeated AIH always lands in the
same bucket and memory stays bounded on a 16 GB machine.

The admission-date window is certified only where every state's file is present for
every competence and the trailing ``maturity_months`` are dropped, because an admission
keeps arriving in later competences for months.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from ..ingest.sih import CLINICAL_FIELDS, STATES
from ..ingest.sih_national import national_root, software_provenance
from .cams_exposure import describe_input, write_with_manifest

OUTCOMES = ("respiratory", "asthma", "pneumonia", "circulatory", "negative_control")
AGE_GROUPS = ("0-4", "5-14", "15-59", "60+")
BRASILIA = "530010"
MAX_UNMAPPED_SHARE = 0.001
# Cells of one or two admissions with exact date, region, age and payment approach
# record-level data; marts stay local and are published only through suppression.
PRIVATE = "private: contains cells below 5; publish only via suppress_small_cells"
KEYS = ["date", "health_region_code", "age_group", "outcome"]


def certified_window(root: Path, *, maturity_months: int = 6) -> dict:
    """Contiguous competences with all 27 states, and the admission dates they cover."""
    seen = {}
    for path in national_root(root).glob("*/*/*.source.json"):
        metadata = json.loads(path.read_text())
        seen.setdefault((metadata["year"], metadata["month"]), set()).add(metadata["state"])
    complete = sorted(c for c, states in seen.items() if states == set(STATES))
    if not complete:
        raise ValueError("No competence has files from all 27 states")
    run = [complete[0]]
    for competence in complete[1:]:
        if competence != _add_months(run[-1], 1):
            break
        run.append(competence)
    first, last = run[0], run[-1]
    # A failed partition mid-series would silently cut every later year out of the window.
    if later := [c for c in complete if c > last]:
        raise ValueError(
            f"Competence {_add_months(last, 1)} is incomplete but {later[0]} is complete; "
            "re-run sih-national before building outcomes"
        )
    mature = _add_months(last, -maturity_months)
    if mature < first:
        raise ValueError("Certified competences are shorter than the maturity lag")
    following = _add_months(mature, 1)
    return {
        "competences": [f"{y}-{m:02d}" for y, m in run],
        "maturity_months": maturity_months,
        "start": date(*first, 1),
        "end": date(*following, 1) - timedelta(days=1),
        "incomplete_competences_after_run": [
            f"{y}-{m:02d}" for y, m in sorted(seen) if (y, m) > last
        ],
    }


def _add_months(competence: tuple[int, int], months: int) -> tuple[int, int]:
    index = competence[0] * 12 + competence[1] - 1 + months
    return index // 12, index % 12 + 1


def _tabnet_status(report_path: Path, window: dict) -> dict:
    """Whether an accepted TabNet check covers years of this window; recorded, not enforced."""
    if not report_path.exists():
        return {"validated": False, "reason": "no tabnet-national report"}
    report = json.loads(report_path.read_text())
    years = {int(c[:4]) for c in window["competences"]}
    covered = sorted({c["year"] for c in report["checks"] if c.get("accepted")} & years)
    return {
        "validated": bool(report["accepted"] and covered),
        "accepted": report["accepted"],
        "years_checked_in_window": covered,
        **describe_input(report_path),
    }


def _merge(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    return (
        pl.concat([left, right])
        .group_by(KEYS)
        .agg(
            pl.col("admissions").sum(),
            pl.when(pl.col("payment").is_not_null().all()).then(pl.col("payment").sum()),
        )
    )


def classified(frame: pl.LazyFrame) -> pl.LazyFrame:
    """Vectorised equivalents of codes.diagnosis_group / age_group / municipality_code."""
    diagnosis = (
        pl.col("DIAG_PRINC")
        .str.strip_chars()
        .str.to_uppercase()
        .str.replace_all(".", "", literal=True)
    )
    chapter = diagnosis.str.slice(0, 1)
    number = diagnosis.str.slice(1, 2).cast(pl.Int32, strict=False)
    valid = diagnosis.str.contains(r"^[A-Z]\d{2}\d*$")
    outcome = (
        pl.when(~valid)
        .then(None)
        .when((chapter == "J") & number.is_between(45, 46))
        .then(pl.lit("asthma"))
        .when((chapter == "J") & number.is_between(12, 18))
        .then(pl.lit("pneumonia"))
        .when(chapter == "J")
        .then(pl.lit("respiratory"))
        .when(chapter == "I")
        .then(pl.lit("circulatory"))
        .when((chapter == "S") | ((chapter == "T") & (number <= 98)))
        .then(pl.lit("negative_control"))
    )
    unit = pl.col("COD_IDADE").str.strip_chars().cast(pl.Int32, strict=False)
    value = pl.col("IDADE")
    years = (
        pl.when(value.is_null() | (value < 0))
        .then(None)
        .when(unit.is_in([1, 2, 3]))
        .then(0)
        .when((unit == 4) & (value <= 99))
        .then(value)
        .when((unit == 5) & (value <= 99))
        .then(value + 100)
    )
    age_group = (
        pl.when(years < 5)
        .then(pl.lit("0-4"))
        .when(years < 15)
        .then(pl.lit("5-14"))
        .when(years < 60)
        .then(pl.lit("15-59"))
        .when(years.is_not_null())
        .then(pl.lit("60+"))
    )
    residence = pl.col("MUNIC_RES").str.strip_chars()
    return frame.with_columns(
        outcome.alias("outcome"),
        age_group.alias("age_group"),
        # SIH codes Distrito Federal residents by administrative region (5300xx-5301xx);
        # the IBGE has a single municipality there, Brasília.
        pl.when(residence.str.contains(r"^53\d{4,5}$"))
        .then(pl.lit(BRASILIA))
        .when(residence.str.contains(r"^\d{6,7}$"))
        .then(residence.str.slice(0, 6))
        .alias("municipality_code"),
        pl.col("DT_INTER").str.strip_chars().str.to_date("%Y%m%d", strict=False).alias("date"),
        pl.date(
            pl.col("ANO_CMPT").cast(pl.Int32, strict=False),
            pl.col("MES_CMPT").cast(pl.Int32, strict=False),
            1,
        ).alias("competence"),
    )


def build_national_outcomes(
    root: Path,
    lookup_path: Path,
    ipca_path: Path,
    output: Path,
    *,
    maturity_months: int = 6,
    buckets: int = 64,
) -> pl.DataFrame:
    """Sparse daily counts (zero cells omitted) with IPCA-deflated payment sums."""
    window = certified_window(root, maturity_months=maturity_months)
    lookup = pl.read_parquet(lookup_path).select("municipality_code", "health_region_code")
    if lookup["municipality_code"].n_unique() != lookup.height:
        raise ValueError("Municipality lookup must be unique")
    if lookup["health_region_code"].null_count():
        raise ValueError("Municipality lookup has unmapped municipalities")
    ipca = pl.read_parquet(ipca_path)
    reference_month = ipca["month"].max()
    reference_index = ipca.filter(pl.col("month") == reference_month)["ipca_index"].item()
    files = [
        path
        for path in sorted(national_root(root).glob("*/*/*.parquet"))
        if f"{path.stem[:4]}-{path.stem[4:]}" in window["competences"]
    ]
    audit = {
        "excluded_continuations": 0,
        "excluded_other_aih_types": 0,
        "invalid_identifiers": 0,
        "identical_duplicate_rows_removed": 0,
        "conflicting_identifiers": 0,
        "conflicting_rows_quarantined": 0,
        "outside_admission_window": 0,
        "invalid_admission_dates": 0,
        "invalid_age": 0,
        "other_diagnoses": 0,
        "unmapped_residence": 0,
        "episodes": 0,
    }
    # Every bucket touches almost every cell, so partial sums are merged as they come
    # and categorical keys are kept as enums: a list of 64 partial tables does not fit.
    region_enum = pl.Enum(sorted(lookup["health_region_code"].unique()))
    compact = {
        "health_region_code": region_enum,
        "age_group": pl.Enum(AGE_GROUPS),
        "outcome": pl.Enum(OUTCOMES),
    }
    running = None
    scan = pl.scan_parquet(files).select("ANO_CMPT", "MES_CMPT", "IDENT", *CLINICAL_FIELDS)
    for bucket in range(buckets):
        records = scan.filter(
            pl.col("N_AIH").str.slice(-4).cast(pl.Int32, strict=False).fill_null(0) % buckets
            == bucket
        ).collect(engine="streaming")
        ident = records["IDENT"].str.strip_chars()
        audit["excluded_continuations"] += int((ident == "5").sum())
        audit["excluded_other_aih_types"] += int((~ident.is_in(["1", "5"])).sum())
        normal = records.filter(ident == "1")
        del records
        valid_id = normal["N_AIH"].str.contains(r"^\d{13}$").fill_null(False)
        audit["invalid_identifiers"] += int((~valid_id).sum())
        normal = normal.filter(valid_id)
        # Keep the earliest competence, as the pilot does: it sets the IPCA month.
        unique = normal.sort("ANO_CMPT", "MES_CMPT").unique(
            subset=CLINICAL_FIELDS, keep="first", maintain_order=True
        )
        audit["identical_duplicate_rows_removed"] += normal.height - unique.height
        conflicting = unique.group_by("N_AIH").len().filter(pl.col("len") > 1)["N_AIH"]
        audit["conflicting_identifiers"] += conflicting.len()
        quarantined = unique["N_AIH"].is_in(conflicting.implode())
        audit["conflicting_rows_quarantined"] += int(quarantined.sum())
        episodes = classified(unique.filter(~quarantined).lazy()).collect()
        del normal, unique
        audit["invalid_admission_dates"] += episodes["date"].null_count()
        episodes = episodes.filter(pl.col("date").is_not_null())
        inside = episodes["date"].is_between(window["start"], window["end"])
        audit["outside_admission_window"] += int((~inside).sum())
        episodes = episodes.filter(inside)
        audit["other_diagnoses"] += episodes["outcome"].null_count()
        episodes = episodes.filter(pl.col("outcome").is_not_null())
        audit["invalid_age"] += episodes["age_group"].null_count()
        episodes = episodes.filter(pl.col("age_group").is_not_null()).join(
            lookup, on="municipality_code", how="left"
        )
        audit["unmapped_residence"] += episodes["health_region_code"].null_count()
        episodes = episodes.filter(pl.col("health_region_code").is_not_null()).join(
            ipca.rename({"month": "competence"}), on="competence", how="left"
        )
        if episodes["ipca_index"].null_count():
            raise ValueError("IPCA index missing for a processing competence; refresh it")
        audit["episodes"] += episodes.height
        episodes = episodes.with_columns(
            (pl.col("VAL_TOT") * reference_index / pl.col("ipca_index")).alias("payment")
        )
        respiratory = episodes.filter(pl.col("outcome").is_in(["asthma", "pneumonia"]))
        partial = (
            pl.concat([episodes, respiratory.with_columns(pl.lit("respiratory").alias("outcome"))])
            .with_columns(pl.col(name).cast(dtype) for name, dtype in compact.items())
            .group_by(KEYS)
            .agg(
                pl.len().cast(pl.UInt32).alias("admissions"),
                # sum() of only nulls is 0, which would read as a free admission.
                pl.when(pl.col("payment").is_not_null().all()).then(pl.col("payment").sum()),
            )
        )
        del episodes, respiratory
        running = partial if running is None else _merge(running, partial)
    # A few foreign or ignored residences are expected; more means a territory mismatch.
    if audit["unmapped_residence"] > MAX_UNMAPPED_SHARE * (
        audit["episodes"] + audit["unmapped_residence"]
    ):
        raise ValueError(
            f"{audit['unmapped_residence']} admissions with unmapped residence; "
            "update the territorial correspondence"
        )
    if running is None:
        raise ValueError("No SIH partition inside the certified window")
    counts = (
        running.with_columns(pl.col(name).cast(pl.String) for name in compact)
        .with_columns(pl.col("admissions").cast(pl.Int64))
        .sort(KEYS)
    )
    del running
    partition_hashes = sorted(
        (
            json.loads(path.with_suffix(".source.json").read_text())["parquet_sha256"],
            f"{path.parent.name}/{path.stem}",
        )
        for path in files
    )
    tabnet = (
        national_root(root).parent.parent.parent / "marts" / "validation" / "tabnet-national.json"
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "classification": PRIVATE,
        "rows": counts.height,
        "sparse": True,
        "zero_fill": "inside admission_window only; see complete_daily_panel",
        "admission_window": [window["start"].isoformat(), window["end"].isoformat()],
        "competences": [window["competences"][0], window["competences"][-1]],
        "maturity_months": maturity_months,
        "incomplete_competences_after_run": window["incomplete_competences_after_run"],
        "payment": (
            f"VAL_TOT deflated by IPCA to {reference_month:%Y-%m}, by processing month; "
            "normal AIHs only, so continuation AIHs of long stays are not included"
        ),
        "outcomes": {
            "respiratory": "J00-J99 (includes asthma and pneumonia)",
            "asthma": "J45-J46",
            "pneumonia": "J12-J18",
            "circulatory": "I00-I99",
            "negative_control": "S00-S99, T00-T98",
        },
        "reconciliation": audit,
        "partitions": len(files),
        "partitions_sha256": hashlib.sha256(json.dumps(partition_hashes).encode()).hexdigest(),
        "parameters": {"buckets": buckets, "max_unmapped_share": MAX_UNMAPPED_SHARE},
        "software": software_provenance(),
        "tabnet_validation": _tabnet_status(tabnet, window),
        "inputs": {
            "lookup": [describe_input(lookup_path)],
            "ipca": [describe_input(ipca_path)],
            "sih_coverage": [describe_input(national_root(root) / "coverage.json")]
            if (national_root(root) / "coverage.json").exists()
            else [],
        },
    }
    write_with_manifest(output, counts.write_parquet, manifest)
    return counts
