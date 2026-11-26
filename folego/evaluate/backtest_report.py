"""Forecast skill across regions, from ``study --backtest-only`` reports."""

import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

from ..ingest.common import atomic_write_text, read_json_hashed
from ..ingest.sih_national import software_provenance
from ..transform.cams_exposure import _portable
from .pool import panel_hashes

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
    if len({json.dumps(panel_hashes(r)) for r in reports}) != 1:
        raise ValueError("Reports differ in panel_sha256")
    shared["panel_sha256"] = panel_hashes(reports[0])
    return shared


def summarize(reports: list[dict]) -> dict:
    if not reports:
        raise ValueError("No backtest reports")
    if any(isinstance(r, dict) and "forecasts" in (r.get("inputs") or {}) for r in reports):
        # Their folds have no top-level MAE; this summary would count them all as errors.
        raise ValueError("Forecast backtest reports need backtest-report --forecast")
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


def backtest_report(paths: list[Path], output: Path | None = None, *, forecast=False) -> dict:
    paths = [Path(p) for p in paths]
    if output is not None and output.resolve() in {p.resolve() for p in paths}:
        raise ValueError("Output would overwrite one of the backtest reports")
    loaded = [read_json_hashed(p) for p in paths]
    reports = [report for report, _ in loaded]
    summary = (summarize_forecast if forecast else summarize)(reports)
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


def _forecast_hashes(report: dict) -> list:
    inputs = report.get("inputs")
    forecasts = inputs.get("forecasts") if isinstance(inputs, dict) else None
    hashes = [f.get("sha256") if isinstance(f, dict) else None for f in forecasts or []]
    if (
        not isinstance(forecasts, list)
        or not hashes
        or not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    ):
        raise ValueError(f"Region {report.get('region')} has no valid forecasts sha256")
    return hashes


def summarize_forecast(reports: list[dict]) -> dict:
    """Operational backtest across regions: forecast PM2.5 against oracle and baseline."""
    from .forecast_backtest import LEAD_DAYS, VARIANTS

    if not reports:
        raise ValueError("No forecast backtest reports")
    shared = _check(reports)
    if len({json.dumps(_forecast_hashes(r)) for r in reports}) != 1:
        raise ValueError("Reports differ in forecasts_sha256")
    shared["forecasts_sha256"] = _forecast_hashes(reports[0])
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
                if "error" in fold or any(
                    not isinstance(fold.get(v), dict) or fold[v].get("mae") is None
                    for v in VARIANTS
                ):
                    slot["fold_errors"] += 1
                    continue
                slot["regions"].add(report["region"])
                slot["folds"].append(fold)
    summary = []
    for outcome, slot in sorted(by_outcome.items()):
        folds = slot["folds"]

        def ratio(numerator, denominator, rows=folds):
            return _median(
                [f[numerator]["mae"] / f[denominator]["mae"] for f in rows if f[denominator]["mae"]]
            )

        entry = {
            "outcome": outcome,
            "regions": len(slot["regions"]),
            "folds": len(folds),
            "issue_days": sum(f["issue_days"] for f in folds),
            "fold_errors": slot["fold_errors"],
            "stratum_errors": slot["stratum_errors"],
            # Each below 1 means the numerator's 5-day totals erred less.
            "median_mae_ratio_forecast_baseline": ratio("forecast", "baseline"),
            "median_mae_ratio_observed_baseline": ratio("observed", "baseline"),
            "median_mae_ratio_forecast_observed": ratio("forecast", "observed"),
            "share_folds_forecast_better_than_baseline": _ratio(
                sum(f["forecast"]["mae"] < f["baseline"]["mae"] for f in folds), len(folds)
            ),
            **{
                f"median_mape_{v}": _median(
                    [f[v]["mape"] for f in folds if f[v]["mape"] is not None]
                )
                for v in VARIANTS
            },
            "observed_peaks": sum(f["observed_peaks"] for f in folds),
        }
        for variant in VARIANTS:
            hits = sum(f[variant]["peaks"]["hits"] for f in folds)
            entry[f"peaks_{variant}"] = {
                "precision": _ratio(hits, sum(f[variant]["peaks"]["predicted"] for f in folds)),
                "recall": _ratio(hits, entry["observed_peaks"]),
                "hits": hits,
            }
        entry["by_lead"] = [
            {
                "lead_day": k,
                "median_mae_ratio_forecast_baseline": _median(
                    [
                        lead["mae_forecast"] / lead["mae_baseline"]
                        for f in folds
                        for lead in f["by_lead"]
                        if lead["lead_day"] == k and lead["mae_baseline"]
                    ]
                ),
                "median_mae_ratio_forecast_observed": _median(
                    [
                        lead["mae_forecast"] / lead["mae_observed"]
                        for f in folds
                        for lead in f["by_lead"]
                        if lead["lead_day"] == k and lead["mae_observed"]
                    ]
                ),
            }
            for k in range(LEAD_DAYS)
        ]
        summary.append(entry)
    exposure = []
    keys = sorted(
        {(e["season"], e["lead_day"]) for r in reports for e in r.get("exposure_error") or []}
    )
    for season, lead in keys:
        rows = [
            e
            for r in reports
            for e in r.get("exposure_error") or []
            if (e["season"], e["lead_day"]) == (season, lead)
        ]
        exposure.append(
            {
                "season": season,
                "lead_day": lead,
                "regions": len(rows),
                **{
                    f"median_{k}": _median([e[k] for e in rows if e.get(k) is not None])
                    for k in ("mean_observed", "bias", "mae", "correlation")
                },
            }
        )
    return {
        "method": (
            "Per region and outcome (all ages), one fold per year; each issue day predicts"
            " lead days 0-4 with the archived forecast as future PM2.5 (forecast), with the"
            " observed PM2.5 (observed, an oracle) and without PM2.5 (baseline). MAE/MAPE of"
            " each issue's 5-day total; peaks are 5-day totals above the training P90."
            " Consecutive issues overlap by four days, so one high stretch can count as"
            " several observed peaks and hits."
        ),
        **shared,
        "regions": sorted(r["region"] for r in reports),
        "dirty_check_includes_untracked": all(
            (r.get("software") or {}).get("git_dirty_includes_untracked") is True for r in reports
        ),
        "any_region_dirty": any(
            (r.get("software") or {}).get("git_dirty") is not False for r in reports
        ),
        "exposure_error": exposure,
        "results": summary,
    }
