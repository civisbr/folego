"""Explicit commands; no default action downloads historical national microdata."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(prog="folego")
    commands = parser.add_subparsers(dest="command", required=True)
    territory = commands.add_parser("territory", help="Atualizar correspondência nacional oficial")
    territory.add_argument("--allow-incomplete", action="store_true")
    territory.add_argument("--refresh", action="store_true")
    commands.add_parser("demo", help="Regerar somente dados sintéticos do painel")
    commands.add_parser("geography", help="Gerar malha pública por região de saúde")
    sih = commands.add_parser("sih", help="Baixar uma competência SIH/SUS")
    sih.add_argument("--state", required=True)
    sih.add_argument("--year", type=int, required=True)
    sih.add_argument("--month", type=int, required=True)
    cams = commands.add_parser("cams", help="Executar requisição exportada do ADS")
    cams.add_argument("--dataset", required=True)
    cams.add_argument("--request", type=Path, required=True)
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
    args = parser.parse_args()
    if args.command == "forecast":
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
    elif args.command == "sih":
        from .ingest.sih import convert_sih, download_sih

        source = download_sih(args.state, args.year, args.month)
        print(convert_sih(source, Path("data/staging/sih") / f"{source.stem}.parquet"))
    elif args.command == "cams":
        from .ingest.cams import retrieve_cams

        print(retrieve_cams(args.dataset, json.loads(args.request.read_text())))
    elif args.command == "export":
        import polars as pl

        from .publish.export_web import export_aggregates

        export_aggregates(pl.read_parquet(args.source), args.output)
