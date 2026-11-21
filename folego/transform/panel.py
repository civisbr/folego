"""Daily research panel: outcomes, PM2.5, weather and holidays per region, age and outcome.

The panel spans the dates every exposure covers. Admissions are zero-filled only inside
the certified admission window of the outcome mart and left null outside it, so an
immature or missing month is never read as a day without admissions.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from .cams_exposure import describe_input, write_with_manifest
from .national_outcomes import AGE_GROUPS, OUTCOMES, PRIVATE


def _coverage(frame: pl.DataFrame, name: str) -> tuple[date, date]:
    """Common contiguous span of a daily regional series; any gap inside it fails."""
    if frame.select("date", "health_region_code").is_duplicated().any():
        raise ValueError(f"Duplicate {name} rows")
    spans = frame.group_by("health_region_code").agg(
        pl.col("date").min().alias("first"), pl.col("date").max().alias("last"), pl.len()
    )
    gaps = spans.filter((pl.col("last") - pl.col("first")).dt.total_days() + 1 != pl.col("len"))
    if gaps.height:
        raise ValueError(f"{name} has missing days in {gaps.height} regions")
    return spans["first"].max(), spans["last"].min()


def build_panel(
    outcomes_path: Path,
    pm25_path: Path,
    meteorology_path: Path,
    calendar_path: Path,
    output: Path,
    *,
    regions: list[str] | None = None,
    srag_path: Path | None = None,
    merra2_path: Path | None = None,
    fires_path: Path | None = None,
) -> dict:
    window = json.loads(outcomes_path.with_suffix(".parquet.source.json").read_text())
    admission_start, admission_end = (date.fromisoformat(d) for d in window["admission_window"])
    pm25 = pl.read_parquet(pm25_path).select("date", "health_region_code", "pm25")
    weather = pl.read_parquet(meteorology_path).select(
        "date", "health_region_code", "temperature", "humidity"
    )
    calendar = pl.read_parquet(calendar_path).select("date", "health_region_code", "holiday")
    sources = {"pm25": pm25, "meteorology": weather, "calendar": calendar}
    if srag_path is not None:
        # Viral SRAG with onset in the 7 days before t, on a log scale. Day t itself is
        # left out: same-day viral SRAG is partly the respiratory outcome and partly a
        # mediator of PM2.5, and adjusting for it would bias the effect toward null.
        srag = pl.read_parquet(srag_path).select("date", "health_region_code", "viral")
        _coverage(srag, "srag")
        sources["srag"] = (
            srag.sort("health_region_code", "date")
            .with_columns(
                pl.col("viral")
                .shift(1)
                .rolling_sum(window_size=7)
                .over("health_region_code")
                .log1p()
                .alias("srag")
            )
            .filter(pl.col("srag").is_not_null())
            .select("date", "health_region_code", "srag")
        )
    # Sensitivity exposures are joined without narrowing the main span, so the main
    # analysis does not change when they are added; days they lack stay null.
    optional = {
        "merra2": (merra2_path, ["pm25_merra2"]),
        "fires": (fires_path, ["fire_count", "frp"]),
    }
    extra_columns, extras = [], {}
    for name, (path, columns) in optional.items():
        if path is not None:
            extras[name] = pl.read_parquet(path).select("date", "health_region_code", *columns)
            _coverage(extras[name], name)
            extra_columns += columns
    codes = set(pm25["health_region_code"])
    for name, frame in {**sources, **extras}.items():
        if set(frame["health_region_code"]) != codes:
            raise ValueError(f"{name} covers different health regions than PM2.5")
    if regions is not None:
        unknown = set(regions) - codes
        if unknown:
            raise ValueError(f"Unknown health regions: {sorted(unknown)}")
        codes = set(regions)
    spans = [_coverage(frame, name) for name, frame in sources.items()]
    start, end = max(s for s, _ in spans), min(e for _, e in spans)
    if start > end:
        raise ValueError("Exposure sources do not overlap in time")
    exposures = pm25
    for name, frame in sources.items():
        if name != "pm25":
            exposures = exposures.join(frame, on=["date", "health_region_code"])
    for frame in extras.values():
        exposures = exposures.join(frame, on=["date", "health_region_code"], how="left")
    exposures = exposures.filter(
        pl.col("date").is_between(start, end) & pl.col("health_region_code").is_in(codes)
    )
    strata = pl.DataFrame({"age_group": AGE_GROUPS}, schema={"age_group": pl.String}).join(
        pl.DataFrame({"outcome": OUTCOMES}), how="cross"
    )
    outcomes = pl.scan_parquet(outcomes_path)
    outcome_regions = set(
        outcomes.select(pl.col("health_region_code").unique()).collect()["health_region_code"]
    )
    # A region coded differently in the outcome mart would join nothing and read as
    # zero admissions every day; that must fail instead.
    if missing := codes - outcome_regions:
        raise ValueError(f"{len(missing)} health regions have no admissions at all")
    if start > admission_end or end < admission_start:
        raise ValueError("Exposure dates do not overlap the certified admission window")
    inside = pl.col("date").is_between(admission_start, admission_end)
    columns = [
        "date",
        "health_region_code",
        "age_group",
        "outcome",
        "pm25",
        "temperature",
        "humidity",
        "holiday",
        *(["srag"] if srag_path is not None else []),
        *extra_columns,
        "admissions",
        "mean_payment",
    ]

    def region_panel(code: str) -> pl.DataFrame:
        return (
            exposures.filter(pl.col("health_region_code") == code)
            .join(strata, how="cross")
            .join(
                outcomes.filter(pl.col("health_region_code") == code).collect(),
                on=["date", "health_region_code", "age_group", "outcome"],
                how="left",
            )
            .with_columns(
                pl.when(inside).then(pl.col("admissions").fill_null(0)).alias("admissions"),
                pl.when(inside & (pl.col("admissions") > 0))
                .then(pl.col("payment") / pl.col("admissions"))
                .alias("mean_payment"),
            )
            .select(columns)
            .sort("age_group", "outcome", "date")
        )

    rows = 0

    def write(staged: Path) -> None:
        # One region at a time: the national panel (~44M rows) does not fit in memory.
        nonlocal rows
        import pyarrow.parquet as pq

        writer = None
        try:
            for code in sorted(codes):
                table = region_panel(code).to_arrow()
                if writer is None:
                    writer = pq.ParquetWriter(staged, table.schema, compression="zstd")
                writer.write_table(table)
                rows += table.num_rows
        finally:
            if writer is not None:
                writer.close()
        # Raising here discards the staged file before it replaces the panel.
        if rows != manifest["rows"]:
            raise ValueError(f"Panel wrote {rows} rows, expected {manifest['rows']}")

    days = (end - start).days + 1
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "classification": PRIVATE,
        "rows": days * len(codes) * strata.height,  # checked while writing
        "regions": len(codes),
        "dates": [start.isoformat(), end.isoformat()],
        "admission_window": window["admission_window"],
        "admissions_outside_window": "null",
        "mean_payment": window["payment"],
        "inputs": {
            name: [describe_input(path)]
            for name, path in {
                "outcomes": outcomes_path,
                "pm25": pm25_path,
                "meteorology": meteorology_path,
                "calendar": calendar_path,
                **({"srag": srag_path} if srag_path is not None else {}),
                **{name: path for name, (path, _) in optional.items() if path is not None},
            }.items()
        },
    }
    write_with_manifest(output, write, manifest)
    return manifest
