import json
from datetime import date

import polars as pl
import pytest

from folego.ingest.common import sha256_file
from folego.ingest.sih import SCHEMA, STATES
from folego.ingest.sih_national import partition_path
from folego.transform.codes import age_group, diagnosis_group
from folego.transform.national_outcomes import (
    build_national_outcomes,
    certified_window,
    classified,
)


def write_partition(root, state, year, month, rows=()):
    path = partition_path(root, state, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        [{**r, "ANO_CMPT": str(year), "MES_CMPT": f"{month:02d}"} for r in rows] or None,
        schema=SCHEMA,
    )
    frame.write_parquet(path)
    path.with_suffix(".source.json").write_text(
        json.dumps(
            {"state": state, "year": year, "month": month, "parquet_sha256": sha256_file(path)}
        )
    )


def aih(n, diagnosis, admitted, *, residence="110020", value=100.0, ident="1", age=70):
    return {
        "N_AIH": f"{n:013d}",
        "IDENT": ident,
        "MUNIC_RES": residence,
        "MUNIC_MOV": residence,
        "DT_INTER": admitted,
        "DT_SAIDA": admitted,
        "DIAG_PRINC": diagnosis,
        "COD_IDADE": "4",
        "IDADE": age,
        "VAL_TOT": value,
    }


def test_vectorised_codes_match_the_reference_functions():
    diagnoses = [
        "J450",
        "J46",
        "J18.9",
        "J120",
        "J00",
        "I21",
        "S720",
        "T98",
        "T99",
        "A09",
        "",
        "x1",
    ]
    ages = [
        (1, 5),
        (3, 11),
        (4, 4),
        (4, 5),
        (4, 14),
        (4, 59),
        (4, 60),
        (5, 3),
        (4, 120),
        (6, 1),
        (4, -1),
    ]
    rows = [
        {"DIAG_PRINC": d, "COD_IDADE": str(u), "IDADE": v, "MUNIC_RES": "110020"}
        for d in diagnoses
        for u, v in ages
    ]
    frame = pl.DataFrame(rows).with_columns(
        pl.lit("20190101").alias("DT_INTER"),
        pl.lit("2019").alias("ANO_CMPT"),
        pl.lit("01").alias("MES_CMPT"),
    )
    result = classified(frame.lazy()).collect()
    for row, got in zip(rows, result.iter_rows(named=True), strict=True):
        assert got["outcome"] == diagnosis_group(row["DIAG_PRINC"]) or (
            diagnosis_group(row["DIAG_PRINC"]) is None and got["outcome"] is None
        ), row
        assert got["age_group"] == age_group(int(row["COD_IDADE"]), row["IDADE"]), row


def test_window_needs_every_state_and_drops_immature_months(tmp_path):
    root = tmp_path / "data"
    for month in range(1, 10):
        for state in STATES:
            if (month, state) != (9, "SP"):
                write_partition(root, state, 2019, month)
    window = certified_window(root, maturity_months=3)
    assert window["competences"][-1] == "2019-08"
    assert window["start"] == date(2019, 1, 1)
    assert window["end"] == date(2019, 5, 31)
    assert window["incomplete_competences_after_run"] == ["2019-09"]


@pytest.fixture
def lookup_and_ipca(tmp_path):
    lookup = tmp_path / "lookup.parquet"
    pl.DataFrame(
        {
            "municipality_code": ["110020", "355030", "530010"],
            "health_region_code": ["11004", "35016", "53001"],
        }
    ).write_parquet(lookup)
    ipca = tmp_path / "ipca.parquet"
    pl.DataFrame(
        {
            "month": pl.date_range(date(2019, 1, 1), date(2019, 12, 1), "1mo", eager=True),
            "ipca_index": [100.0] * 11 + [200.0],
        }
    ).write_parquet(ipca)
    return lookup, ipca


