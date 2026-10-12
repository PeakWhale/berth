"""Read fleets from YAML, JSON and CSV files, with clear error messages."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import yaml

from berth.errors import BerthInputError
from berth.models import Fleet
from berth.planner import parse_fleet

VEHICLE_REQUIRED = ["id", "battery_kwh", "max_kw", "arrival", "departure", "required_soc_pct"]
VEHICLE_OPTIONAL = ["arrival_soc_pct", "trip_kwh"]
VEHICLE_NUMBERS = {"battery_kwh", "max_kw", "required_soc_pct", "arrival_soc_pct", "trip_kwh"}


# ---------------------------------------------------------------------------
# YAML / JSON


def parse_yaml_text(text: str, source: str = "input") -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise BerthInputError(f"this is not valid YAML ({where}{problem})", source) from None
    if data is None:
        raise BerthInputError("the file is empty", source)
    if not isinstance(data, dict):
        raise BerthInputError("the top level must be a mapping with keys like site, tariff and vehicles", source)
    return data


def load_fleet_text(text: str, source: str = "input", base_dir: Path | None = None) -> Fleet:
    """Parse YAML (or JSON, which is also YAML) text into a Fleet.

    File names (like `tariff: tariff.yaml`) are only followed when base_dir
    is given. The HTTP API never passes it, so a request cannot read files
    on the server."""
    data = parse_yaml_text(text, source)
    if base_dir is not None:
        data = resolve_references(data, base_dir, source)
    else:
        for key, what in (("tariff", "the tariff"), ("vehicles", "the vehicles")):
            if isinstance(data.get(key), str):
                raise BerthInputError(
                    f"{key}: '{data[key]}' looks like a file name. Here {what} must be written out in full "
                    "(file names only work on the command line).",
                    source,
                )
    return parse_fleet(data, source)


def load_fleet_file(path: str | Path) -> Fleet:
    path = Path(path)
    text = _read(path)
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BerthInputError(f"this is not valid JSON (line {exc.lineno}: {exc.msg})", str(path)) from None
        data = resolve_references(data, path.parent, str(path))
        return parse_fleet(data, str(path))
    if path.suffix.lower() == ".csv":
        raise BerthInputError(
            "a CSV file holds only vehicles. Use: berth plan --vehicles vehicles.csv --tariff tariff.yaml "
            "--chargers 8x19.2 --site-limit-kw 150, or point to the CSV from a fleet YAML (vehicles: vehicles.csv)",
            str(path),
        )
    return load_fleet_text(text, str(path), path.parent)


def resolve_references(data: Any, base_dir: Path, source: str) -> Any:
    """Allow `tariff: tariff.yaml` and `vehicles: vehicles.csv` in a fleet file."""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    tariff = data.get("tariff")
    if isinstance(tariff, str):
        data["tariff"] = load_tariff_file(base_dir / tariff, referenced_from=source)
    vehicles = data.get("vehicles")
    if isinstance(vehicles, str):
        vpath = base_dir / vehicles
        data["vehicles"] = read_vehicles_csv(_read(vpath, referenced_from=source), str(vpath))
    return data


def load_tariff_file(path: str | Path, referenced_from: str | None = None) -> dict:
    path = Path(path)
    text = _read(path, referenced_from)
    if path.suffix.lower() == ".csv":
        return read_tariff_csv(text, str(path))
    data = parse_yaml_text(text, str(path))
    return data


def _read(path: Path, referenced_from: str | None = None) -> str:
    if not path.exists():
        extra = f" (named in {referenced_from})" if referenced_from else ""
        raise BerthInputError(f"file not found: {path}{extra}")
    if path.is_dir():
        raise BerthInputError(f"{path} is a folder, not a file")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise BerthInputError("the file is not UTF-8 text", str(path)) from None


# ---------------------------------------------------------------------------
# CSV


def _csv_rows(text: str, source: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Header and rows with their line numbers. Skips blank and # lines."""
    lines = [(i, line) for i, line in enumerate(text.splitlines(), start=1)]
    lines = [(i, line) for i, line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise BerthInputError("the CSV file is empty", source)
    header_line, header_text = lines[0]
    header = [_norm(h) for h in next(csv.reader([header_text]))]
    rows = []
    for i, line in lines[1:]:
        cells = [c.strip() for c in next(csv.reader([line]))]
        if len(cells) > len(header) and any(cells[len(header):]):
            raise BerthInputError(f"line {i}: {len(cells)} values but only {len(header)} columns in the header", source)
        cells += [""] * (len(header) - len(cells))
        rows.append((i, cells[: len(header)]))
    return header, rows


def _norm(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _number(value: str, column: str, line: int, source: str) -> float:
    try:
        return float(value.replace("%", "").strip())
    except ValueError:
        raise BerthInputError(f"line {line}: {column} '{value}' is not a number", source) from None


def read_vehicles_csv(text: str, source: str = "vehicles.csv") -> list[dict]:
    """One row per stop. Rows with the same id are stops of one vehicle, in
    order. arrival_soc_pct is needed on a vehicle's first row; trip_kwh is
    the energy used before arriving at a later stop."""
    header, rows = _csv_rows(text, source)
    missing = [c for c in VEHICLE_REQUIRED if c not in header]
    if missing:
        raise BerthInputError(
            f"missing column(s): {', '.join(missing)}. Expected columns: "
            f"{', '.join(VEHICLE_REQUIRED + VEHICLE_OPTIONAL)}",
            source,
        )
    unknown = [c for c in header if c not in VEHICLE_REQUIRED + VEHICLE_OPTIONAL]
    if unknown:
        raise BerthInputError(
            f"unknown column(s): {', '.join(unknown)}. Known columns: "
            f"{', '.join(VEHICLE_REQUIRED + VEHICLE_OPTIONAL)}",
            source,
        )
    if not rows:
        raise BerthInputError("the CSV file has a header but no vehicles", source)

    vehicles: dict[str, dict] = {}
    stops: dict[str, list[dict]] = {}
    problems: list[str] = []
    for line, cells in rows:
        row = dict(zip(header, cells))
        vid = row["id"]
        if not vid:
            problems.append(f"line {line}: id is empty")
            continue
        try:
            nums = {}
            for col in VEHICLE_NUMBERS & set(header):
                if row[col] != "":
                    nums[col] = _number(row[col], col, line, source)
        except BerthInputError as exc:
            problems.extend(exc.problems)
            continue
        for col in ("arrival", "departure"):
            if not row[col]:
                problems.append(f"line {line}: {col} is empty")
        if "required_soc_pct" not in nums:
            problems.append(f"line {line}: required_soc_pct is empty")
        stop = {"arrival": row["arrival"], "departure": row["departure"], "required_soc_pct": nums.get("required_soc_pct")}
        if vid not in vehicles:
            for col in ("battery_kwh", "max_kw", "arrival_soc_pct"):
                if col not in nums:
                    problems.append(f"line {line}: {col} is needed on the first row of vehicle {vid}")
            vehicles[vid] = {
                "id": vid,
                "battery_kwh": nums.get("battery_kwh"),
                "max_kw": nums.get("max_kw"),
                "arrival_soc_pct": nums.get("arrival_soc_pct"),
            }
            stops[vid] = [stop]
        else:
            first = vehicles[vid]
            for col in ("battery_kwh", "max_kw"):
                if col in nums and first[col] is not None and abs(nums[col] - first[col]) > 1e-9:
                    problems.append(f"line {line}: {col} for {vid} differs from its first row")
            stop["trip_kwh"] = nums.get("trip_kwh", 0.0)
            stops[vid].append(stop)
    if problems:
        raise BerthInputError(problems, source)

    out = []
    for vid, veh in vehicles.items():
        s = stops[vid]
        if len(s) == 1:
            out.append({**veh, "arrival": s[0]["arrival"], "departure": s[0]["departure"],
                        "required_soc_pct": s[0]["required_soc_pct"]})
        else:
            out.append({**veh, "stops": s})
    return out


def read_tariff_csv(text: str, source: str = "tariff.csv") -> dict:
    """Either `hour,price_per_kwh` (24 rows) or `from,to,price_per_kwh`."""
    header, rows = _csv_rows(text, source)
    price_col = next((c for c in ("price_per_kwh", "price") if c in header), None)
    if price_col is None:
        raise BerthInputError("missing column price_per_kwh (or price)", source)
    if "hour" in header:
        prices: dict[int, float] = {}
        for line, cells in rows:
            row = dict(zip(header, cells))
            hour = _number(row["hour"], "hour", line, source)
            if hour != int(hour) or not 0 <= hour <= 23:
                raise BerthInputError(f"line {line}: hour must be a whole number from 0 to 23", source)
            prices[int(hour)] = _number(row[price_col], price_col, line, source)
        missing = [h for h in range(24) if h not in prices]
        if missing:
            raise BerthInputError(f"no price for hour(s): {', '.join(map(str, missing))}", source)
        profile: Any = [prices[h] for h in range(24)]
    elif "from" in header and "to" in header:
        profile = []
        for line, cells in rows:
            row = dict(zip(header, cells))
            profile.append({"from": row["from"], "to": row["to"],
                            "value": _number(row[price_col], price_col, line, source)})
    else:
        raise BerthInputError("expected columns hour,price_per_kwh or from,to,price_per_kwh", source)
    return {"name": Path(source).stem, "energy_price_per_kwh": profile}


def parse_chargers(spec: str) -> list[dict]:
    """"8x19.2,2x50" -> [{count: 8, kw: 19.2}, {count: 2, kw: 50}]."""
    groups = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*(?:kw|kW|KW)?", part)
        if not m:
            raise BerthInputError(f"'{part}' is not a charger group. Use COUNTxKW, like 8x19.2 or 8x19.2,2x50")
        groups.append({"count": int(m.group(1)), "kw": float(m.group(2))})
    return groups


# ---------------------------------------------------------------------------
# Writing YAML


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    text = str(v)
    return json.dumps(text) if text == "" or re.search(r"[:#{}\[\],&*?|<>=!%@`'\"]|^\s|\s$|^[-\d]", text) or text.lower() in {
        "true", "false", "yes", "no", "on", "off", "null", "~"} else text


def _flow(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_flow(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_flow(x) for x in v) + "]"
    return _scalar(v)


def fleet_to_yaml(fleet: Fleet) -> str:
    """Readable YAML for a fleet: one line per vehicle (or per stop)."""
    data = fleet.model_dump(by_alias=True, exclude_none=True, exclude_defaults=False)
    lines: list[str] = []
    for key in ("name", "description", "start", "hours", "step_minutes", "time_limit_s"):
        if key in data:
            lines.append(f"{key}: {_scalar(data[key])}")
    lines.append(
        f"shortfall_policy: {data['shortfall_policy']}   "
        "# if not every vehicle can be ready: most_ready (most vehicles fully ready) or fair (share the shortfall)"
    )
    names = {"energy_price_per_kwh": "price", "power_limit_kw": "kw", "other_load_kw": "kw"}
    for section in ("site", "tariff"):
        lines.append(f"{section}:")
        for k, v in data[section].items():
            if isinstance(v, list) and k in names and v and isinstance(v[0], dict):
                v = [{"from": x["from"], "to": x["to"], names[k]: x["value"]} for x in v]
            if isinstance(v, list):
                lines.append(f"  {k}:")
                lines.extend(f"    - {_flow(x)}" for x in v)
            else:
                lines.append(f"  {k}: {_flow(v)}")
    lines.append("vehicles:")
    for veh in data["vehicles"]:
        stops = veh.pop("stops", None)
        if stops:
            stops = [{k: x for k, x in st.items() if not (k == "trip_kwh" and x == 0)} for st in stops]
            lines.append(f"  - {_flow(veh)[:-1]}, stops: [")
            lines.extend(f"      {_flow(s)}," for s in stops)
            lines[-1] = lines[-1].rstrip(",")
            lines.append("    ]}")
        else:
            lines.append(f"  - {_flow(veh)}")
    return "\n".join(lines) + "\n"
