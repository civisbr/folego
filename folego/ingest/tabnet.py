"""Read-only TabNet queries built from the official HTML form, not guessed indexes."""

import hashlib
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import httpx
import polars as pl


class TabnetForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.options = {}
        self.current = None
        self.option = None
        self.action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action")
        elif tag == "select":
            self.current = attrs.get("name")
            self.options[self.current] = []
        elif tag == "option" and self.current:
            self.option = {"value": attrs.get("value"), "text": ""}
            self.options[self.current].append(self.option)

    def handle_endtag(self, tag):
        if tag == "select":
            self.current = None
            self.option = None
        elif tag == "option":
            self.option = None

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data.strip()


def build_query(form: TabnetForm, year: int, municipality_codes: list[str], state: str):
    def require(field, value):
        if not any(o["value"] == value for o in form.options.get(field, [])):
            raise ValueError(f"TabNet option missing: {field}={value}")

    require("Linha", "Ano/mês_processamento")
    require("Coluna", "--Não-Ativa--")
    require("Incremento", "Internações")
    items = [
        ("Linha", "Ano/mês_processamento"),
        ("Coluna", "--Não-Ativa--"),
        ("Incremento", "Internações"),
    ]
    for month in range(1, 13):
        filename = f"nr{state.lower()}{year % 100:02d}{month:02d}.dbf"
        require("Arquivos", filename)
        items.append(("Arquivos", filename))
    selected = []
    for code in municipality_codes:
        matches = [
            o["value"]
            for o in form.options.get("SMunicípio", [])
            if o["text"].startswith(code + " ")
        ]
        if len(matches) != 1:
            raise ValueError(f"Municipality missing/ambiguous in TabNet: {code}")
        selected.append(matches[0])
    chapter = [
        o["value"]
        for o in form.options.get("SCapítulo_CID-10", [])
        if re.match(r"X\.\s", o["text"]) and "respirat" in o["text"].lower()
    ]
    if len(chapter) != 1:
        raise ValueError("Respiratory chapter missing/ambiguous in TabNet")
    for name in form.options:
        if not name or not name.startswith("S"):
            continue
        values = (
            selected
            if name == "SMunicípio"
            else chapter
            if name == "SCapítulo_CID-10"
            else ["TODAS_AS_CATEGORIAS__"]
        )
        for value in values:
            require(name, value)
            items.append((name, value))
    items.extend([("zeradas", "exibirlz"), ("formato", "table"), ("mostre", "Mostra")])
    return items


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def finish_cell(self):
        if self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
        self.cell = None

    def finish_row(self):
        self.finish_cell()
        if self.row is not None:
            self.rows.append(self.row)
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.finish_row()
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.finish_cell()
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.finish_cell()
        elif tag in ("tr", "thead", "tbody", "tfoot", "table"):
            self.finish_row()


def parse_monthly_counts(payload: bytes, year: int) -> pl.DataFrame:
    parser = TableRows()
    parser.feed(payload.decode("latin-1"))
    parser.finish_row()
    months = {
        "jan": 1,
        "fev": 2,
        "mar": 3,
        "abr": 4,
        "mai": 5,
        "jun": 6,
        "jul": 7,
        "ago": 8,
        "set": 9,
        "out": 10,
        "nov": 11,
        "dez": 12,
    }
    rows = []
    for cells in parser.rows:
        if len(cells) != 2:
            continue
        match = re.fullmatch(rf"{year}/([A-Za-z]{{3}})", cells[0])
        if match:
            month = months.get(match[1].lower())
        else:
            match = re.fullmatch(rf"(?:\.\.)?([A-Za-zçÇ]+)/{year}", cells[0])
            if not match:
                continue
            month = months.get(match[1][:3].lower())
        value = cells[1].replace(".", "").replace(" ", "")
        if value == "-":
            value = "0"
        if not month or not value.isdigit():
            raise ValueError("Invalid/unknown TabNet monthly count")
        rows.append({"month": month, "admissions": int(value)})
    frame = pl.DataFrame(rows, schema={"month": pl.Int64, "admissions": pl.Int64})
    if frame.height != 12 or sorted(frame["month"].to_list()) != list(range(1, 13)):
        raise ValueError("TabNet response must contain exactly 12 distinct processing months")
    return frame.sort("month")


def validate_response_scope(payload: bytes, fields, state: str, year: int):
    """Check the query echoed by TabNet before accepting its counts."""
    match = re.search(r"<TABELA>\s*(.*?)</TABELA>", payload.decode("latin-1"), re.S)
    if not match:
        raise ValueError("TabNet response has no auditable query echo")
    echoed = {}
    for line in match[1].splitlines():
        if "=" in line:
            key, value = line.strip().split("=", 1)
            echoed.setdefault(key, []).append(value)
    expected = {"Def": [f"sih/cnv/nr{state.lower()}.def"]}
    for key in ("Linha", "Coluna", "Incremento", "SMunicípio", "SCapítulo_CID-10"):
        expected[key] = [value for name, value in fields if name == key]
    extra_filters = {key for key in echoed if key.startswith("S") and key not in expected}
    if extra_filters:
        raise ValueError("TabNet response scope contains unexpected selection filters")
    for key, values in expected.items():
        if sorted(echoed.get(key, [])) != sorted(values):
            raise ValueError(f"TabNet response scope differs for {key}")
    periods = {f"{year % 100:02d}{month:02d}" for month in range(1, 13)}
    if set(echoed.get("Periodo", [""])[0].split(",")) != periods:
        raise ValueError("TabNet response processing periods differ")


