"""Privacy-bounded publication of pilot validation and public file provenance."""

import json
from importlib.resources import files
from pathlib import Path
from string import Template


def publish_pilot_report(report: dict, destination: Path):
    """Release only broad counts and source-file provenance, never cohort rows."""
    monthly = report["tabnet"].get("months", [])
    disclose_months = bool(monthly) and all(
        min(m["observed"], m["reference"]) >= 5 for m in monthly
    )
    tabnet = {k: v for k, v in report["tabnet"].items() if k not in ("months", "source")}
    for key in ("total_observed", "total_reference"):
        if tabnet.get(key, 5) < 5:
            tabnet[key] = None
    tabnet["months"] = monthly if disclose_months else None
    if "source" in report["tabnet"]:
        tabnet["source"] = report["tabnet"]["source"]
    public = {
        key: report[key]
        for key in [
            "generated_at",
            "name",
            "mode",
            "health_region_code",
            "health_region",
            "processing_year",
            "municipalities",
            "territory_sha256",
            "territory_historical_consistency",
            "files_expected",
            "files_processed",
            "file_states",
            "national_file_coverage",
            "source_bytes",
            "scanned_records",
            "clinical_cohort_complete",
            "zero_filled",
            "payment_basis",
        ]
    }
    public.update(model_ready=False, tabnet=tabnet)
    for key in [
        "selected_residence_records",
        "normal_respiratory_processing_records",
        "normal_respiratory_outside_residence_state",
    ]:
        public[key] = report[key] if report[key] >= 5 else None
    public["quality"] = {
        "ambiguous_identifiers_require_review": report["reconciliation"]["conflicting_identifiers"]
        > 0,
        "invalid_admission_dates_require_review": report["invalid_admission_dates"] > 0,
        "invalid_age_units_require_review": report["invalid_age_units"] > 0,
        "continuations_excluded": True,
        "individual_identifiers_published": False,
    }
    public["source_files"] = [
        {
            key: part[key]
            for key in ["state", "year", "month", "source_url", "source_bytes", "source_sha256"]
        }
        for part in report["source_manifest"]
    ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_suffix(".json").write_text(
        json.dumps(public, ensure_ascii=False, allow_nan=False, indent=2)
    )
    monthly_table = "Não há referência mensal validada disponível para publicação."
    if disclose_months:
        lines = ["| Competência | Fôlego | TabNet | Diferença |", "| --- | ---: | ---: | ---: |"]
        for month in monthly:
            error = month["relative_error"]
            label = f"{error * 100:.2f}%" if error is not None else "não calculável"
            lines.append(
                f"| {report['processing_year']}-{month['month']:02d} | "
                f"{month['observed']} | {month['reference']} | {label} |"
            )
        monthly_table = "\n".join(lines)
    values = {
        "year": report["processing_year"],
        "health_region": report["health_region"],
        "municipality_count": len(report["municipalities"]),
        "generated_at": report["generated_at"],
        "municipalities": ", ".join(m["municipality"] for m in report["municipalities"]),
        "region": report["health_region_code"],
        "files_processed": report["files_processed"],
        "files_expected": report["files_expected"],
        "states": len(report["file_states"]),
        "scanned_records": f"{report['scanned_records']:,}".replace(",", "."),
        "respiratory_records": public["normal_respiratory_processing_records"],
        "outside_state": public["normal_respiratory_outside_residence_state"],
        "tabnet_status": "aprovada"
        if report["tabnet"].get("accepted")
        else report["tabnet"]["status"],
        "monthly_table": monthly_table,
        "name": report["name"],
        "compressed_mib": f"{report['source_bytes'] / 1024**2:.1f}",
        "json_filename": destination.with_suffix(".json").name,
    }
    template = files("folego").joinpath("resources/pilot-report.md").read_text()
    destination.write_text(Template(template).substitute(values))
    return public
