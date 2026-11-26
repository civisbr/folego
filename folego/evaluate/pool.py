"""Random-effects meta-analysis of per-region study reports."""

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from ..ingest.common import atomic_write_text, read_json_hashed
from ..ingest.sih_national import software_provenance
from ..transform.cams_exposure import _portable

# The value study.py builds its Wald intervals with, so the SE is recovered exactly.
Z95 = 1.96
EFFECTS = ("effect", "effect_hac", "effect_with_lagged_outcome")
# Pooling reports that differ in any of these would mix different estimands.
SHARED_FIELDS = ("model_spec_version", "exposure", "meteorology", "exposure_source")
# Effective knots differ by region by design; requested_knots must not.
REGIONAL_BASIS_FIELDS = ("knots",)


def dersimonian_laird(estimates, variances) -> dict:
    """DerSimonian-Laird random-effects pool of log relative risks."""
    if len(estimates) != len(variances) or not estimates:
        raise ValueError("Need matching, non-empty estimates and variances")
    if not all(v > 0 and math.isfinite(v) for v in variances):
        raise ValueError("Variances must be finite and positive")
    weights = [1 / v for v in variances]
    total = sum(weights)
    fixed = sum(w * y for w, y in zip(weights, estimates, strict=True)) / total
    q = sum(w * (y - fixed) ** 2 for w, y in zip(weights, estimates, strict=True))
    k = len(estimates)
    tau2 = 0.0
    if k > 1:
        # Σw − Σw²/Σw written without the cancellation of the textbook form.
        spread = sum(w * (total - w) for w in weights) / total
        tau2 = max(0.0, (q - (k - 1)) / spread) if spread > 0 else 0.0
    random_weights = [1 / (v + tau2) for v in variances]
    pooled = sum(w * y for w, y in zip(random_weights, estimates, strict=True)) / sum(
        random_weights
    )
    se = math.sqrt(1 / sum(random_weights))
    return {
        "log_rr": pooled,
        "se": se,
        "rr": math.exp(pooled),
        "lower": math.exp(pooled - Z95 * se),
        "upper": math.exp(pooled + Z95 * se),
        "tau2": tau2,
        "q": q,
        "i2": max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0,
        "regions": k,
    }


def _log_rr(effect) -> tuple[float, float] | None:
    """Log RR and its variance from a Wald interval, or None for a rejected effect."""
    if not isinstance(effect, dict):
        return None
    values = [effect.get(k) for k in ("rr", "lower", "upper")]
    if not all(_number(v) and v > 0 for v in values):
        return None
    rr, lower, upper = values
    se = (math.log(upper) - math.log(lower)) / (2 * Z95)
    if not se > 0:
        return None
    return math.log(rr), se * se


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def panel_hashes(report: dict) -> list[str]:
    """The panel hashes a report declares; a missing hash would make "same panel" vacuous."""
    inputs = report.get("inputs")
    panel = inputs.get("panel") if isinstance(inputs, dict) else None
    hashes = [p.get("sha256") if isinstance(p, dict) else None for p in panel or []]
    if (
        not isinstance(panel, list)
        or not hashes
        or not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    ):
        raise ValueError(f"Region {report.get('region')} has no valid panel sha256")
    return hashes


def _check_compatible(reports: list[dict]) -> dict:
    for report in reports:
        if not isinstance(report, dict) or not re.fullmatch(r"\d{5}", str(report.get("region"))):
            raise ValueError("Not a study report (no 5-digit region)")
        if not isinstance(report.get("results"), list) or not isinstance(
            report.get("inputs"), dict
        ):
            raise ValueError(f"Not a study report: region {report['region']}")
        if report.get("model_spec_version") is None:
            raise ValueError(f"Region {report['region']} has no model_spec_version")
    regions = [r["region"] for r in reports]
    if len(set(regions)) != len(regions):
        raise ValueError("Each region must appear once")
    shared = {}
    for field in SHARED_FIELDS:
        values = {json.dumps(r.get(field)) for r in reports}
        if len(values) != 1:
            raise ValueError(f"Reports differ in {field}")
        shared[field] = reports[0].get(field)
    for name, read in (
        ("panel_sha256", panel_hashes),
        (
            "basis",
            lambda r: {k: v for k, v in r["basis"].items() if k not in REGIONAL_BASIS_FIELDS},
        ),
        # The backtest design does not enter any pooled effect.
        ("confounders", lambda r: {k: v for k, v in r["confounders"].items() if k != "backtest"}),
    ):
        values = {json.dumps(read(r)) for r in reports}
        if len(values) != 1:
            raise ValueError(f"Reports differ in {name}")
        shared[name] = read(reports[0])
    return shared


def pool_reports(reports: list[dict], effects=EFFECTS) -> dict:
    if not reports:
        raise ValueError("No study reports to pool")
    shared = _check_compatible(reports)
    strata: dict[tuple[str, str], dict] = {}
    for report in reports:
        for result in report["results"]:
            stratum = strata.setdefault((result["outcome"], result["age_group"]), {})
            for key in effects:
                slot = stratum.setdefault(
                    key, {"estimates": [], "variances": [], "rejected": 0, "stratum_errors": 0}
                )
                if "error" in result:
                    # The stratum itself failed (no rows, design error), not the effect rules.
                    slot["stratum_errors"] += 1
                    continue
                value = _log_rr(result.get(key))
                if value is None:
                    slot["rejected"] += 1
                    continue
                slot["estimates"].append(value[0])
                slot["variances"].append(value[1])
                condition = result[key].get("covariance_condition")
                if _number(condition):
                    slot["max_condition"] = max(slot.get("max_condition", 0.0), condition)
    pooled = []
    for (outcome, age_group), stratum in sorted(strata.items()):
        entry = {"outcome": outcome, "age_group": age_group}
        for key, slot in stratum.items():
            summary = (
                dersimonian_laird(slot["estimates"], slot["variances"])
                if slot["estimates"]
                else {"regions": 0}
            )
            summary["rejected"] = slot["rejected"]
            summary["stratum_errors"] = slot["stratum_errors"]
            if "max_condition" in slot:
                summary["max_covariance_condition"] = slot["max_condition"]
            entry[key] = summary
        pooled.append(entry)
    return {
        "method": (
            "DerSimonian-Laird random effects on log RR; SE from the 95% Wald interval as"
            f" (log upper - log lower) / (2 × {Z95}); effects rejected by the study rules"
            " ('rejected') and strata that failed outright ('stratum_errors') are counted and"
            " left out; regions the study refused have no report and are not counted"
        ),
        **shared,
        "regions": sorted(r["region"] for r in reports),
        "any_region_dirty": any(
            (r.get("software") or {}).get("git_dirty") is not False for r in reports
        ),
        # False when an older report checked only tracked files, so any_region_dirty is weaker.
        "dirty_check_includes_untracked": all(
            (r.get("software") or {}).get("git_dirty_includes_untracked") is True for r in reports
        ),
        "results": pooled,
    }


def pool_studies(paths: list[Path], output: Path | None = None) -> dict:
    paths = [Path(p) for p in paths]
    if output is not None and output.resolve() in {p.resolve() for p in paths}:
        raise ValueError("Output would overwrite one of the study reports")
    loaded = [read_json_hashed(p) for p in paths]
    reports = [report for report, _ in loaded]
    pooled = pool_reports(reports)
    inputs = []
    for path, (study, digest) in zip(paths, loaded, strict=True):
        software = study.get("software") or {}
        inputs.append(
            {
                "region": study["region"],
                # Study reports keep a fixed name and are overwritten; the hash pins the run.
                "path": _portable(path),
                "sha256": digest,
                "generated_at": study.get("generated_at"),
                "git_commit": software.get("git_commit"),
                "git_dirty": software.get("git_dirty"),
            }
        )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "software": software_provenance(),
        "mode": "research",
        "validated": False,
        "inputs": sorted(inputs, key=lambda i: i["region"]),
        **pooled,
    }
    if output is not None:
        text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
        # Every run is kept; the fixed name points at the latest one.
        history = output.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
        atomic_write_text(history / f"{output.stem}-{stamp}.json", text)
        atomic_write_text(output, text)
    return report
