from datetime import date

import polars as pl
import pytest

from folego.evaluate.backtest import rolling_origin_splits
from folego.evaluate.metrics import peak_metrics, weekly_metrics
from folego.publish.export_web import suppress_small_cells


def test_suppression_removes_payments_and_related_breakdowns():
    frame = pl.DataFrame(
        {
            "admissions": [0, 4, 5],
            "payment": [0.0, 400.0, 500.0],
            "mean_payment": [0.0, 100.0, 100.0],
        }
    )
    result = suppress_small_cells(frame)
    assert result["admissions"].to_list() == [None, None, 5]
    assert result["payment"].to_list() == [None, None, 500.0]
    assert result["mean_payment"].to_list() == [None, None, 100.0]


def test_metrics_do_not_divide_by_zero():
    result = weekly_metrics([0, 10, 20], [1, 12, 18])
    assert result["mae"] == 5 / 3
    assert result["mape"] == 15
    assert result["mape_excluded_zero"] == 1
    assert peak_metrics([False, True, True], [False, True, False])["recall"] == 0.5


def test_backtest_never_trains_on_future():
    splits = list(
        rolling_origin_splits(
            [date(2018, 1, 1), date(2019, 8, 1), date(2020, 8, 1)], years=[2019, 2020]
        )
    )
    assert len(splits) == 2
    assert all(max(train) < min(test) for _, train, test in splits)


def _pooled_report(meteorology, spec, rr, dirty=False):
    effect = {"rr": rr, "lower": rr - 0.01, "upper": rr + 0.01, "i2": 0.1, "regions": 2}
    return {
        "meteorology": meteorology,
        "model_spec_version": spec,
        "any_region_dirty": dirty,
        "generated_at": "2026-11-25T00:00:00+00:00",
        "software": {"git_commit": "c0ffee0", "git_dirty": False},
        "inputs": [{"region": "11001", "git_commit": "beef001"}],
        "panel_sha256": ["abc"],
        "regions": ["11001", "11002"],
        "exposure_source": "CAMS",
        "method": "DL",
        "basis": {"reference": 15.0, "max_lag": 7, "requested_knots": [15.0, 35.0]},
        "results": [
            {
                "outcome": "respiratory",
                "age_group": "all",
                "effect": effect,
                "effect_hac": effect,
                "effect_with_lagged_outcome": {"regions": 0},
            }
        ],
    }


def _write_pooled(tmp_path, **overrides):
    import json

    specs = {
        "era5": ("ERA5", 7, 1.02),
        "merra2": ("MERRA-2", 7, 1.01),
        "spec6": ("MERRA-2", 6, 1.0),
    }
    paths = {}
    for name, (met, spec, rr) in specs.items():
        report = _pooled_report(met, spec, rr)
        report.update(overrides.get(name, {}))
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(report))
    return paths


def test_export_study_keeps_only_pooled_estimates(tmp_path):
    import json

    from folego.publish.export_web import export_study

    paths = _write_pooled(tmp_path)
    payload = export_study(paths["era5"], paths["merra2"], paths["spec6"], tmp_path / "s.json")
    (stratum,) = payload["strata"]
    assert stratum["main"]["rr"] == 1.02 and stratum["merra2"]["rr"] == 1.01
    assert stratum["spec6"]["rr"] == 1.0 and stratum["lagged"] is None
    assert payload["contrast_ug_m3"] == [15.0, 25.0] and payload["regions"] == 2
    assert "11001" not in (tmp_path / "s.json").read_text()
    assert json.loads((tmp_path / "s.json").read_text()) == payload
    assert [s["git_commit"] for s in payload["sources"]] == ["c0ffee0"] * 3
    assert payload["sources"][0]["region_commits"] == ["beef001"]
    assert payload["dirty_check_includes_untracked"] is False
    assert (tmp_path / "s.json").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"era5": {"any_region_dirty": True}}, "uncommitted"),
        ({"merra2": {"meteorology": "ERA5"}}, "expected MERRA-2"),
        ({"spec6": {"regions": ["11001"]}}, "different regions"),
        ({"spec6": {"model_spec_version": 7}}, "spec 6"),
        ({"spec6": {"panel_sha256": ["other"]}}, "different panels"),
        ({"merra2": {"exposure_source": "MERRA-2"}}, "exposure_source"),
        ({"era5": {"software": {"git_commit": None, "git_dirty": False}}}, "git commit"),
        ({"merra2": {"software": {"git_commit": "c0ffee0", "git_dirty": True}}}, "uncommitted"),
        (
            {"era5": {"results": [{"outcome": "<b>", "age_group": "all", "effect": {}}]}},
            "Unknown stratum|differ in strata",
        ),
        (
            {
                "era5": {
                    "results": [
                        {
                            "outcome": "asthma",
                            "age_group": "all",
                            "effect": {"rr": "1", "lower": 1, "upper": 1, "i2": 0, "regions": 2},
                        }
                    ]
                }
            },
            "non-numeric|differ in strata",
        ),
    ],
)
def test_export_study_refuses_mismatched_or_dirty_reports(tmp_path, overrides, message):
    from folego.publish.export_web import export_study

    paths = _write_pooled(tmp_path, **overrides)
    with pytest.raises(ValueError, match=message):
        export_study(paths["era5"], paths["merra2"], paths["spec6"], tmp_path / "s.json")


def test_published_study_matches_committed_evidence(tmp_path):
    from pathlib import Path

    from folego.publish.export_web import export_study

    evidence = Path("docs/evidence")
    fresh = tmp_path / "study.json"
    export_study(
        evidence / "smoke-cams-era5-knots15_35-spec7.json",
        evidence / "smoke-cams-knots15_35-spec7.json",
        evidence / "smoke-cams-knots15_35-spec6.json",
        fresh,
    )
    # Fails when the evidence changed and `folego export-study` was not rerun.
    assert fresh.read_text() == Path("web/public/data/study.json").read_text()


def test_cli_export_study_exits_cleanly_on_bad_input(tmp_path, monkeypatch, capsys):
    import sys

    from folego.cli import main

    paths = _write_pooled(tmp_path, era5={"any_region_dirty": True})
    argv = ["folego", "export-study", "--output", str(tmp_path / "s.json")]
    for name, path in paths.items():
        argv += [f"--{name}", str(path)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert "uncommitted" in capsys.readouterr().err
    assert not (tmp_path / "s.json").exists()
