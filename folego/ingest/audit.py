"""Separate snapshot integrity, upstream availability and new-data validation."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .ibge import build_correspondence


def audit_territory(
    output=Path("data/marts/territory-audit.json"),
    snapshot=Path("web/public/data"),
    root=Path("data"),
):
    cities = json.loads((snapshot / "municipalities.json").read_text())
    coverage = json.loads((snapshot / "territory-coverage.json").read_text())
    codes = {r["municipality_code"] for r in cities}
    if (
        len(codes) != len(cities)
        or coverage["coverage"] != 1
        or coverage["matched"] != len(cities)
        or coverage["unmapped"]
        or any(not r["health_region_code"] for r in cities)
    ):
        raise ValueError("Invalid frozen territorial snapshot")
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "snapshot_status": "passed",
        "snapshot_date": coverage["snapshot_date"],
        "snapshot_municipalities": len(cities),
        "upstream_status": "pending",
        "freshness_verified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _, fresh = build_correspondence(root, refresh=True)
        report.update(
            upstream_status="available",
            freshness_verified=True,
            upstream_coverage=fresh,
            status="passed",
        )
    except httpx.HTTPError as error:
        # Never label a stale snapshot as fresh. Data/coverage errors still fail.
        report.update(
            upstream_status="unavailable", status="degraded", error_type=type(error).__name__
        )
        print(
            "::warning::Fonte territorial indisponível; "
            "snapshot preservado, atualização não verificada."
        )
    except Exception:
        report.update(upstream_status="validation_failed", status="failed")
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
