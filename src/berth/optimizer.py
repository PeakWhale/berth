"""The charging optimizer (linear / mixed-integer program, solved with HiGHS).

Decision: the charging power of every vehicle in every time step.

Limits:
  * a vehicle charges only while it is parked;
  * power <= the vehicle's max rate and <= the fastest charger;
  * all charging together <= site limit minus other building load;
  * the battery never goes above 100 %;
  * chargers: in each time step at most N vehicles can charge, and at most
    `count` of them on each charger group (with its kW). Berth does not track
    which exact charger a vehicle uses. If a step has more parked vehicles
    than chargers, on/off variables are added for that step only, which
    turns the model into a mixed-integer program. If there are enough
    chargers, the model stays a fast linear program.

Goal, in order:
  1. readiness: make the biggest shortfall (kWh missing at a departure) as
     small as possible, then make the total shortfall as small as possible;
     when every vehicle can be ready both are zero;
  2. cost: energy cost (price x energy) plus demand charge (rate x peak kW).
     Two tiny tie-breakers prefer flatter and earlier charging when the
     cost is the same; they are not part of the reported cost.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from berth.errors import BerthSolverError
from berth.problem import Problem
from berth.solver import INF, LinearModel, SolveResult

TOL_KWH = 1e-3
EARLY_TIEBREAK = 1e-5  # per kWh, grows slowly over the plan
FLAT_TIEBREAK = 1e-4  # per kW of peak, only when there is no demand charge


def contested(prob: Problem, vehicles: list[int]) -> bool:
    """True if these parked vehicles cannot all charge at full rate at the
    same time because there are too few (fast enough) chargers."""
    needs = sorted((prob.charge_rate(v) for v in vehicles), reverse=True)
    slots: list[float] = []
    for count, kw in prob.chargers:
        slots.extend([kw] * min(count, len(needs)))
    slots.sort(reverse=True)
    if len(needs) > len(slots):
        return True
    return any(n > k + 1e-9 for n, k in zip(needs, slots))


class ChargingModel:
    """Builds the model. Flags switch limits off for "what if" checks."""

    def __init__(
        self,
        prob: Problem,
        *,
        site_limit: bool = True,
        charger_limit: bool = True,
        extra_site: bool = False,
        extra_chargers: bool = False,
    ) -> None:
        self.prob = prob
        m = self.m = LinearModel()
        avail = prob.available_kw
        T = prob.n_steps

        # charging power p[v, t] (kW)
        self.p: dict[tuple[int, int], int] = {}
        for v, veh in enumerate(prob.vehicles):
            rate = prob.charge_rate(v)
            for s in veh.stops:
                for t in s.steps:
                    ub = rate
                    if site_limit and not extra_site:
                        ub = min(ub, float(avail[t]))
                    self.p[(v, t)] = m.var(0.0, ub)

        # shortfall per stop (kWh) and the biggest shortfall z
        self.s: list[int] = [m.var(0.0, INF) for _ in prob.stops]
        self.z = m.var(0.0, INF)

        # energy: battery level at each departure
        coef = prob.efficiency * prob.dt
        k = 0
        for v, veh in enumerate(prob.vehicles):
            cum: list[int] = []
            base = veh.arrival_kwh
            for s in veh.stops:
                cum = cum + [self.p[(v, t)] for t in s.steps]
                base -= s.trip_kwh
                n = len(cum)
                m.row(cum, [coef] * n, -INF, veh.battery_kwh - base)  # never above 100 %
                m.row(cum + [self.s[k]], [coef] * n + [1.0], s.required_kwh - base, INF)  # ready (or short)
                m.row([self.s[k], self.z], [1.0, -1.0], -INF, 0.0)
                k += 1

        by_t: dict[int, list[int]] = defaultdict(list)
        for (v, t), j in self.p.items():
            by_t[t].append(j)
        self.by_t = by_t

        # site power limit
        self.x_site: int | None = None
        if site_limit:
            if extra_site:
                self.x_site = m.var(0.0, INF)
            for t, cols in by_t.items():
                if self.x_site is not None:
                    m.row(cols + [self.x_site], [1.0] * len(cols) + [-1.0], -INF, float(avail[t]))
                elif sum(m.ub[j] for j in cols) > avail[t] + 1e-9:
                    m.row(cols, [1.0] * len(cols), -INF, float(avail[t]))

        # site peak (other load + charging)
        self.peak = m.var(float(prob.other_kw.max()), INF)
        for t, cols in by_t.items():
            m.row(cols + [self.peak], [1.0] * len(cols) + [-1.0], -INF, -float(prob.other_kw[t]))

        # chargers: on/off variables only in steps with more vehicles than chargers
        self.x_chg: int | None = None
        self.contested_steps = 0
        if charger_limit:
            if extra_chargers:
                self.x_chg = m.var(0.0, float(len(prob.vehicles)), integer=True)
            present = prob.present()
            for t in range(T):
                vs = present[t]
                if not vs or not contested(prob, vs):
                    continue
                self.contested_steps += 1
                group_cols: list[list[int]] = [[] for _ in prob.chargers]
                for v in vs:
                    us = []
                    caps = []
                    for g, (_, kw) in enumerate(prob.chargers):
                        u = m.var(0.0, 1.0, integer=True)
                        us.append(u)
                        caps.append(-min(kw, prob.vehicles[v].max_kw))
                        group_cols[g].append(u)
                    m.row(us, [1.0] * len(us), -INF, 1.0)  # one charger at a time
                    m.row([self.p[(v, t)]] + us, [1.0] + caps, -INF, 0.0)  # no charger, no power
                for g, (count, _) in enumerate(prob.chargers):
                    cols = group_cols[g]
                    if g == 0 and self.x_chg is not None:
                        m.row(cols + [self.x_chg], [1.0] * len(cols) + [-1.0], -INF, float(count))
                    elif len(cols) > count:
                        m.row(cols, [1.0] * len(cols), -INF, float(count))

    # ---- objectives -----------------------------------------------------------
    def cost_objective(self) -> dict[int, float]:
        prob = self.prob
        T = max(prob.n_steps, 1)
        V = max(len(prob.vehicles), 1)
        # tie-breaker weight: vehicles that leave first charge first (gives
        # each vehicle one block of charging instead of many small pieces)
        order = sorted(range(len(prob.vehicles)), key=lambda v: (prob.vehicles[v].stops[0].departure_min, v))
        weight = {v: 2.0 - rank / V for rank, v in enumerate(order)}
        c: dict[int, float] = {}
        for (v, t), j in self.p.items():
            c[j] = float(prob.price[t]) * prob.dt + EARLY_TIEBREAK * weight[v] * (t + 1) / T * prob.dt
        c[self.peak] = prob.demand_rate if prob.demand_rate > 0 else FLAT_TIEBREAK
        return c

    def power(self, x: np.ndarray) -> np.ndarray:
        prob = self.prob
        out = np.zeros((len(prob.vehicles), prob.n_steps))
        for (v, t), j in self.p.items():
            val = float(x[j])
            out[v, t] = val if val > 1e-6 else 0.0
        return out


@dataclass
class OptimizeOutput:
    power: np.ndarray
    status: str
    optimal: bool
    gap: float | None
    seconds: float
    binaries: int
    variables: int
    rows: int
    stages: list[str]
    max_shortfall_kwh: float
    total_shortfall_kwh: float
    contested_steps: int


def optimize(prob: Problem, time_limit: float | None = None) -> OptimizeOutput:
    """Find the cheapest plan that gets every vehicle ready, or, if that is
    impossible, the plan that leaves the smallest shortfalls."""
    budget = float(time_limit if time_limit is not None else prob.time_limit_s)
    started = time.perf_counter()

    def remaining() -> float:
        return max(1.0, budget - (time.perf_counter() - started))

    model = ChargingModel(prob)
    m = model.m
    results: list[SolveResult] = []

    # Stage 1: smallest biggest-shortfall
    r1 = m.solve({model.z: 1.0}, time_limit=remaining())
    results.append(r1)
    if not r1.has_solution:
        raise BerthSolverError(
            f"the solver found no plan within {budget:g} s (status: {r1.status}). "
            "Try a larger time step or a longer time limit."
        )
    z1 = max(0.0, float(r1.x[model.z]))
    best = r1
    stages = ["readiness"]
    total_short = float(sum(r1.x[j] for j in model.s))

    if z1 <= TOL_KWH:
        bounds = {j: (0.0, TOL_KWH) for j in model.s}
        bounds[model.z] = (0.0, TOL_KWH)
        rows: list = []
        total_short = 0.0
    else:
        zcap = z1 + max(1e-4, 1e-6 * z1)
        bounds = {model.z: (0.0, zcap)}
        # Stage 2: smallest total shortfall, keeping the biggest one as small
        r2 = m.solve({j: 1.0 for j in model.s}, bounds=bounds, time_limit=remaining())
        results.append(r2)
        rows = []
        if r2.has_solution:
            best = r2
            stages.append("total shortfall")
            total_short = float(sum(r2.x[j] for j in model.s))
            rows = [(list(model.s), [1.0] * len(model.s), -INF, total_short + max(1e-3, 1e-6 * total_short))]

    # Stage 3: lowest cost with that readiness
    r3 = m.solve(model.cost_objective(), bounds=bounds, extra_rows=rows, time_limit=remaining(), mip_gap=5e-4)
    results.append(r3)
    if r3.has_solution:
        best = r3
        stages.append("cost")

    optimal = all(r.optimal for r in results)
    last = results[-1]
    status = "optimal" if optimal else f"stopped early ({last.status})"
    return OptimizeOutput(
        power=model.power(best.x),
        status=status,
        optimal=optimal,
        gap=best.gap,
        seconds=time.perf_counter() - started,
        binaries=m.n_integer,
        variables=m.n_vars,
        rows=len(m.row_lb),
        stages=stages,
        max_shortfall_kwh=z1,
        total_shortfall_kwh=total_short,
        contested_steps=model.contested_steps,
    )
