from __future__ import annotations

import copy
import random
from pathlib import Path

import numpy as np
import pytest

from berth.baseline import baseline_power
from berth.evaluate import evaluate, find_violations
from berth.models import Fleet
from berth.optimizer import optimize
from berth.problem import build_problem

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

TOU = {
    "name": "test TOU",
    "energy_price_per_kwh": [
        {"from": "00:00", "to": "06:00", "price": 0.10},
        {"from": "06:00", "to": "16:00", "price": 0.20},
        {"from": "16:00", "to": "21:00", "price": 0.40},
        {"from": "21:00", "to": "24:00", "price": 0.15},
    ],
    "demand_charge_per_kw": 12.0,
}


def make_fleet(vehicles: list[dict], **site) -> dict:
    """A small fleet dict. Site defaults: 100 kW limit, 10 x 22 kW chargers."""
    s = {"power_limit_kw": 100, "chargers": [{"count": 10, "kw": 22}]}
    s.update(site)
    return {"name": "test", "start": "16:00", "site": s, "tariff": copy.deepcopy(TOU), "vehicles": vehicles}


def van(i: int, **kw) -> dict:
    v = {
        "id": f"V{i}",
        "battery_kwh": 80,
        "max_kw": 11,
        "arrival": "18:00",
        "departure": "06:00",
        "arrival_soc_pct": 20,
        "required_soc_pct": 90,
    }
    v.update(kw)
    return v


def random_fleet(seed: int) -> dict:
    """Random small fleets for property tests (fixed seeds)."""
    rng = random.Random(seed)
    vehicles = []
    for i in range(rng.randint(2, 8)):
        arr = rng.randrange(16 * 60, 22 * 60, 5)
        dep = rng.randrange(4 * 60, 9 * 60, 5)
        vehicles.append(
            van(
                i,
                battery_kwh=rng.choice([60, 80, 100]),
                max_kw=rng.choice([7.4, 11, 19.2, 50]),
                arrival=f"{arr // 60:02d}:{arr % 60:02d}",
                departure=f"{dep // 60:02d}:{dep % 60:02d}",
                arrival_soc_pct=rng.randint(5, 60),
                required_soc_pct=rng.choice([70, 80, 90, 100]),
            )
        )
    groups = [{"count": rng.randint(1, 5), "kw": rng.choice([11, 22])}]
    if rng.random() < 0.4:
        groups.append({"count": rng.randint(1, 2), "kw": 50})
    site = {
        "power_limit_kw": rng.choice([40, 60, 100, 200]),
        "chargers": groups,
        "charging_efficiency": rng.choice([0.9, 1.0]),
    }
    if rng.random() < 0.5:
        site["other_load_kw"] = [{"from": "07:00", "to": "19:00", "kw": 15}, {"from": "19:00", "to": "07:00", "kw": 5}]
    f = make_fleet(vehicles, **site)
    f["start"] = "16:00"
    f["tariff"]["demand_charge_per_kw"] = rng.choice([0.0, 12.0])
    return f


def solve(fleet: dict, time_limit: float | None = None):
    """Build, optimize and evaluate. Returns (problem, opt output, metrics, baseline metrics)."""
    prob = build_problem(Fleet.model_validate(fleet))
    opt = optimize(prob, time_limit=time_limit)
    return prob, opt, evaluate(prob, opt.power), evaluate(prob, baseline_power(prob))


def assert_valid(prob, power: np.ndarray) -> None:
    problems = find_violations(prob, power)
    assert problems == [], problems


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES
