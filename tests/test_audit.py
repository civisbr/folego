import json

import httpx
import pytest

from folego.ingest import audit


def test_unavailable_upstream_is_never_reported_as_fresh(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectTimeout("unavailable")

    monkeypatch.setattr(audit, "build_correspondence", unavailable)
    output = tmp_path / "audit.json"
    result = audit.audit_territory(output=output)
    assert result["status"] == "degraded"
    assert result["snapshot_status"] == "passed"
    assert result["freshness_verified"] is False
    assert json.loads(output.read_text())["upstream_status"] == "unavailable"


def test_upstream_data_errors_still_fail(tmp_path, monkeypatch):
    def invalid(*args, **kwargs):
        raise ValueError("Missing municipality")

    monkeypatch.setattr(audit, "build_correspondence", invalid)
    output = tmp_path / "audit.json"
    with pytest.raises(ValueError, match="Missing municipality"):
        audit.audit_territory(output=output)
    assert json.loads(output.read_text())["status"] == "failed"


def test_successful_refresh_records_new_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audit, "build_correspondence", lambda *args, **kwargs: (None, {"coverage": 1})
    )
    result = audit.audit_territory(output=tmp_path / "audit.json")
    assert result["status"] == "passed" and result["freshness_verified"] is True
