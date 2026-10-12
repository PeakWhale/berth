"""Find and load the bundled example fleets."""

from __future__ import annotations

from pathlib import Path

import yaml

from berth.errors import BerthInputError
from berth.models import Fleet


def examples_dir() -> Path:
    installed = Path(__file__).parent / "examples"  # inside an installed wheel
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[2] / "examples"  # source checkout


def list_examples() -> list[dict]:
    """Example fleets: name, title, description."""
    out = []
    folder = examples_dir()
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and "vehicles" in data:
            n = len(data["vehicles"]) if isinstance(data["vehicles"], list) else None
            out.append(
                {
                    "name": path.stem,
                    "title": data.get("name", path.stem),
                    "description": (data.get("description") or "").strip(),
                    "vehicles": n,
                }
            )
    return out


def example_path(name: str) -> Path:
    names = [e["name"] for e in list_examples()]
    if name not in names:
        raise BerthInputError(f"unknown example '{name}'. Choose one of: {', '.join(names) or 'none found'}")
    return examples_dir() / f"{name}.yaml"


def load_example(name: str) -> Fleet:
    from berth.loaders import load_fleet_file

    return load_fleet_file(example_path(name))
