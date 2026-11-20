"""WorldPop gridded population (CC BY 4.0), used only as exposure weights."""

from pathlib import Path

from .common import cached_download

# Unconstrained 1 km estimate for 2020, the last year of the 2000–2020 series.
WORLDPOP_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/BRA/"
    "bra_ppp_2020_1km_Aggregated.tif"
)
# Pinned from the 2026-11-20 download (file dated 2020-06-22 upstream); a change in the
# published file must be reviewed because it changes every exposure weight.
WORLDPOP_SHA256 = "37409736b9b693c140a984be03d7942664242a3139b5081e32a39c8d44029c8b"
WORLDPOP_ATTRIBUTION = (
    "WorldPop (www.worldpop.org), School of Geography and Environmental Science, "
    "University of Southampton. Population Counts, Unconstrained individual countries "
    "2000-2020 (1 km), Brazil 2020. CC BY 4.0."
)


def download_worldpop(root=Path("data")) -> Path:
    return cached_download(
        WORLDPOP_URL,
        root / "raw" / "worldpop" / "bra_ppp_2020_1km.tif",
        expected_sha256=WORLDPOP_SHA256,
    )
