"""Synthetic population rasters and region polygons shared by exposure tests."""

import json

import numpy as np
import rasterio


def write_population(tmp_path, population, *, origin=(-0.375, 0.375), crs="EPSG:4326"):
    # 0.375 deg pixels: each 2x2 block falls on one CAMS point at 0.75 deg.
    from rasterio.transform import from_origin

    population = np.asarray(population, dtype="float32")
    path = tmp_path / "pop.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=population.shape[0],
        width=population.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(*origin, 0.375, 0.375),
        nodata=-99999.0,
    ) as dst:
        dst.write(population, 1)
    return path


def write_regions(tmp_path, regions=None):
    regions = regions or {"A": (-0.375, 0.375), "B": (0.375, 1.125)}

    def feature(code, x0, x1):
        ring = [[x0, 0.375], [x1, 0.375], [x1, -1.125], [x0, -1.125], [x0, 0.375]]
        # MultiPolygon on purpose: the national file mixes both geometry types.
        return {
            "type": "Feature",
            "properties": {"code": code},
            "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
        }

    path = tmp_path / "regions.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [feature(code, *bounds) for code, bounds in regions.items()],
            }
        )
    )
    return path


POPULATION = [
    [1, 2, 10, 20],
    [3, 4, 30, 40],
    [5, 6, -99999, 0],
    [7, 8, 50, 60],
]
