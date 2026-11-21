"""Check the nationwide SIH extraction against TabNet, one residence state × year at a time.

TabNet counts normal AIHs (type 1) by processing competence and state of residence, so the
comparison uses the raw partitions before de-duplication, exactly like the pilot.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from ..ingest.sih import STATES
from ..ingest.sih_national import national_root
from ..ingest.tabnet import compare_monthly_counts, retrieve_reference

DEFAULT_CHECKS = (("RO", 2019), ("AM", 2013), ("SP", 2016), ("BA", 2021), ("RS", 2024))


def state_prefixes(lookup: pl.DataFrame) -> dict[str, str]:
    pairs = lookup.select("state", pl.col("ibge_code").str.slice(0, 2).alias("prefix")).unique()
    if pairs["state"].n_unique() != pairs.height:
        raise ValueError("A state maps to more than one IBGE prefix")
    return dict(pairs.iter_rows())


def observed_monthly(root: Path, state_prefix: str, year: int) -> pl.DataFrame:
    """Normal respiratory AIHs of one residence state, by processing month, from all files."""
    files = sorted(national_root(root).glob(f"{year}/*/{year}??.parquet"))
    present = {(p.parent.name, p.stem) for p in files}
    expected = {(s, f"{year}{m:02d}") for s in STATES for m in range(1, 13)}
    if missing := expected - present:
        raise ValueError(f"{len(missing)} SIH partitions of {year} missing; extract them first")
    counts = (
        pl.scan_parquet(files)
        .filter(
            (pl.col("IDENT").str.strip_chars() == "1")
            & pl.col("DIAG_PRINC").str.strip_chars().str.to_uppercase().str.contains(r"^J\d{2}")
            & pl.col("MUNIC_RES").str.strip_chars().str.starts_with(state_prefix)
            & (pl.col("ANO_CMPT") == str(year))
        )
        .group_by(pl.col("MES_CMPT").cast(pl.Int64, strict=False).alias("month"))
        .agg(pl.len().cast(pl.Int64).alias("admissions"))
        .collect()
    )
    if counts["month"].null_count() or not counts["month"].is_between(1, 12).all():
        raise ValueError("Invalid processing month in SIH partitions")
    return (
        pl.DataFrame({"month": range(1, 13)})
        .join(counts, on="month", how="left")
        .with_columns(pl.col("admissions").fill_null(0))
        .sort("month")
    )


def validate_national(
    root: Path, lookup_path: Path, checks=DEFAULT_CHECKS, *, output: Path | None = None
) -> dict:
    prefixes = state_prefixes(pl.read_parquet(lookup_path))
    results = []
    for state, year in checks:
        entry = {"state": state, "year": year}
        try:
            if state not in prefixes:
                raise ValueError(f"Unknown state {state!r}")
            observed = observed_monthly(root, prefixes[state], year)
            reference, source = retrieve_reference(year, state, None, root)
            entry.update(
                status="compared",
                source={k: source[k] for k in ("url", "retrieved_at", "sha256")},
                **compare_monthly_counts(observed, reference),
            )
        except httpx.HTTPError as error:
            entry.update(status="unavailable", error=type(error).__name__, accepted=False)
        except ValueError as error:
            entry.update(status="not_compared", error=str(error), accepted=False)
        results.append(entry)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "basis": "normal AIHs (IDENT 1), J00-J99, state of residence, processing competence",
        "accepted": all(r["accepted"] for r in results),
        "checks": results,
    }
    if output is not None:
        # Each run is kept as evidence; the fixed name always points at the latest one.
        history = output.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
        text = json.dumps(report, ensure_ascii=False, indent=2)
        (history / f"{output.stem}-{stamp}.json").write_text(text)
        staged = output.with_name(output.name + ".partial")
        staged.write_text(text)
        os.replace(staged, output)
    return report