def retrieve_reference(
    year: int, state: str, municipality_codes: list[str], root=Path("data"), *, refresh=False
):
    scope = {
        "year": year,
        "state": state,
        "municipality_codes": sorted(municipality_codes),
        "period_basis": "processing_competence",
        "outcome": "J00-J99",
        "metric": "Internações",
    }
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]
    directory = root / "raw" / "tabnet" / key
    directory.mkdir(parents=True, exist_ok=True)
    html = directory / "response.html"
    metadata = directory / "source.json"
    if html.exists() and metadata.exists() and not refresh:
        source = json.loads(metadata.read_text())
        if source["sha256"] != hashlib.sha256(html.read_bytes()).hexdigest():
            raise ValueError("Corrupt TabNet cache")
        validate_response_scope(html.read_bytes(), source["fields"], state, year)
        return parse_monthly_counts(html.read_bytes(), year), source
    # A preserved response can be re-parsed after a parser fix, without issuing
    # another query or pretending it is a newly retrieved snapshot.
    last_response = directory / "last-response.html"
    request = directory / "request.json"
    if last_response.exists() and request.exists() and not refresh:
        source = json.loads(request.read_text())
        if all(source.get(key) == value for key, value in scope.items()):
            payload = last_response.read_bytes()
            validate_response_scope(payload, source["fields"], state, year)
            result = parse_monthly_counts(payload, year)
            source["sha256"] = hashlib.sha256(payload).hexdigest()
            html.write_bytes(payload)
            metadata.write_text(json.dumps(source, ensure_ascii=False, indent=2))
            return result, source
    form_url = f"https://tabnet.datasus.gov.br/cgi/deftohtm.exe?sih/cnv/nr{state.lower()}.def"
    query_url = f"https://tabnet.datasus.gov.br/cgi/tabcgi.exe?sih/cnv/nr{state.lower()}.def"
    # Explicitly bypass shell proxy settings for this public legacy endpoint.
    transport = httpx.HTTPTransport(retries=2)
    with httpx.Client(
        timeout=httpx.Timeout(120, connect=15),
        transport=transport,
        trust_env=False,
        follow_redirects=True,
    ) as client:
        response = client.get(form_url)
        response.raise_for_status()
        (directory / "form.html").write_bytes(response.content)
        form = TabnetForm()
        form.feed(response.content.decode("latin-1"))
        if form.action != f"/cgi/tabcgi.exe?sih/cnv/nr{state.lower()}.def":
            raise ValueError("Unexpected TabNet form action")
        fields = build_query(form, year, municipality_codes, state)
        source = {
            **scope,
            "form_url": form_url,
            "url": query_url,
            "fields": fields,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
        (directory / "request.json").write_text(json.dumps(source, ensure_ascii=False, indent=2))
        response = client.post(
            query_url,
            content=urlencode(fields, encoding="latin-1"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": form_url},
        )
        response.raise_for_status()
        (directory / "last-response.html").write_bytes(response.content)
        validate_response_scope(response.content, fields, state, year)
        result = parse_monthly_counts(response.content, year)
        html.write_bytes(response.content)
        source["sha256"] = hashlib.sha256(response.content).hexdigest()
        metadata.write_text(json.dumps(source, ensure_ascii=False, indent=2))
        return result, source


def compare_monthly_counts(observed: pl.DataFrame, reference: pl.DataFrame):
    for frame in (observed, reference):
        if frame.height != 12 or sorted(frame["month"].to_list()) != list(range(1, 13)):
            raise ValueError("Comparison requires 12 distinct months on both sides")
        if frame["admissions"].null_count() or (frame["admissions"] < 0).any():
            raise ValueError("Missing or negative counts")
    merged = (
        observed.select("month", "admissions")
        .join(reference.select("month", "admissions"), on="month", suffix="_reference")
        .sort("month")
    )
    errors = []
    accepted = True
    for row in merged.to_dicts():
        expected = row["admissions_reference"]
        actual = row["admissions"]
        error = abs(actual - expected) / expected if expected else 0.0 if actual == 0 else None
        accepted &= error is not None and error < 0.01
        errors.append(
            {
                "month": row["month"],
                "observed": actual,
                "reference": expected,
                "relative_error": error,
            }
        )
    total_observed = int(merged["admissions"].sum())
    total_reference = int(merged["admissions_reference"].sum())
    annual = (
        abs(total_observed - total_reference) / total_reference
        if total_reference
        else 0.0
        if total_observed == 0
        else None
    )
    return {
        "accepted": bool(accepted),
        "criterion": "relative error < 1% annually and in every month",
        "total_observed": total_observed,
        "total_reference": total_reference,
        "annual_relative_error": annual,
        "months": errors,
    }
