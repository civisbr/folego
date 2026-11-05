import polars as pl


def population_weighted_exposure(pixels: pl.DataFrame) -> pl.DataFrame:
    """Input is the pixel × region intersection with population counts, not area."""
    if (
        pixels.is_empty()
        or pixels.select(
            pl.any_horizontal(
                pl.col("pm25").is_null() | ~pl.col("pm25").is_finite() | (pl.col("pm25") < 0),
                pl.col("population").is_null()
                | ~pl.col("population").is_finite()
                | (pl.col("population") < 0),
            ).any()
        ).item()
    ):
        raise ValueError("Missing or invalid population/PM2.5; do not silently reweight")
    result = pixels.group_by("date", "health_region_code").agg(
        (pl.col("pm25") * pl.col("population")).sum().alias("weighted"),
        pl.col("population").sum().alias("population"),
    )
    if (result["population"] <= 0).any():
        raise ValueError("Zero regional population")
    return result.with_columns((pl.col("weighted") / pl.col("population")).alias("pm25")).drop(
        "weighted"
    )


def ipca_adjust(payment: float, source_index: float, target_index: float) -> float:
    if min(source_index, target_index) <= 0:
        raise ValueError("IPCA indices must be positive")
    return payment * target_index / source_index
