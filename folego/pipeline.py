"""Research forecast runner on prepared, private daily regional panels.

This runner deliberately does not promote output to the public dashboard. That
requires the scientific acceptance evidence in docs/validation.md first.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from .models.crossbasis import CrossBasis
from .models.forecast import forecast_excess
from .models.glm import ConfounderDesign, fit_count_model


def run_forecast(history: pl.DataFrame, future: pl.DataFrame, *, issued_on: date):
    keys = ["health_region_code", "age_group", "outcome"]
    required = {"date", *keys, "pm25", "temperature", "humidity", "holiday"}
    if not required.issubset(history.columns) or not required.issubset(future.columns):
        raise ValueError("Missing daily regional panel columns")
    if not {"admissions", "mean_payment"}.issubset(history.columns):
        raise ValueError("Training history needs admissions and IPCA-adjusted mean_payment")
    for frame in [history, future]:
        if frame.select("date", *keys).is_duplicated().any():
            raise ValueError("Duplicate daily rows")
    if history["date"].max() > issued_on or future["date"].min() <= issued_on:
        raise ValueError("Historical and future dates cross the forecast origin")
    # Conservative publication lag. A vintage-availability calendar is preferable.
    clinical_cutoff = issued_on - timedelta(days=62)
    output = []
    for group in future.select(keys).unique().sort(keys).to_dicts():
        condition = pl.all_horizontal([pl.col(k) == v for k, v in group.items()])
        past = history.filter(condition).sort("date")
        following = future.filter(condition).sort("date")
        if not 1 <= following.height <= 5:
            raise ValueError("Forecast horizon must be 1–5 days")
        if following["date"][0] != issued_on + timedelta(days=1):
            raise ValueError("Forecast must start the day after issuance")
        if past.is_empty() or past["date"][-1] != issued_on:
            raise ValueError("Need recent observed exposure up to issuance")
        for series in [past["date"].to_list(), following["date"].to_list()]:
            if any(b - a != timedelta(days=1) for a, b in zip(series, series[1:], strict=False)):
                raise ValueError("Missing daily rows; never bridge gaps with lag indices")
        basis = CrossBasis()
        exposure_design = basis.transform(past["pm25"].to_numpy())
        train_mask = np.array([d <= clinical_cutoff for d in past["date"]])
        train_mask &= np.isfinite(exposure_design).all(axis=1)
        train_mask &= past["admissions"].is_not_null().to_numpy()
        train = past.filter(pl.Series(train_mask))
        if train.height < 365:
            raise ValueError("Need at least a year of complete training history")
        confounders = ConfounderDesign(include_srag="srag" in past.columns)
        z_train = confounders.transform(train, fit=True)
        design = np.column_stack([exposure_design[train_mask], z_train])
        model = fit_count_model(train["admissions"].to_numpy(), design)
        z_future = confounders.transform(following)
        paid_rows = train.filter(pl.col("admissions") > 0)
        if (
            paid_rows["mean_payment"].null_count()
            or not paid_rows["mean_payment"].is_finite().all()
            or (paid_rows["mean_payment"] < 0).any()
            or train["admissions"].sum() <= 0
        ):
            raise ValueError("Missing/invalid IPCA-adjusted AIH payment or zero admissions")
        # Weight the AIH cell means by counts, not by days with different volumes.
        payment = float(
            (train["mean_payment"] * train["admissions"]).sum() / train["admissions"].sum()
        )
        result = forecast_excess(
            model,
            basis,
            past["pm25"].to_numpy(),
            following["pm25"].to_numpy(),
            z_future,
            cost_per_admission=payment,
        )
        output.append(
            {
                **group,
                "issued_on": issued_on.isoformat(),
                "horizon": following.height,
                "training_cutoff": clinical_cutoff.isoformat(),
                **result,
            }
        )
    if not output:
        raise ValueError("No forecast groups")
    return {"mode": "research", "validated": False, "results": output}


def forecast_files(history: Path, future: Path, issued_on: date, output: Path):
    result = run_forecast(pl.read_parquet(history), pl.read_parquet(future), issued_on=issued_on)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
