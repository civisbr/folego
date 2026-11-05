"""DATASUS residence codes and SIH age/diagnosis semantics."""

import json
import re
from importlib.resources import files

_CHECKSUM_EXCEPTIONS = frozenset(
    json.loads(files("folego").joinpath("resources/ibge-checksum-exceptions.json").read_text())
)


def municipality_code(value: str | int) -> str:
    code = str(value).strip()
    if not re.fullmatch(r"\d{6,7}", code) or code[:2] not in {
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
        "17",
        "21",
        "22",
        "23",
        "24",
        "25",
        "26",
        "27",
        "28",
        "29",
        "31",
        "32",
        "33",
        "35",
        "41",
        "42",
        "43",
        "50",
        "51",
        "52",
        "53",
    }:
        raise ValueError(f"Invalid municipality code: {value!r}")
    if len(code) == 7 and code not in _CHECKSUM_EXCEPTIONS:
        # IBGE uses a modulus-10 check digit (alternating 1/2 weights).
        total = sum(
            sum(map(int, str(int(n) * (1 if i % 2 == 0 else 2)))) for i, n in enumerate(code[:6])
        )
        if (10 - total % 10) % 10 != int(code[-1]):
            raise ValueError(f"Invalid IBGE check digit: {value!r}")
    return code[:6]


def age_group(unit: int, value: int) -> str | None:
    if value < 0:
        return None
    if unit in (1, 2, 3):
        years = 0  # hours, days, months
    elif unit == 4 and value <= 99:
        years = value
    elif unit == 5 and value <= 99:
        years = value + 100
    else:
        return None
    return "0-4" if years < 5 else "5-14" if years < 15 else "15-59" if years < 60 else "60+"


def diagnosis_group(code: str) -> str | None:
    code = str(code).strip().upper().replace(".", "")
    if not re.fullmatch(r"[A-Z]\d{2}\d*", code):
        return None
    chapter, number = code[0], int(code[1:3])
    if chapter == "J":
        return (
            "asthma" if 45 <= number <= 46 else "pneumonia" if 12 <= number <= 18 else "respiratory"
        )
    if chapter == "I":
        return "circulatory"
    if chapter in ("S", "T") and (chapter == "S" or number <= 98):
        return "negative_control"
    return None