def test_build_deduplicates_classifies_and_deflates(tmp_path, lookup_and_ipca):
    root = tmp_path / "data"
    lookup, ipca = lookup_and_ipca
    for month in range(1, 5):
        for state in STATES:
            write_partition(root, state, 2019, month)
    write_partition(
        root,
        "RO",
        2019,
        1,
        [
            aih(1, "J450", "20190110"),
            aih(2, "I21", "20190111", age=30),
            aih(3, "J180", "20181231"),  # before the window
            aih(4, "J00", "20190112", ident="5"),
            aih(5, "J00", "20190112", residence="999999"),
            *(aih(100 + n, "S720", "20190115") for n in range(1000)),
            aih(6, "J00", "20190113", value=10.0),
            aih(7, "J46", "20190114", residence="530040"),
        ],
    )
    write_partition(
        root,
        "RO",
        2019,
        2,
        [
            aih(1, "J450", "20190110"),  # identical repeat
            aih(6, "J00", "20190113", value=99.0),  # conflicting content
        ],
    )
    output = tmp_path / "out" / "outcomes.parquet"
    counts = build_national_outcomes(root, lookup, ipca, output, maturity_months=1)
    got = {
        (r["outcome"], r["age_group"]): (r["admissions"], r["payment"]) for r in counts.to_dicts()
    }
    assert got.pop(("negative_control", "60+"))[0] == 1000
    assert got == {
        ("asthma", "60+"): (1, 200.0),
        ("respiratory", "60+"): (1, 200.0),
        ("circulatory", "15-59"): (1, 200.0),
    }
    brasilia = counts.filter(pl.col("health_region_code") == "53001")
    assert brasilia["outcome"].sort().to_list() == ["asthma", "respiratory"]
    audit = json.loads(output.with_suffix(".parquet.source.json").read_text())["reconciliation"]
    assert audit["identical_duplicate_rows_removed"] == 1
    assert audit["conflicting_identifiers"] == 1
    assert audit["excluded_continuations"] == 1
    assert audit["outside_admission_window"] == 1
    assert audit["unmapped_residence"] == 1
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert manifest["classification"].startswith("private")
    assert len(manifest["partitions_sha256"]) == 64 and manifest["software"]["polars"]


def daily(start, days, regions, **values):
    dates = pl.date_range(
        start, start + __import__("datetime").timedelta(days=days - 1), eager=True
    )
    frame = pl.DataFrame({"date": dates}).join(
        pl.DataFrame({"health_region_code": regions}), how="cross"
    )
    return frame.with_columns(**{k: pl.lit(v) for k, v in values.items()})


def test_panel_zero_fills_only_inside_the_admission_window(tmp_path):
    from folego.transform.cams_exposure import write_with_manifest
    from folego.transform.panel import build_panel

    regions = ["11004", "35016"]
    outcomes = tmp_path / "outcomes.parquet"
    write_with_manifest(
        outcomes,
        pl.DataFrame(
            {
                "date": [date(2019, 1, 2)],
                "health_region_code": ["11004"],
                "age_group": ["60+"],
                "outcome": ["asthma"],
                "admissions": [2],
                "payment": [300.0],
            }
        ).write_parquet,
        {"admission_window": ["2019-01-01", "2019-01-03"], "payment": "IPCA"},
    )
    paths = {}
    for name, frame in {
        "pm25": daily(date(2018, 12, 30), 10, regions, pm25=10.0),
        "meteo": daily(date(2019, 1, 1), 5, regions, temperature=25.0, humidity=80.0),
        "calendar": daily(date(2018, 1, 1), 800, regions, holiday=0),
    }.items():
        paths[name] = tmp_path / f"{name}.parquet"
        frame.write_parquet(paths[name])
    output = tmp_path / "panel.parquet"
    manifest = build_panel(
        outcomes, paths["pm25"], paths["meteo"], paths["calendar"], output, regions=["11004"]
    )
    panel = pl.read_parquet(output)
    assert manifest["dates"] == ["2019-01-01", "2019-01-05"]
    assert panel.height == manifest["rows"] == 5 * 20
    cell = panel.filter((pl.col("outcome") == "asthma") & (pl.col("age_group") == "60+"))
    assert cell["admissions"].to_list() == [0, 2, 0, None, None]
    assert cell["mean_payment"].to_list() == [None, 150.0, None, None, None]
    assert set(panel["health_region_code"]) == {"11004"}
    srag = tmp_path / "srag.parquet"
    daily(date(2018, 12, 20), 30, regions, viral=2).with_columns(
        # A spike on 2 Jan must only reach the following seven days.
        pl.when(pl.col("date") == date(2019, 1, 2)).then(100).otherwise(2).alias("viral")
    ).write_parquet(srag)
    build_panel(
        outcomes,
        paths["pm25"],
        paths["meteo"],
        paths["calendar"],
        output,
        regions=["11004"],
        srag_path=srag,
    )
    import math

    by_day = dict(pl.read_parquet(output).select("date", "srag").unique().sort("date").iter_rows())
    assert by_day[date(2019, 1, 2)] == math.log1p(14)
    assert by_day[date(2019, 1, 3)] == math.log1p(112)
    merra = tmp_path / "merra.parquet"
    daily(date(2019, 1, 2), 10, regions, pm25_merra2=9.0).write_parquet(merra)
    fires = tmp_path / "fires.parquet"
    daily(date(2018, 12, 1), 60, regions, fire_count=3, frp=1.5).write_parquet(fires)
    manifest = build_panel(
        outcomes,
        paths["pm25"],
        paths["meteo"],
        paths["calendar"],
        output,
        regions=["11004"],
        merra2_path=merra,
        fires_path=fires,
    )
    panel = pl.read_parquet(output)
    # MERRA-2 starts on 2 Jan; the main span still starts on 1 Jan, with a null there.
    assert manifest["dates"][0] == "2019-01-01"
    by_day = dict(panel.select("date", "pm25_merra2").unique().iter_rows())
    assert by_day[date(2019, 1, 1)] is None and by_day[date(2019, 1, 2)] == 9.0
    assert set(panel["fire_count"]) == {3}
    assert manifest["pm25_source_column"] == "pm25"
    # A MERRA-2 mart used as the main exposure becomes the panel's pm25 column.
    main = tmp_path / "merra-main.parquet"
    daily(date(2018, 12, 1), 60, regions, pm25_merra2=7.0).write_parquet(main)
    manifest = build_panel(
        outcomes, main, paths["meteo"], paths["calendar"], output, regions=["11004"]
    )
    assert manifest["pm25_source_column"] == "pm25_merra2"
    assert set(pl.read_parquet(output)["pm25"]) == {7.0}


