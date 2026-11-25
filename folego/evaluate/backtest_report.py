"""Forecast skill across regions, from ``study --backtest-only`` reports."""

import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

from ..ingest.common import atomic_write_text, read_json_hashed
from ..ingest.sih_national import software_provenance
from ..transform.cams_exposure import _portable

# Reports that differ in any of these measure different forecasts.
SHARED_FIELDS = (
    "exposure",
    "meteorology",
    "exposure_source",
    "design",
    "backtest_inputs",
    "model_spec_version",
)


def _median(values):
    return statistics.median(values) if values else None


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _check(reports: list[dict]) -> dict:
    for report in reports:
        if not isinstance(report, dict) or not re.fullmatch(r"\d{5}", str(report.get("region"))):
            raise ValueError("Not a backtest report (no 5-digit region)")
        if not isinstance(report.get("results"), list) or "design" not in report:
            raise ValueError(f"Not a backtest report: region {report['region']}")
    regions = [r["region"] for r in reports]
    if len(set(regions)) != len(regions):
        raise ValueError("Each region must appear once")
    shared = {}
    for field in SHARED_FIELDS:
        if len({json.dumps(r.get(field)) for r in reports}) != 1:
            raise ValueError(f"Reports differ in {field}")
        shared[field] = reports[0].get(field)
    # Effective knots differ by region by design; the rest of the basis must not.
    bases = {
        json.dumps({k: v for k, v in (r.get("basis") or {}).items() if k != "knots"})
        for r in reports
    }
    if len(bases) != 1:
        raise ValueError("Reports differ in basis")
    shared["basis"] = {k: v for k, v in (reports[0].get("basis") or {}).items() if k != "knots"}
    panels = {json.dumps([p.get("sha256") for p in r["inputs"]["panel"]]) for r in reports}
    if len(panels) != 1:
        raise ValueError("Reports differ in panel_sha256")
    return shared


def summarize(reports: list[dict]) -> dict:
    if not reports:
        raise ValueError("No backtest reports")
    shared = _check(reports)
    by_outcome: dict[str, dict] = {}
    for report in reports:
        for result in report["results"]:
            slot = by_outcome.setdefault(
                result["outcome"],
                {"regions": set(), "stratum_errors": 0, "fold_errors": 0, "folds": []},
            )
            if "error" in result:
                slot["stratum_errors"] += 1
                continue
            for fold in result["folds"]:
                if "error" in fold or "baseline" not in fold or fold.get("mae") is None:
                    slot["fold_errors"] += 1
                    continue
                slot["regions"].add(report["region"])
                slot["folds"].append(fold)
    summary = []
    for outcome, slot in sorted(by_outcome.items()):
        folds = slot["folds"]
        paired = [f for f in folds if f["baseline"]["mae"]]
        entry = {
            "outcome": outcome,
            "regions": len(slot["regions"]),
            "folds": len(folds),
            "fold_errors": slot["fold_errors"],
            "stratum_errors": slot["stratum_errors"],
            "median_mape": _median([f["mape"] for f in folds if f["mape"] is not None]),
            "median_mape_baseline": _median(
                [f["baseline"]["mape"] for f in folds if f["baseline"]["mape"] is not None]
            ),
            # Below 1 means PM2.5 lowered the weekly error against the same model without it.
            "median_mae_ratio": _median([f["mae"] / f["baseline"]["mae"] for f in paired]),
            "paired_folds": len(paired),
            "share_folds_pm25_better": _ratio(
                sum(f["mae"] < f["baseline"]["mae"] for f in paired), len(paired)
            ),
            "observed_peaks": sum(f["observed_peaks"] for f in folds),
        }
        for key, read in (
            ("peaks", lambda f: f["peaks"]),
            ("peaks_baseline", lambda f: f["baseline"]["peaks"]),
        ):
            hits = sum(read(f)["hits"] for f in folds)
            entry[key] = {
                "precision": _ratio(hits, sum(read(f)["predicted"] for f in folds)),
                "recall": _ratio(hits, entry["observed_peaks"]),
                "hits": hits,
            }
        years = sorted({f["year"] for f in folds})
        entry["by_year"] = [
            {
                "year": year,
                "folds": sum(f["year"] == year for f in folds),
                "median_mape": _median(
                    [f["mape"] for f in folds if f["year"] == year and f["mape"] is not None]
                ),
                "median_mae_ratio": _median(
                    [f["mae"] / f["baseline"]["mae"] for f in paired if f["year"] == year]
                ),
            }
            for year in years
        ]
        summary.append(entry)
    return {
        "method": (
            "Per region and outcome (all ages), rolling-origin folds; weekly MAE/MAPE of the"
            " model with PM2.5 and of the same confounders without it; peaks are test weeks"
            " above the 90th percentile of training weeks, pooled across folds as"
            " hits/predicted (precision) and hits/observed (recall)"
        ),
        **shared,
        "regions": sorted(r["region"] for r in reports),
        # False when an older report checked only tracked files, so any_region_dirty is weaker.
        "dirty_check_includes_untracked": all(
            (r.get("software") or {}).get("git_dirty_includes_untracked") is True for r in reports
        ),
        "any_region_dirty": any(
            (r.get("software") or {}).get("git_dirty") is not False for r in reports
        ),
        "results": summary,
    }


def backtest_report(paths: list[Path], output: Path | None = None) -> dict:
    paths = [Path(p) for p in paths]
    if output is not None and output.resolve() in {p.resolve() for p in paths}:
        raise ValueError("Output would overwrite one of the backtest reports")
    loaded = [read_json_hashed(p) for p in paths]
    reports = [report for report, _ in loaded]
    summary = summarize(reports)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "software": software_provenance(),
        "mode": "research",
        "validated": False,
        "inputs": sorted(
            (
                {
                    "region": r["region"],
                    "path": _portable(p),
                    "sha256": digest,
                    "git_commit": (r.get("software") or {}).get("git_commit"),
                    "git_dirty": (r.get("software") or {}).get("git_dirty"),
                }
                for p, (r, digest) in zip(paths, loaded, strict=True)
            ),
            key=lambda i: i["region"],
        ),
        **summary,
    }
    if output is not None:
        text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
        history = output.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
        atomic_write_text(history / f"{output.stem}-{stamp}.json", text)
        atomic_write_text(output, text)
    return report
