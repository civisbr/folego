"""Aggregate approved, de-duplicated AIHs by date of admission and residence."""

import polars as pl

from .codes import age_group, diagnosis_group, municipality_code


def aggregate_outcomes(frame: pl.DataFrame, lookup: pl.DataFrame) -> pl.DataFrame:
    if lookup["municipality_code"].n_unique() != lookup.height:
        raise ValueError("Municipality lookup must be unique")
    # AIH type 5 is a long-stay continuation, not a new admission.
    if "IDENT" in frame.columns:
        frame = frame.filter(pl.col("IDENT").cast(pl.String) == "1")
    if "N_AIH" in frame.columns:
        if frame["N_AIH"].n_unique() != frame.height:
            raise ValueError("Duplicate AIH identifiers; resolve revisions before aggregation")
    frame = frame.with_columns(
        pl.col("MUNIC_RES")
        .map_elements(municipality_code, return_dtype=pl.String)
        .alias("municipality_code"),
        pl.col("DT_INTER")
        .cast(pl.String)
        .str.replace_all("-", "")
        .str.to_date("%Y%m%d")
        .alias("date"),
        pl.col("DIAG_PRINC").map_elements(diagnosis_group, return_dtype=pl.String).alias("outcome"),
        pl.struct("COD_IDADE", "IDADE")
        .map_elements(
            lambda x: age_group(int(x["COD_IDADE"]), int(x["IDADE"])), return_dtype=pl.String
        )
        .alias("age_group"),
        pl.col("VAL_TOT").cast(pl.String).str.replace(",", ".").cast(pl.Float64).alias("payment"),
    ).join(
        lookup.select("municipality_code", "health_region_code"), on="municipality_code", how="left"
    )
    if frame["health_region_code"].null_count():
        raise ValueError("Unmapped municipalities: update the territorial correspondence first")
    frame = frame.filter(pl.col("outcome").is_not_null() & pl.col("age_group").is_not_null())
    respiratory = frame.filter(pl.col("outcome").is_in(["asthma", "pneumonia"])).with_columns(
        pl.lit("respiratory").alias("outcome")
    )
    return (
        pl.concat([frame, respiratory])
        .group_by("date", "health_region_code", "age_group", "outcome")
        .agg(pl.len().alias("admissions"), pl.col("payment").sum())
        .sort("date", "health_region_code", "age_group", "outcome")
    )


def complete_daily_panel(frame: pl.DataFrame, start, end) -> pl.DataFrame:
    """Zero-fill only within an explicitly certified complete SIH coverage window."""
    keys = ["health_region_code", "age_group", "outcome"]
    dates = pl.DataFrame({"date": pl.date_range(start, end, eager=True)})
    grid = dates.join(frame.select(keys).unique(), how="cross")
    return grid.join(frame, on=["date", *keys], how="left").with_columns(
        pl.col("admissions").fill_null(0), pl.col("payment").fill_null(0)
    )
