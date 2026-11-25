"""Privacy gates run before any public export."""

import json
import math
import re
from pathlib import Path

import polars as pl

from ..ingest.common import atomic_write_text, sha256_file
from ..transform.cams_exposure import _portable


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


STUDY_EFFECTS = {"effect": "main", "effect_hac": "hac", "effect_with_lagged_outcome": "lagged"}


def _pooled(path: Path, meteorology: str, spec: int) -> dict:
    report = json.loads(path.read_text())
    if report.get("meteorology") != meteorology or report.get("model_spec_version") != spec:
        raise ValueError(f"{path.name}: expected {meteorology} meteorology, spec {spec}")
    # Public figures must trace back to committed code, region by region.
    if report.get("any_region_dirty") is not False:
        raise ValueError(f"{path.name}: some region came from uncommitted code")
    if (report.get("software") or {}).get("git_dirty") is not False:
        raise ValueError(f"{path.name}: pooled from uncommitted code")
    return report


def _summary(effect: dict) -> dict | None:
    if not effect.get("regions"):
        return None
    numbers = [effect.get(k) for k in ("rr", "lower", "upper", "i2")]
    # The page writes these into HTML; anything but plain numbers is refused here.
    if not all(
        isinstance(v, float | int) and not isinstance(v, bool) and math.isfinite(v) for v in numbers
    ) or not (isinstance(effect["regions"], int) and not isinstance(effect["regions"], bool)):
        raise ValueError("Pooled effect with non-numeric fields")
    return {
        key: round(effect[key], 6) if key != "regions" else effect[key]
        for key in ("rr", "lower", "upper", "i2", "regions")
    }


STUDY_OUTCOMES = {"respiratory", "pneumonia", "asthma", "circulatory", "negative_control"}
STUDY_AGES = {"all", "0-4", "60+"}


def _commit(report: dict) -> str:
    commit = (report.get("software") or {}).get("git_commit")
    if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit)):
        raise ValueError("Pooled report without a git commit")
    return commit


def export_study(era5: Path, merra2: Path, spec6: Path, output: Path) -> dict:
    """Pooled relative risks only: no counts, no region-level estimates."""
    main = _pooled(era5, "ERA5", 7)
    alternative = _pooled(merra2, "MERRA-2", 7)
    previous = _pooled(spec6, "MERRA-2", 6)
    reports = (main, alternative, previous)
    if not main["regions"] == alternative["regions"] == previous["regions"]:
        raise ValueError("Pooled reports cover different regions")
    for field in ("exposure_source", "basis"):
        if len({json.dumps(r.get(field), sort_keys=True) for r in reports}) != 1:
            raise ValueError(f"Pooled reports differ in {field}")
    # The page compares versions 6 and 7 on the same panel.
    if alternative["panel_sha256"] != previous["panel_sha256"]:
        raise ValueError("Versions 6 and 7 were pooled from different panels")
    keys = [{(r["outcome"], r["age_group"]) for r in report["results"]} for report in reports]
    if not keys[0] == keys[1] == keys[2]:
        raise ValueError("Pooled reports differ in strata")
    by_stratum = {
        name: {(r["outcome"], r["age_group"]): r for r in report["results"]}
        for name, report in (("merra2", alternative), ("spec6", previous))
    }
    strata = []
    for result in main["results"]:
        key = (result["outcome"], result["age_group"])
        # Only labels the page knows are published; they also end up in its HTML.
        if key[0] not in STUDY_OUTCOMES or key[1] not in STUDY_AGES:
            raise ValueError(f"Unknown stratum {key}")
        entry = {"outcome": key[0], "age_group": key[1]}
        for field, name in STUDY_EFFECTS.items():
            entry[name] = _summary(result.get(field) or {})
        for name, results in by_stratum.items():
            entry[name] = _summary((results.get(key) or {}).get("effect") or {})
        strata.append(entry)
    basis = main["basis"]
    payload = {
        "mode": "research",
        "validated": False,
        "generated_at": main["generated_at"],
        "regions": len(main["regions"]),
        "exposure_source": main["exposure_source"],
        # study.cumulative_relative_risk reports every effect for a +10 µg/m³ step.
        "contrast_ug_m3": [basis["reference"], basis["reference"] + 10],
        "max_lag": basis["max_lag"],
        "knots_ug_m3": basis["requested_knots"],
        # Older pools checked only tracked files; the page words its claim accordingly.
        "dirty_check_includes_untracked": all(
            r.get("dirty_check_includes_untracked") is True for r in reports
        ),
        "method": main["method"],
        "sources": [
            {
                "role": role,
                "path": _portable(path),
                "sha256": sha256_file(path),
                "git_commit": _commit(report),
                # The regional studies may predate the pooling run.
                "region_commits": sorted(
                    {_commit({"software": i}) for i in report.get("inputs") or []}
                ),
                "meteorology": report["meteorology"],
                "model_spec_version": report["model_spec_version"],
                "generated_at": report["generated_at"],
            }
            for role, path, report in (
                ("main", era5, main),
                ("merra2", merra2, alternative),
                ("spec6", spec6, previous),
            )
        ],
        "strata": strata,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=1) + "\n"
    # A public web asset: readable by the static server, unlike private reports.
    atomic_write_text(output, text, mode=0o644)
    return payload
