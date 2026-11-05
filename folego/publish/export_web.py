"""Privacy gates run before any public export."""

import json
from pathlib import Path

import polars as pl


def suppress_small_cells(frame: pl.DataFrame) -> pl.DataFrame:
    """Release one aggregation level only: no totals/margins enabling subtraction.

    Every numeric measure in a small cell is suppressed, including cost means.
    """
    small = pl.col("admissions") < 5
    return frame.with_columns(
        [
            pl.when(small).then(None).otherwise(pl.col(name)).alias(name)
            for name, dtype in frame.schema.items()
            if dtype.is_numeric()
        ]
    )


def export_aggregates(frame: pl.DataFrame, output: Path) -> None:
    forbidden = {
        "N_AIH",
        "CPF",
        "NOME",
        "MUNIC_RES",
        "DT_INTER",
        "DIAG_PRINC",
        "IDADE",
        "COD_IDADE",
    }
    if forbidden.intersection(frame.columns):
        raise ValueError("Microdata must never be published")
    required = {"date", "health_region_code", "age_group", "outcome", "admissions", "payment"}
    if set(frame.columns) != required:
        raise ValueError("Export only the documented aggregate schema")
    if frame.select("date", "health_region_code", "age_group", "outcome").is_duplicated().any():
        raise ValueError("Duplicate aggregate cells")
    if frame["outcome"].n_unique() != 1:
        raise ValueError("Release a single outcome to prevent overlapping subtotal disclosure")
    if not set(frame["age_group"].unique()).issubset({"0-4", "5-14", "15-59", "60+"}):
        raise ValueError("Only disjoint age groups are publishable")
    public = suppress_small_cells(frame)
    output.mkdir(parents=True, exist_ok=True)
    public.write_parquet(output / "aggregates.parquet")
    (output / "aggregates.json").write_text(
        json.dumps(
            public.with_columns(pl.col("date").cast(pl.String)).to_dicts(),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
