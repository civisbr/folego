import json
from datetime import date

import pytest

from folego.transform.calendar import holiday_calendar, region_states


def test_regions_get_national_and_own_state_holidays():
    frame = holiday_calendar(date(2019, 1, 1), date(2019, 12, 31), {"11004": "RO", "35001": "SP"})
    assert frame.height == 2 * 365

    def flag(code, day, column="holiday"):
        row = frame.filter((frame["health_region_code"] == code) & (frame["date"] == day))
        return row[column].item()

    assert flag("11004", date(2019, 9, 7)) == flag("35001", date(2019, 9, 7)) == 1
    assert flag("11004", date(2019, 1, 4)) == 1  # Rondônia creation day
    assert flag("35001", date(2019, 1, 4)) == 0
    assert flag("35001", date(2019, 7, 9)) == 1  # São Paulo constitutionalist revolution
    assert flag("11004", date(2019, 3, 5)) == 0
    assert flag("11004", date(2019, 3, 5), "optional_holiday") == 1  # Carnival Tuesday
    assert flag("11004", date(2019, 3, 6)) == 0


def test_region_states_come_from_the_geometry_file(tmp_path):
    path = tmp_path / "regions.geojson"
    features = [
        {"type": "Feature", "properties": {"code": c, "state": s}, "geometry": None}
        for c, s in [("11004", "RO"), ("35001", "SP")]
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    assert region_states(path) == {"11004": "RO", "35001": "SP"}


def test_unknown_state_fails():
    with pytest.raises(ValueError, match="state"):
        holiday_calendar(date(2019, 1, 1), date(2019, 1, 2), {"99999": "XX"})
