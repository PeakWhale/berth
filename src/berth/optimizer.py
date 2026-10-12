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
     when every vehicle can be ready both are zero. A vehicle that is not
     parked long enough gets as much as it can take, and the part it can
     never get does not count against the others;
  2. cost: energy cost (price x energy) plus demand charge (rate x peak kW);
  3. tidy: keep the cost at its lowest value and, among plans with that
     cost, prefer a lower peak and let the vehicles that leave first charge
     first. This gives each vehicle one block of charging where possible.
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

        # shortfall per stop (kWh) and the biggest avoidable shortfall z
        # (the part above what no plan can avoid because of too little time)
        self.unavoidable = prob.unavoidable_kwh()
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
                m.row([self.s[k], self.z], [1.0, -1.0], -INF, self.unavoidable[k])
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
        self.u_at: dict[int, list[tuple[int, list[int]]]] = {}  # step -> [(vehicle, [u per group])]
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
                self.u_at[t] = []
                for v in vs:
                    us = []
                    caps = []
                    for g, (_, kw) in enumerate(prob.chargers):
                        u = m.var(0.0, 1.0, integer=True)
                        us.append(u)
                        caps.append(-min(kw, prob.vehicles[v].max_kw))
                        group_cols[g].append(u)
                    self.u_at[t].append((v, us))
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
        """Energy cost plus demand charge (the real money)."""
        prob = self.prob
        c: dict[int, float] = {j: float(prob.price[t]) * prob.dt for (v, t), j in self.p.items()}
        if prob.demand_rate > 0:
            c[self.peak] = prob.demand_rate
        return c

    def tidy_objective(self) -> dict[int, float]:
        """Used only after cost is fixed: lower peak first, then vehicles that
        leave first charge first (one block of charging per vehicle)."""
        prob = self.prob
        T = max(prob.n_steps, 1)
        V = max(len(prob.vehicles), 1)
        order = sorted(range(len(prob.vehicles)), key=lambda v: (prob.vehicles[v].stops[0].departure_min, v))
        weight = {v: 2.0 - rank / V for rank, v in enumerate(order)}
        c: dict[int, float] = {j: 0.01 * weight[v] * (t + 1) / T * prob.dt for (v, t), j in self.p.items()}
        c[self.peak] = 1.0
        return c

    def fix_chargers(self, x: np.ndarray, steps: list[int] | None = None) -> dict[int, tuple[float, float]]:
        """Round a relaxed solution: in each busy step give the chargers to
        the vehicles with the most planned power (a slower charger when it is
        fast enough). Returns bounds that fix the on/off variables."""
        prob = self.prob
        fixed: dict[int, tuple[float, float]] = {}
        for t in steps if steps is not None else list(self.u_at):
            entries = self.u_at[t]
            free = [count for count, _ in prob.chargers]
            order = sorted(entries, key=lambda e: -float(x[self.p[(e[0], t)]]))
            for v, us in order:
                want = float(x[self.p[(v, t)]])
                choice = None
                if want > 1e-6:
                    open_groups = [g for g in range(len(free)) if free[g] > 0]
                    enough = [
                        g for g in open_groups
                        if min(prob.chargers[g][1], prob.vehicles[v].max_kw) >= want - 1e-6
                    ]
                    if enough:
                        choice = max(enough)  # slowest charger that is fast enough
                    elif open_groups:
                        choice = min(open_groups)  # fastest charger left
                if choice is not None:
                    free[choice] -= 1
                for g, u in enumerate(us):
                    val = 1.0 if g == choice else 0.0
                    fixed[u] = (val, val)
        return fixed

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


