"""The charging optimizer (linear / mixed-integer program, solved with HiGHS).

Decision: the charging power of every vehicle in every time step.

Limits:
  * a vehicle charges only while it is parked;
  * power <= the vehicle's max rate and <= the fastest charger;
  * all charging together <= site limit minus other building load;
  * the battery never goes above 100 % and never below empty: a vehicle
    that leaves with less than its next trip needs arrives empty (where
    that depends on the plan, an on/off variable per stop decides);
  * chargers: in each time step at most N vehicles can charge, and at most
    `count` of them on each charger group (with its kW). Berth does not track
    which exact charger a vehicle uses. If a step has more parked vehicles
    than chargers, on/off variables are added for that step only, which
    turns the model into a mixed-integer program. If there are enough
    chargers, the model stays a fast linear program.

Goal, in order:
  1. readiness. When every vehicle can be ready, it is. When not:
       - shortfall_policy "most_ready" (default): first get as many vehicles
         fully ready as possible (an on/off variable per vehicle counts
         them), then, keeping that number, make the biggest shortfall (kWh
         missing at a departure) as small as possible, then the total;
       - shortfall_policy "fair": make the biggest shortfall as small as
         possible over all vehicles, then the total.
     A vehicle that is not parked long enough gets as much as it can take,
     and the part it can never get does not count against the others;
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
from berth.problem import Problem, arrival_level
from berth.solver import INF, LinearModel, SolveResult

TOL_KWH = 1e-3
READY_KWH = 0.005  # a stop counts as ready in the model when at most this short (reports use 0.01)
COST_GAP = 0.01  # stop when the cost is proven within 1 % of the lowest possible
# The tidy step only makes the schedule neater at the same cost, so it gets a
# small search. A node limit (not only a time limit) keeps the result the
# same from run to run on a busy or slow computer.
TIDY_NODES = 20
TIDY_SECONDS = 5.0


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
        self.r: list[int] = []  # vehicle ready on/off variables (most_ready policy only)

        # energy: battery level at each departure, written as `base` plus
        # sum(vals * cols). The level at an arrival is the previous level
        # minus the trip, but never below empty (see Problem.trip_modes).
        coef = prob.efficiency * prob.dt
        self.modes = prob.trip_modes()
        self.arrive: dict[int, tuple[int, int]] = {}  # stop -> (arrival level e, on/off b), "choose" stops only
        k = 0
        for v, veh in enumerate(prob.vehicles):
            cols: list[int] = []
            vals: list[float] = []
            base = veh.arrival_kwh
            for s in veh.stops:
                mode = self.modes[k]
                if mode == "carry":
                    base -= s.trip_kwh
                elif mode == "empty":
                    cols, vals, base = [], [], 0.0
                elif mode == "choose":
                    # e = max(0, x) with x = previous level - trip: b = 1 means
                    # it had enough for the trip (e = x), b = 0 that it ran out (e = 0)
                    big = veh.battery_kwh
                    e = m.var(0.0, big)
                    b = m.var(0.0, 1.0, integer=True)
                    neg = [-c for c in vals]
                    m.row([e] + cols, [1.0] + neg, base - s.trip_kwh, INF)  # e >= x
                    m.row([e] + cols + [b], [1.0] + neg + [big], -INF, base - s.trip_kwh + big)  # e <= x if b = 1
                    m.row([e, b], [1.0, -big], -INF, 0.0)  # e = 0 if b = 0
                    self.arrive[k] = (e, b)
                    cols, vals, base = [e], [1.0], 0.0
                here = [self.p[(v, t)] for t in s.steps]
                cols = cols + here
                vals = vals + [coef] * len(here)
                m.row(cols, vals, -INF, veh.battery_kwh - base)  # never above 100 %
                m.row(cols + [self.s[k]], vals + [1.0], s.required_kwh - base, INF)  # ready (or short)
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

    def add_ready_vars(self) -> None:
        """One on/off variable per vehicle: 1 means it is ready at every stop.
        Used by the most_ready policy to count ready vehicles. A vehicle that
        can never be ready (not parked long enough) is fixed at 0."""
        prob, m = self.prob, self.m
        self.r = []
        k = 0
        for v, veh in enumerate(prob.vehicles):
            n = len(veh.stops)
            can = all(u <= READY_KWH for u in self.unavoidable[k : k + n])
            r = m.var(0.0, 1.0 if can else 0.0, integer=True)
            least = prob.min_level_kwh(v)
            for i, st in enumerate(veh.stops):
                big = max(0.0, st.required_kwh - least[i])  # largest possible shortfall at this stop
                if big > READY_KWH:
                    # ready (r = 1): shortfall <= READY_KWH; not ready: no limit
                    m.row([self.s[k + i], r], [1.0, big], -INF, big + READY_KWH)
            self.r.append(r)
            k += n

    def trips_made(self) -> dict[int, tuple[float, float]]:
        """Bounds that say: every vehicle has enough for each trip after a
        stop where it can be ready (only stops where this is not certain
        have a variable for it)."""
        return {b: (1.0, 1.0) for k, (_, b) in self.arrive.items() if self.unavoidable[k - 1] <= READY_KWH}

    def assign_from(self, x: np.ndarray) -> dict[tuple[int, int], int]:
        """Charger group used by each vehicle in each busy step, read from a solution."""
        out: dict[tuple[int, int], int] = {}
        for t, entries in self.u_at.items():
            for v, us in entries:
                for g, u in enumerate(us):
                    if x[u] > 0.5:
                        out[(v, t)] = g
        return out

    def complete(self, x: np.ndarray) -> np.ndarray:
        """A full, consistent variable vector for the schedule in x (used after
        variables were added to the model)."""
        return self.vector_from(self.power(x), self.assign_from(x))

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

    def vector_from(self, power: np.ndarray, assign: dict[tuple[int, int], int]) -> np.ndarray:
        """All model variables for a given schedule (used to start the solver
        from the charge-on-arrival baseline)."""
        prob = self.prob
        m = self.m
        x = np.zeros(m.n_vars)
        for (v, t), j in self.p.items():
            x[j] = power[v, t]
        coef = prob.efficiency * prob.dt
        k = 0
        z = 0.0
        for v, veh in enumerate(prob.vehicles):
            level = veh.arrival_kwh
            for st in veh.stops:
                if k in self.arrive:
                    e, b = self.arrive[k]
                    x[e] = arrival_level(level, st.trip_kwh)
                    x[b] = 1.0 if level - st.trip_kwh >= -1e-9 else 0.0
                level = arrival_level(level, st.trip_kwh) + coef * float(power[v, st.a : st.d].sum())
                short = max(0.0, st.required_kwh - level)
                x[self.s[k]] = short
                z = max(z, short - self.unavoidable[k])
                k += 1
        x[self.z] = max(0.0, z)
        k = 0
        for v, veh in enumerate(prob.vehicles):
            n = len(veh.stops)
            if self.r:
                ok = all(x[self.s[k + i]] <= READY_KWH for i in range(n)) and self.m.ub[self.r[v]] > 0.5
                x[self.r[v]] = 1.0 if ok else 0.0
            k += n
        x[self.peak] = max(float(prob.other_kw.max()), float((prob.other_kw + power.sum(axis=0)).max()))
        for t, entries in self.u_at.items():
            for v, us in entries:
                g = assign.get((v, t))
                for gi, u in enumerate(us):
                    x[u] = 1.0 if gi == g else 0.0
        return x

    def objective_value(self, objective: dict[int, float], x: np.ndarray) -> float:
        return float(sum(c * x[j] for j, c in objective.items()))

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
    policy: str = "most_ready"
    ready_target: int | None = None  # most_ready: vehicles the plan gets fully ready (None if not needed)


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
        self.stage_end: float | None = None  # end of the current stage's time share
        self.neat = True  # tidy the rounded last hours (only worth it for the cost stage)

    def remaining(self) -> float:
        now = time.perf_counter()
        left = self.budget - (now - self.started)
        if self.stage_end is not None:
            left = min(left, self.stage_end - now)
        return max(1.0, left)

    def note(self, r: SolveResult) -> None:
        if not r.optimal:
            self.optimal = False
            self.status = f"stopped early ({r.status})"

    def solve(self, objective, bounds, rows, gap: float, abs_gap: float, start=None,
              time_cap: float | None = None, note: bool = True, neat: bool = False) -> SolveResult:
        """One stage. With on/off variables, first try "relax and fix" (see
        _relax_and_fix). If that is within the gap of the relaxation bound we
        are done, with a proven gap; otherwise run the full mixed-integer
        solve, starting from the rounded plan when there is one. `time_cap`
        limits the whole stage; with note=False stopping early is not
        reported (for a stage whose exact answer is not needed). `neat`
        tidies the rounded last hours (used for the cost stage)."""
        self.stage_end = None if time_cap is None else time.perf_counter() + time_cap
        self.neat = neat
        try:
            return self._solve(objective, bounds, rows, gap, abs_gap, start, note)
        finally:
            self.stage_end = None

    def _solve(self, objective, bounds, rows, gap, abs_gap, start, note) -> SolveResult:
        m = self.model.m
        self.last_fix = None
        start_value = self.model.objective_value(objective, start) if start is not None else None
        if self.model.u_at and self.model.x_chg is None:
            got = self._relax_and_fix(objective, bounds, rows, gap, abs_gap)
            if got is not None:
                fixed, fix, accepted = got
                if accepted and (start_value is None or fixed.objective <= start_value + 1e-9):
                    self.last_fix = fix
                    return fixed
                if start_value is None or fixed.objective < start_value:
                    start = fixed.x
        if not m.n_integer:
            start = None  # a linear program needs no start
        r = m.solve(objective, bounds=bounds, extra_rows=rows, time_limit=self.remaining(), mip_gap=gap,
                    abs_gap=abs_gap, start=start)
        if note:
            self.note(r)
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

        t0 = time.perf_counter()
        lp, x0 = relaxed({})
        if lp is None:
            return None
        bound = lp.objective
        one_pass = time.perf_counter() - t0
        steps = sorted(self.model.u_at)
        sizes = [max(1, int(round(4 / self.model.prob.dt)))]  # about 4 hours at a time
        if sizes[0] > 1:
            sizes.append(1)  # if that fails: one time step at a time (slower, rounds better)
        best = None
        for chunk in sizes:
            if chunk == 1 and one_pass * len(steps) > self.remaining() / 2:
                break  # not enough time left for the slow pass
            got = self._round(objective, bounds, rows, gap, abs_gap, relaxed, x0, bound, steps, chunk, tidy)
            if got is None:
                continue
            if got[2]:
                return got
            if best is None or got[0].objective < best[0].objective:
                best = got
        return best

    def _round(self, objective, bounds, rows, gap, abs_gap, relaxed, x, bound, steps, chunk, tidy):
        m = self.model.m
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
        if fixed.has_solution and fixed.objective <= bound + max(min(gap, 1e-3) * abs(bound), abs_gap):
            fix = trial
        else:
            exact = m.solve(objective, bounds={**bounds, **fix}, extra_rows=rows,
                            time_limit=self.remaining(), mip_gap=gap, abs_gap=abs_gap)
            if not exact.has_solution:
                return None
            use = exact
            if self.neat:
                row = (list(objective), list(objective.values()), -INF,
                       exact.objective + max(1e-7 * abs(exact.objective), 1e-6))
                neat = m.solve(tidy, bounds={**bounds, **fix}, extra_rows=rows + [row], mip_gap=1e-3,
                               max_nodes=TIDY_NODES, time_limit=min(self.remaining(), TIDY_SECONDS),
                               start=exact.x)
                if neat.has_solution:
                    use = neat
            for t in last:
                for _, us in self.model.u_at[t]:
                    for u in us:
                        val = float(round(use.x[u]))
                        fix[u] = (val, val)
            fixed = m.solve(objective, bounds={**bounds, **fix}, extra_rows=rows, time_limit=self.remaining())
            if not fixed.has_solution:
                return None
        accepted = fixed.objective <= bound + max(min(gap, 1e-3) * abs(bound), abs_gap)
        fixed.gap = max(0.0, fixed.objective - bound) / max(abs(fixed.objective), 1e-9)
        fixed.optimal = accepted
        return fixed, fix, accepted


def optimize(
    prob: Problem,
    time_limit: float | None = None,
    baseline: tuple[np.ndarray, dict[tuple[int, int], int]] | None = None,
) -> OptimizeOutput:
    """Find the cheapest plan that gets every vehicle ready, or, if that is
    impossible, the plan that leaves the smallest shortfalls.

    The charge-on-arrival baseline is a valid plan, so the solver starts from
    it: even if the time limit stops the search, the plan is never less ready
    than the baseline (fewer vehicles fully ready with the most_ready
    policy, a bigger avoidable shortfall with the fair policy) and, when the
    baseline gets everyone ready, never more expensive than the baseline."""
    budget = float(time_limit if time_limit is not None else prob.time_limit_s)
    policy = prob.shortfall_policy
    model = ChargingModel(prob)
    m = model.m
    run = _Stages(model, budget)
    stages: list[str] = []
    if baseline is None:
        from berth.baseline import baseline_schedule

        baseline = baseline_schedule(prob)
    x_base = model.vector_from(*baseline)

    # Stage 1: smallest biggest-shortfall. With most_ready this only has to
    # tell whether every vehicle that has time can be ready (z = 0), so it
    # gets a share of the time; if it cannot tell, stage 1a below decides.
    # A vehicle that is ready at a stop has enough for the next trip, so when
    # everyone can be ready no vehicle runs out on the road. Try that first:
    # it keeps the model easy. Only if not everyone can be ready may a
    # vehicle run out (see ChargingModel.arrive).
    trips = model.trips_made()
    quick = policy == "most_ready"
    cap = max(2.0, 0.2 * budget) if quick else None
    r1 = run.solve({model.z: 1.0}, trips, [], gap=1e-4, abs_gap=0.01, start=x_base, time_cap=cap, note=False)
    if trips and not (r1.has_solution and r1.x[model.z] <= TOL_KWH):
        trips = {}
        if not (quick and r1.has_solution):  # most_ready: stage 1a below decides
            again = run.solve({model.z: 1.0}, {}, [], gap=1e-4, abs_gap=0.01,
                              start=r1.x if r1.has_solution else x_base, time_cap=cap, note=not quick)
            if again.has_solution:
                r1 = again
    elif not quick:
        run.note(r1)
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
    ready_target: int | None = None

    if z1 > TOL_KWH and policy == "most_ready":
        # Not everyone can be ready. Stage 1a: as many vehicles fully ready as
        # possible. Start from whichever of the baseline and the stage 1 plan
        # has more ready vehicles, so the answer is never below the baseline.
        model.add_ready_vars()
        x_base = model.vector_from(*baseline)
        x1 = model.complete(r1.x)
        r1.x = x1  # the model has new variables; keep the stage 1 plan usable as a start
        count = {j: -1.0 for j in model.r}
        start = x1 if model.objective_value(count, x1) < model.objective_value(count, x_base) else x_base
        r0 = run.solve(count, {}, [], gap=0.0, abs_gap=0.999, start=start)  # whole vehicles: within 1 is exact
        if r0.has_solution:
            ready_target = int(round(sum(float(r0.x[j]) for j in model.r)))
            rows = [(list(model.r), [1.0] * len(model.r), ready_target - 0.5, INF)]
            best = r0
            stages.append("most vehicles ready")
            # Stage 1b: keeping that many ready, the smallest biggest-shortfall.
            # Proving the best answer here can take long (the count makes the
            # model harder), so 1b and 2 get a share of the time each.
            share = max(2.0, 0.15 * budget)
            r1b = run.solve({model.z: 1.0}, {}, rows, gap=1e-4, abs_gap=0.01, start=r0.x, time_cap=share)
            if r1b.has_solution:
                best = r1b
                z1 = max(0.0, float(r1b.x[model.z]))
                total_short = float(sum(r1b.x[j] for j in model.s))

    if z1 <= TOL_KWH:
        bounds = {j: (0.0, u + TOL_KWH) for j, u in zip(model.s, model.unavoidable)}
        bounds[model.z] = (0.0, TOL_KWH)
        bounds.update(trips)
        total_short = float(sum(model.unavoidable))
    else:
        bounds = {model.z: (0.0, z1 + max(1e-4, 1e-6 * z1))}
        # Stage 2: smallest total shortfall, keeping the biggest one as small
        r2 = run.solve({j: 1.0 for j in model.s}, bounds, rows, gap=1e-4, abs_gap=0.05, start=best.x,
                       time_cap=max(2.0, 0.15 * budget) if model.r else None)
        if r2.has_solution:
            best = r2
            stages.append("total shortfall")
            total_short = float(sum(r2.x[j] for j in model.s))
            rows = rows + [(list(model.s), [1.0] * len(model.s), -INF, total_short + max(1e-3, 1e-6 * total_short))]

    # Stage 3: lowest cost with that readiness (start from the cheaper of
    # the readiness plan and the baseline, when the baseline qualifies)
    cost = model.cost_objective()
    start = best.x
    base_ok = all(lo - 1e-9 <= x_base[j] <= hi + 1e-9 for j, (lo, hi) in bounds.items()) and not rows
    if base_ok and model.objective_value(cost, x_base) < model.objective_value(cost, start):
        start = x_base
    r3 = run.solve(cost, bounds, rows, gap=COST_GAP, abs_gap=0.01, start=start, neat=True)
    gap = r3.gap
    if r3.has_solution:
        best = r3
        stages.append("cost")
        # Stage 4: same cost, tidier schedule (lower peak, one block per vehicle)
        cost_now = sum(cost[j] * float(r3.x[j]) for j in cost)
        cost_row = (list(cost), list(cost.values()), -INF, cost_now + max(1e-4, 1e-7 * abs(cost_now)))
        tidy = model.tidy_objective()
        keep = {j: (float(round(r3.x[j])),) * 2 for j in model.r}  # same vehicles ready
        keep.update({b: (float(round(r3.x[b])),) * 2 for _, b in model.arrive.values()})  # same trips made
        if run.last_fix is not None:  # chargers already fixed: tidy as a linear program
            r4 = m.solve(tidy, bounds={**bounds, **keep, **run.last_fix}, extra_rows=rows + [cost_row],
                         time_limit=run.remaining())
        else:
            r4 = m.solve(tidy, bounds={**bounds, **keep}, extra_rows=rows + [cost_row], mip_gap=1e-3, start=r3.x,
                         max_nodes=TIDY_NODES, time_limit=min(run.remaining(), TIDY_SECONDS))
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
        policy=policy,
        ready_target=ready_target,
    )
