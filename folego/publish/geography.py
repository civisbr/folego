"""Dissolve official IBGE municipality polygons using a verified SUS lookup."""

import json
from collections import defaultdict
from pathlib import Path

import polars as pl
from shapely import make_valid, union_all
from shapely.geometry import mapping, shape


def export_geography(boundaries: Path, lookup: pl.DataFrame, output: Path):
    correspondence = {row["ibge_code"]: row for row in lookup.to_dicts()}
    geometries = defaultdict(list)
    metadata = {}
    for feature in json.loads(boundaries.read_text())["features"]:
        code = str(feature["properties"]["codarea"])
        row = correspondence.get(code)
        if not row or not row["health_region_code"]:
            continue
        region = row["health_region_code"]
        geometries[region].append(make_valid(shape(feature["geometry"])))
        metadata[region] = {"code": region, "name": row["health_region"], "state": row["state"]}
    features = []
    for region, parts in sorted(geometries.items()):
        geometry = union_all(parts).simplify(0.025, preserve_topology=True)
        point = geometry.representative_point()
        features.append(
            {
                "type": "Feature",
                "id": region,
                "properties": {
                    **metadata[region],
                    "center": [round(point.x, 4), round(point.y, 4)],
                },
                "geometry": mapping(geometry),
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return features