class _Stages:
    """Runs the solve stages and keeps track of time, gaps and status."""

    def __init__(self, model: ChargingModel, budget: float) -> None:
        self.model = model
        self.budget = budget
        self.started = time.perf_counter()
        self.optimal = True
        self.gap: float | None = None
        self.status = "optimal"
        self.last_fix: dict | None = None

    def remaining(self) -> float:
        return max(1.0, self.budget - (time.perf_counter() - self.started))

    def _note(self, r: SolveResult) -> None:
        if not r.optimal:
            self.optimal = False
            self.status = f"stopped early ({r.status})"

    def solve(self, objective, bounds, rows, gap: float, abs_gap: float, start=None) -> SolveResult:
        """One stage. With on/off variables, first try "relax and fix" (see
        _relax_and_fix). If that is within the gap of the relaxation bound we
        are done, with a proven gap; otherwise run the full mixed-integer
        solve, starting from the rounded plan when there is one."""
        m = self.model.m
        self.last_fix = None
        if self.model.u_at and self.model.x_chg is None:
            got = self._relax_and_fix(objective, bounds, rows, gap, abs_gap)
            if got is not None:
                fixed, fix, accepted = got
                if accepted:
                    self.last_fix = fix
                    return fixed
                start = fixed.x
        r = m.solve(objective, bounds=bounds, extra_rows=rows, time_limit=self.remaining(), mip_gap=gap, start=start)
        self._note(r)
        return r

    def _relax_and_fix(self, objective, bounds, rows, gap, abs_gap):
        """Solve the relaxation (chargers may be shared, a lower bound). Then,
        a few hours at a time, fix the chargers from it and solve the rest
        again, so later hours can make up for the rounding. Each relaxed
        solution is first re-solved with the tidy objective at the same value,
        which makes vehicles charge in blocks and rounds well."""
        m = self.model.m
        tidy = self.model.tidy_objective()

        def relaxed(extra_bounds):
            r = m.solve(objective, bounds={**bounds, **extra_bounds}, extra_rows=rows,
                        time_limit=self.remaining(), relax=True)
            if not r.has_solution:
                return None, None
            row = (list(objective), list(objective.values()), -INF,
                   r.objective + max(1e-7 * abs(r.objective), 1e-6))
            g = m.solve(tidy, bounds={**bounds, **extra_bounds}, extra_rows=rows + [row],
                        time_limit=self.remaining(), relax=True)
            return r, (g.x if g.has_solution else r.x)

        lp, x = relaxed({})
        if lp is None:
            return None
        bound = lp.objective
        steps = sorted(self.model.u_at)
        chunk = max(1, int(round(4 / self.model.prob.dt)))  # about 4 hours
        fix: dict[int, tuple[float, float]] = {}
        starts = list(range(0, len(steps), chunk))
        for i in starts[:-1]:
            if i > 0:
                r, x = relaxed(fix)
                if r is None:
                    return None
            fix.update(self.model.fix_chargers(x, steps[i:i + chunk]))
        # The last hours have nothing after them to make up for rounding.
        # Try rounding them too; if that loses too much, solve them exactly
        # (a small mixed-integer problem), then tidy them at the same value.
        last = steps[starts[-1]:]
        if starts[-1] > 0:
            r, x = relaxed(fix)
            if r is None:
                return None
        trial = {**fix, **self.model.fix_chargers(x, last)}
        fixed = m.solve(objective, bounds={**bounds, **trial}, extra_rows=rows, time_limit=self.remaining())
        if fixed.has_solution and fixed.objective <= bound + max(gap * abs(bound), abs_gap):
            fix = trial
        else:
            exact = m.solve(objective, bounds={**bounds, **fix}, extra_rows=rows,
                            time_limit=self.remaining(), mip_gap=gap)
            if not exact.has_solution:
                return None
            row = (list(objective), list(objective.values()), -INF,
                   exact.objective + max(1e-7 * abs(exact.objective), 1e-6))
            neat = m.solve(tidy, bounds={**bounds, **fix}, extra_rows=rows + [row],
                           time_limit=min(self.remaining(), 5.0), mip_gap=1e-3, start=exact.x)
            use = neat if neat.has_solution else exact
            for t in last:
                for _, us in self.model.u_at[t]:
                    for u in us:
                        val = float(round(use.x[u]))
                        fix[u] = (val, val)
            fixed = m.solve(objective, bounds={**bounds, **fix}, extra_rows=rows, time_limit=self.remaining())
            if not fixed.has_solution:
                return None
        accepted = fixed.objective <= bound + max(gap * abs(bound), abs_gap)
        fixed.gap = max(0.0, fixed.objective - bound) / max(abs(fixed.objective), 1e-9)
        fixed.optimal = accepted
        return fixed, fix, accepted


