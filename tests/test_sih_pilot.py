"""Reference-reader parity and processing/admission-time separation."""

import struct

import polars as pl
import pytest


def make_dbf(path, rows):
    fields = [
        ("N_AIH", "C", 13, 0),
        ("MUNIC_RES", "C", 6, 0),
        ("VAL_TOT", "N", 14, 2),
        ("PRIVATE", "C", 20, 0),
    ]
    length = 1 + sum(f[2] for f in fields)
    header = bytearray(32)
    header[0] = 3
    struct.pack_into("<IHH", header, 4, len(rows), 32 + 32 * len(fields) + 1, length)
    result = bytes(header)
    for name, kind, size, decimals in fields:
        field = bytearray(32)
        field[: len(name)] = name.encode()
        field[11] = ord(kind)
        field[16] = size
        field[17] = decimals
        result += bytes(field)
    result += b"\r"
    for row in rows:
        result += b"*" if row.get("deleted") else b" "
        for name, kind, size, _decimals in fields:
            value = str(row[name]).encode("latin-1")
            result += value.rjust(size) if kind == "N" else value.ljust(size)
    path.write_bytes(result + b"\x1a")


def test_projected_dbf_matches_reference_and_never_retains_private_fields(tmp_path):
    dbfread = pytest.importorskip("dbfread")
    from folego.ingest.dbf import projected_dbf_rows

    p = tmp_path / "data.dbf"
    make_dbf(
        p,
        [
            {
                "N_AIH": "0000000000001",
                "MUNIC_RES": "110020",
                "VAL_TOT": "123.45",
                "PRIVATE": "sintético",
            },
            {
                "N_AIH": "0000000000002",
                "MUNIC_RES": "355030",
                "VAL_TOT": "99.00",
                "PRIVATE": "exemplo",
            },
            {
                "N_AIH": "0000000000003",
                "MUNIC_RES": "110020",
                "VAL_TOT": "88.00",
                "PRIVATE": "excluído",
                "deleted": True,
            },
        ],
    )
    fields = ["N_AIH", "MUNIC_RES", "VAL_TOT"]
    stats = {}
    actual = list(projected_dbf_rows(p, fields, {"110020"}, stats=stats))
    expected = [
        {k: r[k] for k in fields}
        for r in dbfread.DBF(str(p), encoding="latin-1")
        if r["MUNIC_RES"] == "110020"
    ]
    assert actual == expected
    assert stats == {"scanned_records": 3, "deleted_records": 1, "selected_records": 1}
    assert all("PRIVATE" not in r for r in actual)


def test_truncated_dbf_and_missing_fields_fail(tmp_path):
    pytest.importorskip("dbfread")
    from folego.ingest.dbf import projected_dbf_rows

    p = tmp_path / "data.dbf"
    make_dbf(
        p, [{"N_AIH": "0000000000001", "MUNIC_RES": "110020", "VAL_TOT": "1.00", "PRIVATE": "x"}]
    )
    with pytest.raises(ValueError, match="Missing"):
        list(projected_dbf_rows(p, ["NOT_PRESENT"], {"110020"}))
    p.write_bytes(p.read_bytes()[:-12])
    with pytest.raises(ValueError, match="Truncated"):
        list(projected_dbf_rows(p, ["MUNIC_RES"], {"110020"}))


def sample_episodes():
    return pl.DataFrame(
        {
            "N_AIH": [
                "0000000000001",
                "0000000000001",
                "0000000000002",
                "0000000000002",
                "0000000000003",
                "0000000000004",
            ],
            "MUNIC_RES": ["110020"] * 6,
            "DT_INTER": ["20181230", "20181230", "20190102", "20190102", "20190103", "20190104"],
            "DT_SAIDA": ["20190101"] * 6,
            "DIAG_PRINC": ["J450"] * 6,
            "COD_IDADE": ["4"] * 6,
            "IDADE": [70] * 6,
            "VAL_TOT": [100.0, 100.0, 200.0, 250.0, 90.0, 80.0],
            "IDENT": ["1", "1", "1", "1", "5", "1"],
            "ANO_CMPT": ["2019"] * 6,
            "MES_CMPT": ["01", "02", "01", "02", "01", "01"],
            "MUNIC_MOV": ["110020"] * 5 + ["355030"],
            "source_state": ["RO"] * 5 + ["SP"],
            "source_month": [1, 2, 1, 2, 1, 1],
        }
    )


