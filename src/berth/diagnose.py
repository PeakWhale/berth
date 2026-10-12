"""Explain why vehicles fall short.

Three possible reasons:
  * time: even alone, at full power, for the whole stop, the vehicle cannot
    get the energy it needs (this part can never be fixed by the site);
  * site: the site power limit (after other building load) holds charging back;
  * chargers: there are too few chargers (or too few fast ones).

To tell site and chargers apart we solve the readiness problem again
without one of the limits and see if the shortfall goes down. When one change
alone fixes everything, we also compute how much is needed: the smallest
extra site kW, or the smallest number of extra chargers.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from berth.evaluate import READY_TOL_KWH, Metrics
from berth.optimizer import ChargingModel
from berth.problem import Problem

TIME, SITE, CHARGERS = "not_enough_time", "site_limit", "too_few_chargers"


@dataclass
class Diagnosis:
    codes: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    reasons: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    unavoidable_kwh: dict[tuple[str, int], float] = field(default_factory=dict)
    binding: list[str] = field(default_factory=list)
    extra_site_kw: float | None = None
    extra_chargers: int | None = None
    extra_proven: bool = True  # False: the amounts are enough, but smaller ones may do
    extra_charger_kw: float | None = None
    both_needed: bool = False
    unchecked: bool = False  # no time was left for the "what if" solves
    summary: list[str] = field(default_factory=list)


def _min_total_shortfall(prob: Problem, time_limit: float, **flags) -> float | None:
    model = ChargingModel(prob, **flags)
    r = model.m.solve({j: 1.0 for j in model.s}, time_limit=time_limit, mip_gap=1e-3, abs_gap=0.05)
    if not r.has_solution:
        return None
    return float(sum(r.x[j] for j in model.s))


def _min_extra(
    prob: Problem, unavoidable: list[float], what: str, time_limit: float, other_limit: bool = True
) -> tuple[float, bool] | None:
    """Smallest extra site kW (or extra chargers) so that every vehicle gets
    all it can, and whether it is proven smallest (the solve may stop at its
    time limit with a larger amount that is still enough). other_limit=False
    removes the other limit at the same time."""
    model = ChargingModel(
        prob,
        extra_site=(what == "site"),
        extra_chargers=(what == "chargers"),
        site_limit=(what == "site") or other_limit,
        charger_limit=(what == "chargers") or other_limit,
    )
    var = model.x_site if what == "site" else model.x_chg
    bounds = {j: (0.0, u + 0.005) for j, u in zip(model.s, unavoidable)}
    r = model.m.solve({var: 1.0}, bounds=bounds, time_limit=time_limit, mip_gap=1e-3, abs_gap=0.5)
    if not r.has_solution:
        return None
    return float(r.x[var]), r.optimal


def _full_limits(prob: Problem, metrics: Metrics, short, unavoidable: dict) -> list[str]:
    """Without "what if" solves: the limits that are full while a vehicle
    that falls short (for more than lack of time) is parked."""
    present = prob.present()
    avail = prob.available_kw
    site = chargers = False
    for o in short:
        if o.shortfall_kwh - unavoidable[(o.stop.vehicle_id, o.stop.number)] <= READY_TOL_KWH:
            continue
        for t in o.stop.steps:
            site = site or metrics.total_kw[t] >= avail[t] - 1e-3
            chargers = chargers or len(present[t]) > prob.total_chargers
    found = [code for code, full in ((SITE, site), (CHARGERS, chargers)) if full]
    return found or [SITE, CHARGERS]


def diagnose(prob: Problem, metrics: Metrics, deadline: float) -> Diagnosis | None:
    """`deadline` (time.perf_counter() seconds) is the end of the plan's
    time limit. The "what if" solves only need rough answers: each gets at
    most 5 s and what is left until the deadline, with a small floor."""
    short = [o for o in metrics.stops if not o.ready]
    if not short:
        return None
    d = Diagnosis()
    present = prob.present()
    avail = prob.available_kw

    # 1. the part no site change can fix
    unavoidable: list[float] = []
    alone: dict[tuple[str, int], float] = {}
    for v, veh in enumerate(prob.vehicles):
        for s, best in zip(veh.stops, prob.alone_max_kwh(v)):
            u = max(0.0, s.required_kwh - best)
            unavoidable.append(u)
            d.unavoidable_kwh[(veh.id, s.number)] = u
            alone[(veh.id, s.number)] = best

    # 2. which shared limit holds the rest back
    total_short = sum(o.shortfall_kwh for o in metrics.stops)
    total_unavoidable = sum(unavoidable)
    avoidable = sum(max(0.0, o.shortfall_kwh - d.unavoidable_kwh[(o.stop.vehicle_id, o.stop.number)]) for o in short)

    def budget() -> float:  # for the next "what if" solve
        return min(5.0, max(0.25, deadline - time.perf_counter()))

    if avoidable > READY_TOL_KWH:
        # A limit holds charging back if removing it lowers the smallest
        # possible total shortfall. Compare with that smallest total under all
        # limits, not with this plan's total: a plan that gets the most
        # vehicles ready (or shares the shortfall) can have a bigger total.
        w_all = _min_total_shortfall(prob, budget())
        ref = min(total_short, w_all) if w_all is not None else total_short
        margin = max(0.1, 0.002 * ref)  # the what-if solves are rough (small gaps)
        w_site = _min_total_shortfall(prob, budget(), site_limit=False)
        w_chg = _min_total_shortfall(prob, budget(), charger_limit=False)
        site_fixes = chg_fixes = False
        if w_site is None or w_chg is None:
            # no time left for the "what if" solves: say which limits are full in this plan
            d.unchecked = True
            d.binding = _full_limits(prob, metrics, short, d.unavoidable_kwh)
        else:
            if w_site < ref - margin:
                d.binding.append(SITE)
            if w_chg < ref - margin:
                d.binding.append(CHARGERS)
            site_fixes = w_site <= total_unavoidable + READY_TOL_KWH
            chg_fixes = w_chg <= total_unavoidable + READY_TOL_KWH
            if not (site_fixes or chg_fixes):
                d.both_needed = True
                d.binding = [SITE, CHARGERS]
        # how much more is needed (with the other limit as it is, or, when
        # both are needed, with the other limit removed)
        if site_fixes or d.both_needed:
            got = _min_extra(prob, unavoidable, "site", budget(), other_limit=not d.both_needed)
            if got is not None:
                d.extra_site_kw = float(math.ceil(got[0] - 1e-6))
                d.extra_proven = d.extra_proven and got[1]
        if chg_fixes or d.both_needed:
            got = _min_extra(prob, unavoidable, "chargers", budget(), other_limit=not d.both_needed)
            if got is not None:
                d.extra_chargers = int(round(got[0]))
                d.extra_proven = d.extra_proven and got[1]
                d.extra_charger_kw = prob.max_charger_kw

    # 3. reasons per short stop, in plain words
    for o in short:
        s = o.stop
        key = (s.vehicle_id, s.number)
        veh = prob.vehicles[s.vehicle]
        codes: list[str] = []
        text: list[str] = []
        u = d.unavoidable_kwh[key]
        hours = len(s.steps) * prob.dt
        if u > READY_TOL_KWH:
            codes.append(TIME)
            best = alone[key]
            text.append(
                f"Not enough time: it is parked {hours:.2f} h. Even charging alone at "
                f"{prob.charge_rate(s.vehicle):g} kW the whole time, it reaches only "
                f"{best / veh.battery_kwh * 100:.0f}% ({best:.1f} kWh); it needs "
                f"{s.required_kwh / veh.battery_kwh * 100:.0f}% ({s.required_kwh:.1f} kWh)."
            )
        if o.shortfall_kwh - u > READY_TOL_KWH:
            steps = list(s.steps)
            crowd = max((len(present[t]) for t in steps), default=0)
            if SITE in d.binding:
                codes.append(SITE)
                lo = min((avail[t] for t in steps), default=0.0)
                hi = max((avail[t] for t in steps), default=0.0)
                span = f"{lo:g} kW" if abs(hi - lo) < 1e-9 else f"{lo:g} to {hi:g} kW"
                text.append(
                    f"Site power limit: while it is parked the site has only {span} for charging "
                    f"(limit minus other load), shared by up to {crowd} parked vehicles."
                )
            if CHARGERS in d.binding:
                codes.append(CHARGERS)
                if crowd > prob.total_chargers:
                    text.append(
                        f"Too few chargers: up to {crowd} vehicles are parked at the same time, "
                        f"but there are only {prob.total_chargers} chargers."
                    )
                else:
                    text.append(
                        "Too few fast chargers: there are not enough chargers at this vehicle's "
                        f"full rate ({prob.charge_rate(s.vehicle):g} kW) for everyone parked."
                    )
        d.codes[key] = codes
        d.reasons[key] = text

    # 4. fleet summary
    n_short = len({o.stop.vehicle_id for o in short})
    d.summary.append(
        f"{n_short} of {len(prob.vehicles)} vehicles cannot be fully ready; "
        f"{sum(o.shortfall_kwh for o in short):.1f} kWh are missing in total."
    )
    timed = sorted({k[0] for k, u in d.unavoidable_kwh.items() if u > READY_TOL_KWH})
    if timed:
        d.summary.append(
            f"{', '.join(timed)}: not parked long enough to be ready even with unlimited site power "
            "and chargers. Move the departure later, plan an earlier arrival, or use a faster charger."
        )
    if d.unchecked:
        d.summary.append(
            "Berth ran out of time before it could check what would help, so the reasons above only say which "
            "limits are full in this plan. Raise time_limit_s for a closer look."
        )
    if d.both_needed:
        d.summary.append("Neither more site power alone nor more chargers alone is enough: both hold charging back.")
        if d.extra_site_kw is not None:
            d.summary.append(f"With enough chargers, about {d.extra_site_kw:g} kW more site capacity would be enough.")
        if d.extra_chargers is not None:
            d.summary.append(
                f"With enough site power, {d.extra_chargers} more {d.extra_charger_kw:g} kW charger(s) would be enough."
            )
    else:
        if d.extra_site_kw is not None:
            d.summary.append(
                f"About {d.extra_site_kw:g} kW more site capacity (or that much less other load at the busy "
                "times) would be enough for every vehicle that has enough time."
            )
        if d.extra_chargers is not None:
            d.summary.append(
                f"{d.extra_chargers} more {d.extra_charger_kw:g} kW charger(s) would be enough for every "
                "vehicle that has enough time."
            )
    if (d.extra_site_kw is not None or d.extra_chargers is not None) and not d.extra_proven:
        d.summary.append("The solver ran out of time while working out these amounts, so less may be enough.")
    return d
