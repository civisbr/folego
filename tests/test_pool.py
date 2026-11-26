import json
import math

import pytest

from folego.evaluate.pool import dersimonian_laird, pool_reports, pool_studies


def test_dersimonian_laird_matches_hand_computation():
    result = dersimonian_laird([0.0, 0.2], [0.01, 0.01])
    # Q = 2 with 1 df, so tau² = (2 - 1) / (200 - 100) and each weight becomes 1 / 0.02.
    assert result["tau2"] == pytest.approx(0.01)
    assert result["log_rr"] == pytest.approx(0.1)
    assert result["se"] == pytest.approx(0.1)
    assert result["i2"] == pytest.approx(0.5)
    assert result["rr"] == pytest.approx(math.exp(0.1))


def test_homogeneous_estimates_reduce_to_fixed_effect():
    result = dersimonian_laird([0.1, 0.1, 0.1], [0.04, 0.01, 0.01])
    assert result["tau2"] == 0
    assert result["i2"] == 0
    assert result["se"] == pytest.approx(math.sqrt(1 / (25 + 100 + 100)))


@pytest.mark.parametrize("variances", [[0.0], [-1.0], [math.inf], [math.nan]])
def test_invalid_variances_raise(variances):
    with pytest.raises(ValueError, match="Variances"):
        dersimonian_laird([0.1], variances)


def _effect(rr, se, condition=1e12):
    return {
        "rr": rr,
        "lower": rr * math.exp(-1.96 * se),
        "upper": rr * math.exp(1.96 * se),
        "covariance_condition": condition,
    }


def _report(region, results, **overrides):
    report = {
        "region": region,
        "model_spec_version": 7,
        "exposure": "pm25",
        "meteorology": "MERRA-2",
        "exposure_source": "CAMS",
        "inputs": {"panel": [{"path": "p.parquet", "sha256": "a" * 64}]},
        "basis": {"requested_knots": [15, 35], "reference": 15, "max_lag": 7},
        "confounders": {"inference": "spec"},
        "software": {"git_commit": "c0ffee", "git_dirty": False},
        "results": results,
    }
    report.update(overrides)
    return report


def test_pool_counts_rejected_effects_and_keeps_largest_condition():
    reports = [
        _report(
            "11001",
            [{"outcome": "respiratory", "age_group": "all", "effect": _effect(1.02, 0.01, 1e12)}],
        ),
        _report(
            "11002",
            [{"outcome": "respiratory", "age_group": "all", "effect": _effect(1.01, 0.01, 3e14)}],
        ),
        _report(
            "11003",
            [{"outcome": "respiratory", "age_group": "all", "effect_error": "Degenerate fit"}],
        ),
        _report(
            "11004", [{"outcome": "respiratory", "age_group": "all", "error": "No admissions"}]
        ),
    ]
    (stratum,) = pool_reports(reports, effects=("effect",))["results"]
    effect = stratum["effect"]
    assert effect["regions"] == 2
    assert effect["rejected"] == 1 and effect["stratum_errors"] == 1
    assert effect["max_covariance_condition"] == 3e14
    assert effect["rr"] == pytest.approx(math.exp((math.log(1.02) + math.log(1.01)) / 2))


def test_all_rejected_stratum_has_no_estimate():
    reports = [_report("11001", [{"outcome": "asthma", "age_group": "60+", "error": "x"}])]
    (stratum,) = pool_reports(reports, effects=("effect",))["results"]
    assert stratum["effect"] == {"regions": 0, "rejected": 0, "stratum_errors": 1}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"model_spec_version": 6}, "model_spec_version"),
        ({"inputs": {"panel": [{"path": "p.parquet", "sha256": "b" * 64}]}}, "panel_sha256"),
        (
            {"basis": {"requested_knots": [15, 35, 75], "reference": 15, "max_lag": 7}},
            "basis",
        ),
        ({"confounders": {"inference": "old"}}, "confounders"),
        (
            {"confounders": {"inference": "spec", "autocorrelation_sensitivity": "HAC 3"}},
            "confounders",
        ),
        ({"exposure_source": "MERRA-2"}, "exposure_source"),
        ({"meteorology": "ERA5"}, "meteorology"),
        ({"model_spec_version": None}, "no model_spec_version"),
    ],
)
def test_incompatible_reports_are_refused(override, message):
    reports = [_report("11001", []), _report("11002", [], **override)]
    with pytest.raises(ValueError, match=message):
        pool_reports(reports)


def test_duplicate_region_is_refused():
    with pytest.raises(ValueError, match="once"):
        pool_reports([_report("11001", []), _report("11001", [])])


def test_pool_studies_writes_report_and_history(tmp_path):
    paths = []
    for region, rr in (("11001", 1.02), ("11002", 1.0)):
        path = tmp_path / f"{region}.json"
        path.write_text(
            json.dumps(
                _report(
                    region,
                    [{"outcome": "respiratory", "age_group": "all", "effect": _effect(rr, 0.02)}],
                )
            )
        )
        paths.append(path)
    output = tmp_path / "pooled" / "pool.json"
    output.parent.mkdir()
    report = pool_studies(paths, output)
    assert json.loads(output.read_text()) == report
    assert report["regions"] == ["11001", "11002"]
    first = report["inputs"][0]
    assert first["region"] == "11001" and first["git_dirty"] is False
    assert first["path"] == "11001.json" and len(first["sha256"]) == 64
    assert report["any_region_dirty"] is False
    assert report["dirty_check_includes_untracked"] is False
    pool_studies(paths, output)
    assert len(list((output.parent / "history").glob("pool-*.json"))) == 2
    with pytest.raises(ValueError, match="overwrite"):
        pool_studies(paths, paths[0])
    assert not output.with_name("pool.json.partial").exists()


def test_bool_and_non_finite_values_are_not_numbers():
    effect = {"rr": True, "lower": 0.9, "upper": 1.1}
    reports = [_report("11001", [{"outcome": "asthma", "age_group": "all", "effect": effect}])]
    (stratum,) = pool_reports(reports, effects=("effect",))["results"]
    assert stratum["effect"]["rejected"] == 1
    nan_condition = _effect(1.01, 0.01, float("nan"))
    reports = [
        _report("11001", [{"outcome": "asthma", "age_group": "all", "effect": nan_condition}])
    ]
    (stratum,) = pool_reports(reports, effects=("effect",))["results"]
    assert "max_covariance_condition" not in stratum["effect"]


@pytest.mark.parametrize("content", ["{", "[]", '{"region": "11001"}', '{"region": ["x"]}'])
def test_cli_rejects_invalid_reports(tmp_path, monkeypatch, content):
    import sys

    from folego.cli import main

    path = tmp_path / "bad.json"
    path.write_text(content)
    monkeypatch.setattr(
        sys, "argv", ["folego", "pool", str(path), "--output", str(tmp_path / "o.json")]
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_cli_rejects_missing_report(tmp_path, monkeypatch):
    import sys

    from folego.cli import main

    argv = ["folego", "pool", str(tmp_path / "none.json"), "--output", str(tmp_path / "o.json")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_cli_rejects_unreadable_path(tmp_path, monkeypatch, capsys):
    import sys

    from folego.cli import main

    argv = ["folego", "pool", str(tmp_path / ("x" * 300)), "--output", str(tmp_path / "o.json")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    # Python 3.14 is_file() returns False instead of raising ENAMETOOLONG.
    assert any(m in capsys.readouterr().err for m in ("E/S", "inexistente"))


def test_cli_reports_write_failure(tmp_path, monkeypatch, capsys):
    import errno
    import sys

    from folego import cli
    from folego.evaluate import pool

    report = tmp_path / "r.json"
    report.write_text("{}")

    def denied(*_):
        raise OSError(errno.EACCES, "Permission denied", str(tmp_path / "o.json"))

    monkeypatch.setattr(pool, "pool_studies", denied)
    argv = ["folego", "pool", str(report), "--output", str(tmp_path / "o.json")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    assert "Permission denied" in capsys.readouterr().err


def test_failed_write_leaves_no_partial(tmp_path, monkeypatch):
    from folego.ingest import common

    target = tmp_path / "out.json"
    target.write_text("old")

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(common.os, "replace", broken)
    with pytest.raises(OSError):
        common.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_does_not_follow_planted_symlink(tmp_path):
    from folego.ingest.common import atomic_write_text

    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "out.json.partial").symlink_to(victim)
    atomic_write_text(tmp_path / "out.json", "new")
    assert victim.read_text() == "keep"
    assert (tmp_path / "out.json").read_text() == "new"


@pytest.mark.parametrize(
    "panel", [[], [{"path": "p.parquet"}], [{"sha256": None}], [{"sha256": "A" * 64}]]
)
def test_missing_or_invalid_panel_hash_is_refused(panel):
    report = _report("11001", [], inputs={"panel": panel})
    with pytest.raises(ValueError, match="panel sha256"):
        pool_reports([report])