def test_reconciliation_keeps_identical_repeats_and_quarantines_conflicts():
    from folego.pilot import reconcile_episodes

    episodes, audit = reconcile_episodes(sample_episodes())
    assert set(episodes["N_AIH"]) == {"0000000000001", "0000000000004"}
    assert audit["excluded_continuations"] == 1
    assert audit["identical_duplicate_rows_removed"] == 1
    assert audit["conflicting_identifiers"] == 1
    assert audit["conflicting_rows_quarantined"] == 2


def test_monthly_tabnet_comparison_uses_processing_rows_before_episode_deduplication():
    from folego.pilot import monthly_processing_counts

    result = monthly_processing_counts(sample_episodes(), 2019)
    assert result.height == 12
    assert result.filter(pl.col("month") == 1)["admissions"].item() == 3
    assert result.filter(pl.col("month") == 2)["admissions"].item() == 2
    # The first record belongs to 2018 by admission but 2019 by processing.
    assert result["admissions"].sum() == 5


def test_tabnet_gate_requires_all_months_and_relative_error_below_one_percent():
    from folego.ingest.tabnet import compare_monthly_counts

    observed = pl.DataFrame({"month": list(range(1, 13)), "admissions": [100] * 12})
    assert compare_monthly_counts(observed, observed)["accepted"] is True
    bad = observed.with_columns(
        pl.when(pl.col("month") == 1).then(90).otherwise(pl.col("admissions")).alias("admissions")
    )
    assert compare_monthly_counts(bad, observed)["accepted"] is False
    with pytest.raises(ValueError, match="12"):
        compare_monthly_counts(observed.head(11), observed)


def test_tabnet_parser_does_not_treat_unavailable_values_as_zero():
    from folego.ingest.tabnet import parse_monthly_counts

    names = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    page = (
        "<table>" + "".join(f"<tr><td>2019/{m}</td><td>1.234</td></tr>" for m in names) + "</table>"
    )
    assert parse_monthly_counts(page.encode("latin-1"), 2019)["admissions"].sum() == 12 * 1234
    with pytest.raises(ValueError, match="Invalid"):
        parse_monthly_counts(page.replace("1.234", "...", 1).encode("latin-1"), 2019)
    assert (
        parse_monthly_counts(page.replace("1.234", "-", 1).encode("latin-1"), 2019)["admissions"][0]
        == 0
    )


def test_tabnet_legacy_implicit_cells_and_full_month_names():
    from folego.ingest.tabnet import parse_monthly_counts

    names = [
        "Janeiro",
        "Fevereiro",
        "Março",
        "Abril",
        "Maio",
        "Junho",
        "Julho",
        "Agosto",
        "Setembro",
        "Outubro",
        "Novembro",
        "Dezembro",
    ]
    page = (
        "<table><tbody><tr><td>2019<td>1200"
        + "".join(f"<tr><td>..{m}/2019<td>100" for m in names)
        + "</tbody></table>"
    )
    assert parse_monthly_counts(page.encode("latin-1"), 2019)["admissions"].sum() == 1200


def test_tabnet_response_echo_must_match_query_and_year():
    from folego.ingest.tabnet import validate_response_scope

    fields = [
        ("Linha", "Ano/mês_processamento"),
        ("Coluna", "--Não-Ativa--"),
        ("Incremento", "Internações"),
        ("SMunicípio", "18"),
        ("SCapítulo_CID-10", "10"),
    ]
    text = "<!--<TABELA>\nDef=sih/cnv/nrro.def\n" + "".join(f"{k}={v}\n" for k, v in fields)
    text += "Periodo=" + ",".join(f"19{m:02d}" for m in range(1, 13)) + "\n</TABELA>-->"
    validate_response_scope(text.encode("latin-1"), fields, "RO", 2019)
    with pytest.raises(ValueError, match="periods"):
        validate_response_scope(text.encode("latin-1"), fields, "RO", 2020)
    with pytest.raises(ValueError, match="scope"):
        validate_response_scope(
            text.replace("SMunicípio=18", "SMunicípio=19").encode("latin-1"), fields, "RO", 2019
        )


def test_form_discovery_does_not_hardcode_municipality_or_chapter_indexes():
    from folego.ingest.tabnet import TabnetForm, build_query

    fields = {
        "Linha": [("Ano/mês_processamento", "Ano/mês processamento")],
        "Coluna": [("--Não-Ativa--", "Não ativa")],
        "Incremento": [("Internações", "Internações")],
        "Arquivos": [(f"nrro19{m:02d}.dbf", str(m)) for m in range(1, 13)],
        "SMunicípio": [("TODAS_AS_CATEGORIAS__", "Todas"), ("345", "110020 PORTO VELHO")],
        "SCapítulo_CID-10": [
            ("TODAS_AS_CATEGORIAS__", "Todas"),
            ("99", "X. Doenças do aparelho respiratório"),
        ],
    }
    form = TabnetForm()
    form.feed(
        "".join(
            f'<select name="{name}">'
            + "".join(f'<option value="{value}">{label}</option>' for value, label in options)
            + "</select>"
            for name, options in fields.items()
        )
    )
    query = build_query(form, 2019, ["110020"], "RO")
    assert ("SMunicípio", "345") in query
    assert ("SCapítulo_CID-10", "99") in query


def test_regional_files_cannot_be_validated_as_national_residence_coverage(tmp_path):
    import json

    from folego.pilot import run_pilot

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {"schema_version": 1, "name": "pilot", "processing_year": 2019, "file_states": ["RO"]}
        )
    )
    with pytest.raises(ValueError, match="National file coverage"):
        run_pilot(p, root=tmp_path / "data", with_tabnet=True)


def test_published_manifest_covers_27_states_and_all_processing_months():
    import json
    from pathlib import Path

    from folego.ingest.sih import STATES

    report = json.loads(Path("docs/pilots/madeira-mamore-2019.json").read_text())
    sources = report["source_files"]
    assert len(sources) == 27 * 12
    assert {(r["state"], r["month"]) for r in sources} == {
        (s, m) for s in STATES for m in range(1, 13)
    }
    assert sum(r["source_bytes"] for r in sources) == report["source_bytes"]
    assert all(len(r["source_sha256"]) == 64 for r in sources)
    assert report["tabnet"]["accepted"] is True
    assert report["tabnet"]["total_observed"] == report["tabnet"]["total_reference"] == 2677
    assert sum(m["observed"] for m in report["tabnet"]["months"]) == 2677
    assert all(m["relative_error"] == 0 for m in report["tabnet"]["months"])
    assert report["model_ready"] is False and report["clinical_cohort_complete"] is False


def test_report_drops_private_partition_counts_and_suppresses_small_cells(tmp_path):
    import json
    from pathlib import Path

    from folego.publish.pilot_report import publish_pilot_report

    published = json.loads(Path("docs/pilots/madeira-mamore-2019.json").read_text())
    report = {
        **published,
        "source_manifest": [
            {**r, "selected_records": 1, "N_AIH": "DO_NOT_PUBLISH"}
            for r in published["source_files"]
        ],
        "reconciliation": {"conflicting_identifiers": 0},
        "invalid_admission_dates": 0,
        "invalid_age_units": 0,
    }
    report["normal_respiratory_outside_residence_state"] = 3
    report["tabnet"]["months"][0]["observed"] = 3
    output = tmp_path / "report.md"
    public = publish_pilot_report(report, output)
    assert public["normal_respiratory_outside_residence_state"] is None
    assert public["tabnet"]["months"] is None
    assert all("selected_records" not in r and "N_AIH" not in r for r in public["source_files"])
    assert "DO_NOT_PUBLISH" not in output.with_suffix(".json").read_text()