def test_panel_refuses_gaps_in_an_exposure(tmp_path):
    from folego.transform.cams_exposure import write_with_manifest
    from folego.transform.panel import build_panel

    outcomes = tmp_path / "outcomes.parquet"
    write_with_manifest(
        outcomes,
        pl.DataFrame(
            schema={
                "date": pl.Date,
                "health_region_code": pl.String,
                "age_group": pl.String,
                "outcome": pl.String,
                "admissions": pl.Int64,
                "payment": pl.Float64,
            }
        ).write_parquet,
        {"admission_window": ["2019-01-01", "2019-01-03"], "payment": "IPCA"},
    )
    pm25 = daily(date(2019, 1, 1), 5, ["11004"], pm25=1.0).filter(
        pl.col("date") != date(2019, 1, 3)
    )
    paths = {}
    for name, frame in {
        "pm25": pm25,
        "meteo": daily(date(2019, 1, 1), 5, ["11004"], temperature=25.0, humidity=80.0),
        "calendar": daily(date(2019, 1, 1), 5, ["11004"], holiday=0),
    }.items():
        paths[name] = tmp_path / f"{name}.parquet"
        frame.write_parquet(paths[name])
    with pytest.raises(ValueError, match="missing days"):
        build_panel(
            outcomes, paths["pm25"], paths["meteo"], paths["calendar"], tmp_path / "p.parquet"
        )


def test_tabnet_national_counts_residence_state_from_all_files(tmp_path, monkeypatch):
    from folego.evaluate import tabnet_national

    root = tmp_path / "data"
    for month in range(1, 13):
        for state in STATES:
            write_partition(root, state, 2019, month)
    write_partition(
        root,
        "SP",
        2019,
        3,
        [
            aih(1, "J450", "20190301", residence="355030"),
            aih(2, "J00", "20190301", residence="355030", ident="5"),
            aih(3, "I21", "20190301", residence="355030"),
            aih(5, "J00", "20190301", residence="355030"),
            aih(6, "J00", "20190301"),  # RO resident treated in SP
        ],
    )
    write_partition(root, "RO", 2019, 3, [aih(4, "J18", "20190301", residence="355030")])
    lookup = tmp_path / "lookup.parquet"
    pl.DataFrame({"state": ["RO", "SP"], "ibge_code": ["1100205", "3550308"]}).write_parquet(lookup)
    reference = pl.DataFrame({"month": range(1, 13), "admissions": [0, 0, 3] + [0] * 9})
    monkeypatch.setattr(
        tabnet_national,
        "retrieve_reference",
        lambda year, state, codes, root: (
            reference,
            {"url": "u", "retrieved_at": "t", "sha256": "s"},
        ),
    )
    report = tabnet_national.validate_national(root, lookup, [("SP", 2019), ("SP", 2020)])
    assert report["checks"][0]["accepted"] is True
    assert report["checks"][0]["total_observed"] == 3
    assert report["checks"][1]["status"] == "not_compared"
    assert report["accepted"] is False


def test_many_unmapped_residences_fail(tmp_path, lookup_and_ipca):
    root = tmp_path / "data"
    lookup, ipca = lookup_and_ipca
    for month in range(1, 3):
        for state in STATES:
            write_partition(root, state, 2019, month)
    write_partition(root, "RO", 2019, 1, [aih(1, "J00", "20190110", residence="999999")])
    with pytest.raises(ValueError, match="unmapped residence"):
        build_national_outcomes(root, lookup, ipca, tmp_path / "o.parquet", maturity_months=1)


