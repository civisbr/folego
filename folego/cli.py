"""Explicit commands; no default action downloads historical national microdata."""

import argparse
import json
import os
from pathlib import Path


def study_suffix(meteorology: str, pm25: str, exposure: str, knots=None) -> str:
    """Report name suffix, so runs on different sources or bases never overwrite each other."""
    from .models.crossbasis import CrossBasis

    suffix = "" if exposure == "pm25" else f"-{exposure}"
    if meteorology == "MERRA-2":
        suffix += "-met_merra2"
    if pm25 == "MERRA-2" and exposure == "pm25":
        # Distinct from "-pm25_merra2", the MERRA-2 sensitivity run on a CAMS panel.
        suffix += "-pm25main_merra2"
    if "unknown" in (meteorology, pm25):
        suffix += "-unknown_source"
    if knots is not None and tuple(knots) != CrossBasis().knots:
        suffix += "-knots" + "_".join(f"{k:g}" for k in knots)
    return suffix


def main():
    parser = argparse.ArgumentParser(prog="folego")
    commands = parser.add_subparsers(dest="command", required=True)
    territory = commands.add_parser("territory", help="Atualizar correspondência nacional oficial")
    territory.add_argument("--allow-incomplete", action="store_true")
    territory.add_argument("--refresh", action="store_true")
    commands.add_parser("audit-territory", help="Auditar snapshot e disponibilidade das fontes")
    commands.add_parser("configure-ads", help="Salvar a chave ADS localmente com entrada oculta")
    commands.add_parser("demo", help="Regerar somente dados sintéticos do painel")
    commands.add_parser("geography", help="Gerar malha pública por região de saúde")
    sih = commands.add_parser("sih", help="Baixar uma competência SIH/SUS")
    sih.add_argument("--state", required=True)
    sih.add_argument("--year", type=int, required=True)
    sih.add_argument("--month", type=int, required=True)
    national = commands.add_parser(
        "sih-national", help="Extrair internações J/I/S/T de todo o país e apagar os DBC"
    )
    national.add_argument("--start", default="2012-01", help="Competência inicial AAAA-MM")
    national.add_argument("--end", required=True, help="Competência final AAAA-MM")
    national.add_argument("--states", nargs="+")
    national.add_argument("--workers", type=int, default=3)
    national.add_argument("--keep-source", action="store_true", help="Manter os DBC baixados")
    outcomes = commands.add_parser(
        "outcomes", help="Internações diárias por região a partir do SIH nacional"
    )
    outcomes.add_argument("--maturity-months", type=int, default=6)
    outcomes.add_argument(
        "--lookup", type=Path, default=Path("data/staging/municipalities.parquet")
    )
    outcomes.add_argument("--ipca", type=Path, default=Path("data/marts/ipca.parquet"))
    outcomes.add_argument(
        "--output", type=Path, default=Path("data/marts/outcomes/daily-admissions.parquet")
    )
    srag_cmd = commands.add_parser(
        "srag", help="SRAG hospitalizada (SIVEP-Gripe) por região e dia de início dos sintomas"
    )
    srag_cmd.add_argument("--start", type=int, default=2012)
    srag_cmd.add_argument("--end", type=int, default=2025)
    srag_cmd.add_argument(
        "--lookup", type=Path, default=Path("data/staging/municipalities.parquet")
    )
    srag_cmd.add_argument("--keep-source", action="store_true")
    srag_cmd.add_argument("--output", type=Path, default=Path("data/marts/srag/daily-srag.parquet"))
    study = commands.add_parser("study", help="Estudo de associação PM2,5 × internações por região")
    study.add_argument("--region", required=True, help="Código da região de saúde (5 dígitos)")
    study.add_argument("--panel", type=Path, default=Path("data/marts/panel/daily-panel.parquet"))
    study.add_argument("--output", type=Path, help="Padrão: data/marts/studies/<região>.json")
    study.add_argument(
        "--exposure", default="pm25", choices=["pm25", "pm25_merra2"], help="Coluna de PM2,5"
    )
    study.add_argument(
        "--knots",
        type=float,
        nargs="+",
        help="Nós da base de exposição em µg/m³ (padrão 15 35 75); iguais entre regiões"
        " para combinar estudos",
    )
    pool = commands.add_parser(
        "pool", help="Meta-análise (DerSimonian-Laird) de relatórios de estudo por região"
    )
    pool.add_argument("reports", type=Path, nargs="+", help="Relatórios JSON de `study`")
    pool.add_argument("--output", type=Path, required=True)
    fires = commands.add_parser("fires", help="Focos de queimada FIRMS VIIRS por região e dia")
    fires.add_argument("--start", default="2012-01-20")
    fires.add_argument("--end", default="2025-12-31")
    fires.add_argument("--regions", type=Path, default=Path("web/public/data/regions.geojson"))
    fires.add_argument(
        "--output", type=Path, default=Path("data/marts/exposure/fires-daily.parquet")
    )
    merra = commands.add_parser("merra2", help="MERRA-2 por região: PM2,5 ou temperatura/umidade")
    merra.add_argument("--start", default="2012-01")
    merra.add_argument("--end", default="2025-12")
    merra.add_argument("--regions", type=Path, default=Path("web/public/data/regions.geojson"))
    merra.add_argument(
        "--product",
        choices=["pm25", "weather"],
        default="pm25",
        help="pm25: aerossóis (sensibilidade); weather: temperatura e umidade a 2 m",
    )
    merra.add_argument("--output", type=Path, help="Padrão conforme --product")
    maturity = commands.add_parser(
        "maturity", help="Medir em quantos meses as internações chegam ao SIH"
    )
    maturity.add_argument(
        "--output", type=Path, default=Path("data/marts/validation/sih-maturity.json")
    )
    tabnet_check = commands.add_parser(
        "tabnet-national", help="Conferir o SIH nacional com o TabNet por UF de residência e ano"
    )
    tabnet_check.add_argument(
        "--checks", nargs="+", help="Pares UF:ANO, por exemplo SP:2016 (padrão: amostra fixa)"
    )
    tabnet_check.add_argument(
        "--lookup", type=Path, default=Path("data/staging/municipalities.parquet")
    )
    tabnet_check.add_argument(
        "--output", type=Path, default=Path("data/marts/validation/tabnet-national.json")
    )
    panel = commands.add_parser("panel", help="Montar o painel diário de pesquisa")
    panel.add_argument(
        "--outcomes", type=Path, default=Path("data/marts/outcomes/daily-admissions.parquet")
    )
    panel.add_argument("--pm25", type=Path, default=Path("data/marts/exposure/pm25-daily.parquet"))
    panel.add_argument(
        "--meteorology",
        type=Path,
        default=Path("data/marts/exposure/meteorology-daily.parquet"),
    )
    panel.add_argument("--calendar", type=Path, default=Path("data/marts/calendar.parquet"))
    panel.add_argument("--srag", type=Path, help="SRAG diária (folego srag) como confundidor")
    panel.add_argument("--merra2", type=Path, help="PM2,5 MERRA-2 (folego merra2), sensibilidade")
    panel.add_argument("--fires", type=Path, help="Focos FIRMS (folego fires)")
    panel.add_argument("--regions", nargs="+", help="Códigos de região de saúde (padrão: todas)")
    panel.add_argument("--output", type=Path, default=Path("data/marts/panel/daily-panel.parquet"))
    cams = commands.add_parser("cams", help="Executar requisição exportada do ADS")
    cams.add_argument("--dataset", required=True)
    cams.add_argument("--request", type=Path, required=True)
    exposure = commands.add_parser(
        "exposure", help="PM2,5 diário do CAMS ponderado pela população, por região de saúde"
    )
    exposure.add_argument("cams_file", type=Path)
    exposure.add_argument("--regions", type=Path, default=Path("web/public/data/regions.geojson"))
    exposure.add_argument(
        "--output", type=Path, default=Path("data/marts/exposure/pm25-daily.parquet")
    )
    meteorology = commands.add_parser(
        "meteorology", help="Temperatura e umidade diárias do ERA5 por região de saúde"
    )
    meteorology.add_argument("era5_files", type=Path, nargs="+")
    meteorology.add_argument(
        "--regions", type=Path, default=Path("web/public/data/regions.geojson")
    )
    meteorology.add_argument(
        "--output", type=Path, default=Path("data/marts/exposure/meteorology-daily.parquet")
    )
    ipca = commands.add_parser("ipca", help="Atualizar o número-índice mensal do IPCA (SIDRA)")
    ipca.add_argument("--refresh", action="store_true")
    ipca.add_argument("--output", type=Path, default=Path("data/marts/ipca.parquet"))
    calendar = commands.add_parser("calendar", help="Feriados nacionais e estaduais por região")
    calendar.add_argument("--start", required=True)
    calendar.add_argument("--end", required=True)
    calendar.add_argument("--regions", type=Path, default=Path("web/public/data/regions.geojson"))
    calendar.add_argument("--output", type=Path, default=Path("data/marts/calendar.parquet"))
    export = commands.add_parser("export", help="Exportar agregados com supressão de células")
    export.add_argument("source", type=Path)
    export.add_argument("--output", type=Path, default=Path("data/public"))
    forecast = commands.add_parser(
        "forecast", help="Previsão de pesquisa a partir de painéis privados preparados"
    )
    forecast.add_argument("--history", type=Path, required=True)
    forecast.add_argument("--future", type=Path, required=True)
    forecast.add_argument("--issued-on", required=True)
    forecast.add_argument("--output", type=Path, default=Path("data/marts/research-forecast.json"))
    pilot = commands.add_parser(
        "pilot", help="Piloto SIH real com conferência por competência no TabNet"
    )
    pilot.add_argument(
        "--config", type=Path, default=Path("configs/pilots/madeira-mamore-2019.json")
    )
    pilot.add_argument("--skip-tabnet", action="store_true")
    pilot.add_argument("--require-tabnet", action="store_true")
    pilot.add_argument("--publish-report", type=Path)
    args = parser.parse_args()
    if args.command == "configure-ads":
        from .ingest.ads_setup import configure_ads_interactively

        try:
            configure_ads_interactively()
        except (FileExistsError, ValueError) as error:
            parser.exit(1, str(error) + "\n")
    elif args.command == "pilot":
        from .pilot import run_pilot
        from .publish.pilot_report import publish_pilot_report

        report = run_pilot(args.config, with_tabnet=not args.skip_tabnet)
        if args.publish_report:
            publish_pilot_report(report, args.publish_report)
        if args.require_tabnet and not report["tabnet"].get("accepted", False):
            parser.exit(1, "A conferência TabNet não foi aprovada; consulte o relatório local.\n")
        summary = {
            key: report[key]
            for key in [
                "name",
                "files_processed",
                "normal_respiratory_processing_records",
                "model_ready",
            ]
        }
        summary["tabnet"] = {
            key: report["tabnet"].get(key)
            for key in [
                "status",
                "accepted",
                "total_observed",
                "total_reference",
                "annual_relative_error",
            ]
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "audit-territory":
        from .ingest.audit import audit_territory

        print(json.dumps(audit_territory(), ensure_ascii=False, indent=2))
    elif args.command == "forecast":
        from datetime import date

        from .pipeline import forecast_files

        forecast_files(args.history, args.future, date.fromisoformat(args.issued_on), args.output)
    elif args.command == "territory":
        from .ingest.ibge import build_correspondence

        _, report = build_correspondence(
            refresh=args.refresh, require_complete=not args.allow_incomplete
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "geography":
        import polars as pl

        from .ingest.ibge import download_boundaries
        from .publish.geography import export_geography

        result = export_geography(
            download_boundaries(),
            pl.read_parquet("data/staging/municipalities.parquet"),
            Path("web/public/data/regions.geojson"),
        )
        print(f"{len(result)} regiões exportadas")
    elif args.command == "demo":
        from .publish.demo import build_demo

        print(f"{build_demo()} linhas sintéticas; nenhum dado de saúde observado")
    elif args.command == "outcomes":
        from .transform.national_outcomes import build_national_outcomes

        if not 1 <= args.maturity_months <= 24:
            parser.exit(2, "--maturity-months deve ficar entre 1 e 24.\n")
        os.umask(0o077)
        build_national_outcomes(
            Path("data"),
            args.lookup,
            args.ipca,
            args.output,
            maturity_months=args.maturity_months,
        )
        manifest = json.loads(args.output.with_suffix(".parquet.source.json").read_text())
        print(
            json.dumps(
                {k: manifest[k] for k in ("rows", "admission_window", "reconciliation")},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "srag":
        from datetime import date

        import polars as pl

        from .ingest.srag import build_srag

        # The current year is still being notified; its zeros would not be real.
        if not 2009 <= args.start <= args.end < date.today().year:
            parser.exit(2, "Use 2009 <= início <= fim <= ano passado.\n")
        os.umask(0o077)
        series = build_srag(
            args.start, args.end, args.lookup, args.output, keep_source=args.keep_source
        )
        print(
            json.dumps(
                {
                    "rows": series.height,
                    "viral_by_year": {
                        str(r["year"]): r["viral"]
                        for r in series.group_by(pl.col("date").dt.year().alias("year"))
                        .agg(pl.col("viral").sum())
                        .sort("year")
                        .to_dicts()
                    },
                },
                indent=2,
            )
        )
    elif args.command == "study":
        import re

        from .evaluate.study import input_source, meteorology_source, region_study

        if not re.fullmatch(r"\d{5}", args.region):
            parser.exit(2, "Código de região de saúde tem 5 dígitos.\n")
        os.umask(0o077)
        basis = None
        if args.knots is not None:
            from .models.crossbasis import CrossBasis

            # Rounded so the report name (and the design) cannot hide near-duplicate knots.
            knots = tuple(sorted({round(k, 1) for k in args.knots}))
            if (
                not all(0 < k < 1000 for k in knots)
                or CrossBasis().reference not in knots
                or len(knots) > 8
                or any(b - a < 1 for a, b in zip(knots, knots[1:], strict=False))
            ):
                parser.exit(
                    2,
                    "Até 8 nós entre 0 e 1000 µg/m³, a 1 µg/m³ ou mais um do outro,"
                    " incluindo a referência 15.\n",
                )
            basis = CrossBasis(knots=knots)
        suffix = study_suffix(
            meteorology_source(args.panel),
            input_source(args.panel, "pm25"),
            args.exposure,
            None if basis is None else basis.knots,
        )
        output = args.output or Path("data/marts/studies") / f"{args.region}{suffix}.json"
        report = region_study(args.panel, args.region, output, exposure=args.exposure, basis=basis)
        summary = [
            {
                "outcome": r["outcome"],
                "age_group": r["age_group"],
                **(
                    {
                        k: None if v is None else round(v, 3)
                        for k, v in r["effect"].items()
                        if k in ("rr", "lower", "upper")
                    }
                    if "effect" in r
                    else {"error": r.get("error") or r.get("effect_error")}
                ),
            }
            for r in report["results"]
        ]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            f"Controle negativo sinalizado: {report['negative_control_flag']}; relatório: {output}"
        )
    elif args.command == "fires":
        from datetime import date

        from .ingest.firms_archive import build_fires

        try:
            start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        except ValueError:
            parser.exit(2, "Use datas AAAA-MM-DD.\n")
        if not date(2012, 1, 20) <= start < end < date.today():
            parser.exit(2, "Use 2012-01-20 <= início < fim < hoje (arquivo VIIRS SNPP).\n")
        os.umask(0o077)
        series = build_fires(start, end, args.regions, args.output)
        print(json.dumps({"rows": series.height, "fires": int(series["fire_count"].sum())}))
    elif args.command == "merra2":
        from datetime import date

        from .ingest.merra2 import build_merra2, build_merra2_weather
        from .ingest.worldpop import download_worldpop

        try:
            start, end = (date.fromisoformat(f"{v}-01") for v in (args.start, args.end))
        except ValueError:
            parser.exit(2, "Use meses AAAA-MM.\n")
        if not date(2012, 1, 1) <= start <= end < date.today().replace(day=1):
            parser.exit(2, "Use 2012-01 <= início <= fim < mês atual.\n")
        os.umask(0o077)
        build, default = {
            "pm25": (build_merra2, "pm25-merra2-daily.parquet"),
            "weather": (build_merra2_weather, "meteorology-merra2-daily.parquet"),
        }[args.product]
        output = args.output or Path("data/marts/exposure") / default
        series = build(start, end, download_worldpop(), args.regions, output)
        print(
            json.dumps(
                {
                    "rows": series.height,
                    "dates": [str(series["date"].min()), str(series["date"].max())],
                }
            )
        )
    elif args.command == "pool":
        from .evaluate.pool import pool_studies

        missing = [str(p) for p in args.reports if not p.is_file()]
        if missing:
            parser.exit(2, f"Relatório inexistente: {missing[0]}\n")
        os.umask(0o077)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = pool_studies(args.reports, args.output)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            parser.exit(2, f"Relatórios incompatíveis ou inválidos: {error}\n")
        print(
            json.dumps(
                [
                    {
                        "outcome": r["outcome"],
                        "age_group": r["age_group"],
                        **{
                            k: round(r["effect"][k], 3)
                            for k in ("rr", "lower", "upper")
                            if k in r["effect"]
                        },
                        "regions": r["effect"]["regions"],
                    }
                    for r in report["results"]
                ],
                ensure_ascii=False,
            )
        )
    elif args.command == "maturity":
        from .transform.national_outcomes import maturity_profile

        os.umask(0o077)
        report = maturity_profile(Path("data"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: report[k] for k in ("admission_months", "cumulative_share")}))
    elif args.command == "tabnet-national":
        import re

        from .evaluate.tabnet_national import DEFAULT_CHECKS, validate_national

        checks = DEFAULT_CHECKS
        if args.checks:
            from .ingest.sih import STATES

            if not all(re.fullmatch(r"[A-Za-z]{2}:20\d{2}", c) for c in args.checks):
                parser.exit(2, "Use pares UF:ANO, por exemplo SP:2016.\n")
            checks = [(c[:2].upper(), int(c[3:])) for c in args.checks]
            if not {state for state, _ in checks} <= set(STATES):
                parser.exit(2, f"UF inválida; use: {' '.join(STATES)}\n")
        os.umask(0o077)
        report = validate_national(Path("data"), args.lookup, checks, output=args.output)
        summary = [
            {
                k: r.get(k)
                for k in ("state", "year", "status", "accepted", "annual_relative_error", "error")
            }
            for r in report["checks"]
        ]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not report["accepted"]:
            parser.exit(1, "Conferência TabNet não aprovada em todas as UF/anos.\n")
    elif args.command == "panel":
        import re

        from .transform.panel import build_panel

        if args.regions and not all(re.fullmatch(r"\d{5}", code) for code in args.regions):
            parser.exit(2, "Códigos de região de saúde têm 5 dígitos.\n")
        os.umask(0o077)
        manifest = build_panel(
            args.outcomes,
            args.pm25,
            args.meteorology,
            args.calendar,
            args.output,
            regions=args.regions,
            srag_path=args.srag,
            merra2_path=args.merra2,
            fires_path=args.fires,
        )
        print(json.dumps({k: manifest[k] for k in ("rows", "regions", "dates")}, indent=2))
    elif args.command == "sih-national":
        from datetime import date

        from .ingest.sih import STATES
        from .ingest.sih_national import extract_national

        try:
            start, end = (date.fromisoformat(f"{value}-01") for value in (args.start, args.end))
        except ValueError:
            parser.exit(2, "Use competências no formato AAAA-MM.\n")
        if not date(2012, 1, 1) <= start <= end <= date.today():
            parser.exit(2, "Use 2012-01 <= início <= fim <= mês atual.\n")
        states = [state.upper() for state in args.states] if args.states else STATES
        if not set(states) <= set(STATES):
            parser.exit(2, f"UF inválida; use: {' '.join(STATES)}\n")
        if not 1 <= args.workers <= 8:
            parser.exit(2, "--workers deve ficar entre 1 e 8.\n")
        # Nationwide health microdata: readable by the operator only.
        os.umask(0o077)
        report = extract_national(
            start,
            end,
            states=states,
            workers=args.workers,
            keep_source=args.keep_source,
        )
        summary = {k: v for k, v in report.items() if k not in ("states",)}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if report["failed"]:
            parser.exit(1, f"{len(report['failed'])} partições falharam; rode de novo.\n")
    elif args.command == "sih":
        from .ingest.sih import convert_sih, download_sih

        source = download_sih(args.state, args.year, args.month)
        print(convert_sih(source, Path("data/staging/sih") / f"{source.stem}.parquet"))
    elif args.command == "cams":
        from .ingest.cams import retrieve_cams

        print(retrieve_cams(args.dataset, json.loads(args.request.read_text())))
    elif args.command == "exposure":
        from .ingest.worldpop import WORLDPOP_ATTRIBUTION, download_worldpop
        from .transform.cams_exposure import build_exposure

        series = build_exposure(
            args.cams_file,
            download_worldpop(),
            args.regions,
            args.output,
            attribution=(WORLDPOP_ATTRIBUTION,),
        )
        print(
            f"{series.height} linhas, {series['health_region_code'].n_unique()} regiões: "
            f"{args.output}"
        )
    elif args.command == "meteorology":
        from .ingest.worldpop import WORLDPOP_ATTRIBUTION, download_worldpop
        from .transform.meteorology import build_meteorology

        series = build_meteorology(
            args.era5_files,
            download_worldpop(),
            args.regions,
            args.output,
            attribution=(WORLDPOP_ATTRIBUTION,),
        )
        print(
            f"{series.height} linhas, {series['health_region_code'].n_unique()} regiões: "
            f"{args.output}"
        )
    elif args.command == "ipca":
        from datetime import UTC, datetime

        from .ingest.ipca import IPCA_ATTRIBUTION, download_ipca
        from .transform.cams_exposure import describe_input, write_with_manifest

        frame, source = download_ipca(refresh=args.refresh)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_with_manifest(
            args.output,
            frame.write_parquet,
            {
                "created_at": datetime.now(UTC).isoformat(),
                "months": [str(frame["month"].min()), str(frame["month"].max())],
                "inputs": {"sidra": [describe_input(source)]},
                "attribution": [IPCA_ATTRIBUTION],
            },
        )
        print(f"IPCA {frame['month'].min()} a {frame['month'].max()}: {args.output}")
    elif args.command == "calendar":
        from datetime import UTC, date, datetime

        import holidays

        from .transform.calendar import holiday_calendar, region_states
        from .transform.cams_exposure import describe_input, write_with_manifest

        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        if start > end:
            parser.exit(1, "--start deve ser anterior ou igual a --end\n")
        frame = holiday_calendar(start, end, region_states(args.regions))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_with_manifest(
            args.output,
            frame.write_parquet,
            {
                "created_at": datetime.now(UTC).isoformat(),
                "dates": [args.start, args.end],
                "holidays_package": holidays.__version__,
                "coverage": "national and state public holidays; municipal ones are absent",
                "inputs": {"regions": [describe_input(args.regions)]},
            },
        )
        print(
            f"{frame.height} linhas, {frame['health_region_code'].n_unique()} regiões: "
            f"{args.output}"
        )
    elif args.command == "export":
        import polars as pl

        from .publish.export_web import export_aggregates

        export_aggregates(pl.read_parquet(args.source), args.output)
