"""The time limit covers the whole plan, and a solver that runs out of time
still gives a valid plan: never less ready than charging on arrival and,
when that gets everyone ready, never more expensive."""

from __future__ import annotations

import random
import time

import numpy as np
import pytest

from berth.planner import plan
from berth.solver import LinearModel, SolveResult

from conftest import make_fleet, short_fleet, van


@pytest.fixture
def solver_finds_nothing(monkeypatch):
    """Every solve stops at its time limit without a plan (as on a slow
    computer with a large fleet)."""

    def solve(self, cost, **kw):
        return SolveResult(has_solution=False, optimal=False, status="Time limit reached",
                           x=np.zeros(self.n_vars), objective=float("nan"), gap=None, seconds=0.0)

    monkeypatch.setattr(LinearModel, "solve", solve)


def test_no_time_for_the_cost_step_gives_charging_on_arrival(solver_finds_nothing):
    r = plan(make_fleet([van(i) for i in range(4)]))
    assert r.all_ready
    assert r.optimized.total_cost <= r.baseline.total_cost + 1e-6
    assert r.optimized.grid_kwh == pytest.approx(r.baseline.grid_kwh)
    assert not r.solver.optimal


@pytest.mark.parametrize("policy", ["most_ready", "fair"])
def test_no_time_for_readiness_still_gives_a_plan(solver_finds_nothing, policy):
    fleet = short_fleet(0)
    fleet["shortfall_policy"] = policy
    r = plan(fleet)  # used to raise "the solver found no plan"
    assert r.optimized.vehicles_ready >= r.baseline.vehicles_ready
    assert not r.solver.optimal


def _crowded_fleet() -> dict:
    rng = random.Random(7)
    vehicles = []
    for i in range(40):
        arr = rng.randrange(16 * 60, 22 * 60, 5)
        dep = rng.randrange(4 * 60, 8 * 60, 5)
        vehicles.append(van(i, max_kw=22, arrival=f"{arr // 60:02d}:{arr % 60:02d}",
                            departure=f"{dep // 60:02d}:{dep % 60:02d}", arrival_soc_pct=rng.randint(10, 40)))
    return make_fleet(vehicles, power_limit_kw=120, chargers=[{"count": 8, "kw": 22}, {"count": 2, "kw": 50}])


def test_time_limit_covers_the_whole_plan():
    # 40 vans on 10 chargers with a tight site: a hard case that also needs a
    # diagnosis. It used to take 25 s or more with a 2 s limit.
    started = time.perf_counter()
    r = plan(_crowded_fleet(), time_limit=2)
    assert time.perf_counter() - started < 6
    assert not r.all_ready and r.diagnosis is not None
    assert r.optimized.vehicles_ready >= r.baseline.vehicles_ready