def optimize(prob: Problem, time_limit: float | None = None) -> OptimizeOutput:
    """Find the cheapest plan that gets every vehicle ready, or, if that is
    impossible, the plan that leaves the smallest shortfalls."""
    budget = float(time_limit if time_limit is not None else prob.time_limit_s)
    model = ChargingModel(prob)
    m = model.m
    run = _Stages(model, budget)
    stages: list[str] = []

    # Stage 1: smallest biggest-shortfall
    r1 = run.solve({model.z: 1.0}, {}, [], gap=1e-4, abs_gap=TOL_KWH)
    if not r1.has_solution:
        raise BerthSolverError(
            f"the solver found no plan within {budget:g} s (status: {r1.status}). "
            "Try a larger time step or a longer time limit."
        )
    best = r1
    stages.append("readiness")
    z1 = max(0.0, float(r1.x[model.z]))
    total_short = float(sum(r1.x[j] for j in model.s))
    rows: list = []
    if z1 <= TOL_KWH:
        bounds = {j: (0.0, u + TOL_KWH) for j, u in zip(model.s, model.unavoidable)}
        bounds[model.z] = (0.0, TOL_KWH)
        total_short = float(sum(model.unavoidable))
    else:
        bounds = {model.z: (0.0, z1 + max(1e-4, 1e-6 * z1))}
        # Stage 2: smallest total shortfall, keeping the biggest one as small
        r2 = run.solve({j: 1.0 for j in model.s}, bounds, [], gap=1e-4, abs_gap=TOL_KWH, start=best.x)
        if r2.has_solution:
            best = r2
            stages.append("total shortfall")
            total_short = float(sum(r2.x[j] for j in model.s))
            rows = [(list(model.s), [1.0] * len(model.s), -INF, total_short + max(1e-3, 1e-6 * total_short))]

    # Stage 3: lowest cost with that readiness
    cost = model.cost_objective()
    r3 = run.solve(cost, bounds, rows, gap=1e-3, abs_gap=1e-4, start=best.x)
    gap = r3.gap
    if r3.has_solution:
        best = r3
        stages.append("cost")
        # Stage 4: same cost, tidier schedule (lower peak, one block per vehicle)
        cost_now = sum(cost[j] * float(r3.x[j]) for j in cost)
        cost_row = (list(cost), list(cost.values()), -INF, cost_now + max(1e-4, 1e-7 * abs(cost_now)))
        tidy = model.tidy_objective()
        if run.last_fix is not None:  # chargers already fixed: tidy as a linear program
            r4 = m.solve(tidy, bounds={**bounds, **run.last_fix}, extra_rows=rows + [cost_row],
                         time_limit=run.remaining())
        else:
            r4 = m.solve(tidy, bounds=bounds, extra_rows=rows + [cost_row], mip_gap=1e-3, start=r3.x,
                         time_limit=min(run.remaining(), max(2.0, budget / 4)))
        if r4.has_solution:
            best = r4
            stages.append("tidy")

    return OptimizeOutput(
        power=model.power(best.x),
        status=run.status,
        optimal=run.optimal,
        gap=gap,
        seconds=time.perf_counter() - run.started,
        binaries=m.n_integer,
        variables=m.n_vars,
        rows=len(m.row_lb),
        stages=stages,
        max_shortfall_kwh=z1,
        total_shortfall_kwh=total_short,
        contested_steps=model.contested_steps,
    )