def test_statewide_tabnet_query_selects_every_municipality():
    from folego.ingest.tabnet import TabnetForm, build_query

    fields = {
        "Linha": [("Ano/mês_processamento", "")],
        "Coluna": [("--Não-Ativa--", "")],
        "Incremento": [("Internações", "")],
        "Arquivos": [(f"nrsp16{m:02d}.dbf", "") for m in range(1, 13)],
        "SMunicípio": [("TODAS_AS_CATEGORIAS__", "Todas"), ("1", "355030 SAO PAULO")],
        "SCapítulo_CID-10": [("10", "X. Doenças do aparelho respiratório")],
    }
    form = TabnetForm()
    form.feed(
        "".join(
            f'<select name="{n}">'
            + "".join(f'<option value="{v}">{t}</option>' for v, t in o)
            + "</select>"
            for n, o in fields.items()
        )
    )
    query = build_query(form, 2016, None, "SP")
    assert [v for k, v in query if k == "SMunicípio"] == ["TODAS_AS_CATEGORIAS__"]


def test_codes_with_letters_after_digits_and_null_age_are_not_classified():
    frame = pl.DataFrame(
        {
            "DIAG_PRINC": ["J18X", "J18"],
            "COD_IDADE": ["2", "2"],
            "IDADE": [None, 3],
            "MUNIC_RES": ["530140", "5301402"],
            "DT_INTER": ["20190101"] * 2,
            "ANO_CMPT": ["2019"] * 2,
            "MES_CMPT": ["01"] * 2,
        },
        schema_overrides={"IDADE": pl.Int64},
    )
    result = classified(frame.lazy()).collect()
    assert result["outcome"].to_list() == [None, "pneumonia"]
    assert result["age_group"].to_list() == [None, "0-4"]
    assert result["municipality_code"].to_list() == ["530010", "530010"]


def test_a_gap_before_later_complete_competences_fails(tmp_path):
    root = tmp_path / "data"
    for month in range(1, 6):
        for state in STATES:
            if (month, state) != (2, "RJ"):
                write_partition(root, state, 2019, month)
    with pytest.raises(ValueError, match="re-run sih-national"):
        certified_window(root, maturity_months=1)


def test_panel_fails_when_outcome_regions_differ(tmp_path):
    from folego.transform.cams_exposure import write_with_manifest
    from folego.transform.panel import build_panel

    outcomes = tmp_path / "outcomes.parquet"
    write_with_manifest(
        outcomes,
        pl.DataFrame(
            {
                "date": [date(2019, 1, 2)],
                "health_region_code": ["99999"],
                "age_group": ["60+"],
                "outcome": ["asthma"],
                "admissions": [1],
                "payment": [1.0],
            }
        ).write_parquet,
        {"admission_window": ["2019-01-01", "2019-01-03"], "payment": "IPCA"},
    )
    paths = {}
    for name, frame in {
        "pm25": daily(date(2019, 1, 1), 5, ["11004"], pm25=1.0),
        "meteo": daily(date(2019, 1, 1), 5, ["11004"], temperature=25.0, humidity=80.0),
        "calendar": daily(date(2019, 1, 1), 5, ["11004"]).with_columns(
            pl.lit(0, dtype=pl.Int64).alias("holiday")
        ),
    }.items():
        paths[name] = tmp_path / f"{name}.parquet"
        frame.write_parquet(paths[name])
    with pytest.raises(ValueError, match="no admissions at all"):
        build_panel(
            outcomes, paths["pm25"], paths["meteo"], paths["calendar"], tmp_path / "p.parquet"
        )


def test_maturity_profile_counts_records_by_processing_lag(tmp_path):
    from folego.transform.national_outcomes import maturity_profile

    root = tmp_path / "data"
    for month in range(1, 13):
        for state in STATES:
            write_partition(root, state, 2019, month)
    # Two January admissions: one processed in January, one in March.
    write_partition(root, "RO", 2019, 1, [aih(1, "J00", "20190115")])
    write_partition(root, "RO", 2019, 3, [aih(2, "J00", "20190120"), aih(3, "I21", "20190301")])
    report = maturity_profile(root, max_lag=3)
    assert report["admission_months"] == ["2019-01", "2019-09"]
    assert report["by_chapter"]["J"] == [0.5, 0.5, 1.0, 1.0]
    assert report["cumulative_share"][0] == pytest.approx(2 / 3, abs=1e-4)
