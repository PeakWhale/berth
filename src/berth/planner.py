"""plan(): the one function behind the CLI, API, web page and MCP server."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import ValidationError

from berth.baseline import baseline_schedule
from berth.diagnose import diagnose
from berth.errors import BerthInputError, format_validation_error
from berth.evaluate import Metrics, evaluate
from berth.models import SHORTFALL_POLICIES, Fleet
from berth.optimizer import optimize
from berth.problem import Problem, build_problem
from berth.report import money
from berth.results import (
    ChargingWindow,
    DiagnosisResult,
    PlanResult,
    Savings,
    ShortItem,
    SolverInfo,
    StopResult,
    Totals,
    VehicleResult,
)


def parse_fleet(data: Any, source: str | None = None) -> Fleet:
    """Validate a dict (from YAML or JSON) into a Fleet with clear errors."""
    if isinstance(data, Fleet):
        return data
    if not isinstance(data, dict):
        raise BerthInputError("the fleet must be a mapping with site, tariff and vehicles", source)
    try:
        return Fleet.model_validate(data)
    except ValidationError as exc:
        raise BerthInputError(format_validation_error(exc, data), source) from None


def plan(
    fleet: Fleet | dict,
    time_limit: float | None = None,
    shortfall_policy: str | None = None,
) -> PlanResult:
    """Plan charging for a fleet. Raises BerthInputError for bad input.
    `shortfall_policy` ("most_ready" or "fair") overrides the fleet's own."""
    fleet = parse_fleet(fleet)
    if shortfall_policy is not None:
        fleet = with_policy(fleet, shortfall_policy)
    prob = build_problem(fleet)
    limit = time_limit if time_limit is not None else prob.time_limit_s
    base_power, base_assign = baseline_schedule(prob)
    opt = optimize(prob, limit, baseline=(base_power, base_assign))
    m_opt = evaluate(prob, opt.power)
    m_base = evaluate(prob, base_power)
    fallback = False
    if prob.shortfall_policy == "most_ready" and m_opt.vehicles_ready < m_base.vehicles_ready:
        # Should not happen (the solver starts from the baseline), but the
        # promise is: never fewer vehicles ready than charging on arrival.
        opt.power = base_power.copy()
        m_opt = m_base
        fallback = True
    diag = diagnose(prob, m_opt, limit) if not m_opt.all_ready else None
    result = _build_result(prob, opt, m_opt, base_power, m_base, diag)
    if fallback:
        result.notes.append(
            "The solver found no plan with as many vehicles ready as charging on arrival in its time limit, "
            "so this plan is charging on arrival. Raise time_limit_s to search longer."
        )
    return result


def with_policy(fleet: Fleet, policy: str) -> Fleet:
    """The same fleet with another shortfall policy (checked)."""
    if policy not in SHORTFALL_POLICIES:
        raise BerthInputError(
            f"shortfall_policy must be one of: {', '.join(SHORTFALL_POLICIES)} (got {policy!r})"
        )
    return fleet.model_copy(update={"shortfall_policy": policy})


def _r(x: float, nd: int = 4) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def _totals(m: Metrics) -> Totals:
    return Totals(
        energy_cost=_r(m.energy_cost),
        demand_cost=_r(m.demand_cost),
        total_cost=_r(m.total_cost),
        grid_kwh=_r(m.grid_kwh, 3),
        peak_charging_kw=_r(m.peak_charging_kw, 3),
        peak_site_kw=_r(m.peak_site_kw, 3),
        vehicles_ready=m.vehicles_ready,
        vehicles_total=m.vehicles_total,
        all_ready=m.all_ready,
        shortfall_kwh=_r(m.shortfall_kwh, 3),
        max_shortfall_kwh=_r(m.worst.shortfall_kwh, 3) if m.worst else 0.0,
    )


def _windows(prob: Problem, row: np.ndarray) -> list[ChargingWindow]:
    out: list[ChargingWindow] = []
    t = 0
    T = len(row)
    while t < T:
        if row[t] > 1e-3:
            t0 = t
            while t < T and row[t] > 1e-3:
                t += 1
            seg = row[t0:t]
            out.append(
                ChargingWindow(
                    start=prob.edge_labels[t0],
                    end=prob.edge_labels[t],
                    kwh=_r(seg.sum() * prob.dt, 2),
                    max_kw=_r(seg.max(), 2),
                )
            )
        else:
            t += 1
    return out


def _build_result(prob, opt, m_opt: Metrics, base_power, m_base: Metrics, diag) -> PlanResult:
    cur = prob.currency
    vehicles: list[VehicleResult] = []
    shortfalls: list[ShortItem] = []
    k = 0
    for v, veh in enumerate(prob.vehicles):
        stops: list[StopResult] = []
        for s in veh.stops:
            o = m_opt.stops[k]
            b = m_base.stops[k]
            k += 1
            key = (veh.id, s.number)
            codes = diag.codes.get(key, []) if diag else []
            reasons = diag.reasons.get(key, []) if diag else []
            stops.append(
                StopResult(
                    stop=s.number,
                    arrival=s.arrival_label,
                    departure=s.departure_label,
                    required_soc_pct=_r(s.required_soc_pct, 2),
                    required_kwh=_r(s.required_kwh, 3),
                    departure_kwh=_r(o.departure_kwh, 3),
                    departure_soc_pct=_r(o.departure_soc_pct, 2),
                    shortfall_kwh=_r(o.shortfall_kwh if not o.ready else 0.0, 3),
                    ready=o.ready,
                    baseline_departure_soc_pct=_r(b.departure_soc_pct, 2),
                    baseline_ready=b.ready,
                    reason_codes=codes,
                    reasons=reasons,
                )
            )
            if not o.ready:
                shortfalls.append(
                    ShortItem(
                        vehicle_id=veh.id,
                        stop=s.number,
                        departure=s.departure_label,
                        shortfall_kwh=_r(o.shortfall_kwh, 3),
                        reason_codes=codes,
                        reasons=reasons,
                    )
                )
        row = opt.power[v]
        vehicles.append(
            VehicleResult(
                id=veh.id,
                battery_kwh=veh.battery_kwh,
                max_kw=veh.max_kw,
                ready=m_opt.vehicle_ready[v],
                baseline_ready=m_base.vehicle_ready[v],
                shortfall_kwh=_r(sum(st.shortfall_kwh for st in stops), 3),
                grid_kwh=_r(row.sum() * prob.dt, 3),
                stops=stops,
                windows=_windows(prob, row),
                power_kw=[_r(x) for x in row],
            )
        )

    t_opt, t_base = _totals(m_opt), _totals(m_base)
    save = m_base.total_cost - m_opt.total_cost
    savings = Savings(
        cost=_r(save),
        cost_pct=_r(save / m_base.total_cost * 100, 2) if m_base.total_cost > 1e-9 else None,
        peak_kw=_r(m_base.peak_charging_kw - m_opt.peak_charging_kw, 3),
        peak_pct=_r((m_base.peak_charging_kw - m_opt.peak_charging_kw) / m_base.peak_charging_kw * 100, 2)
        if m_base.peak_charging_kw > 1e-9
        else None,
    )

    V = len(prob.vehicles)
    if m_opt.all_ready:
        head = "The vehicle will be ready. " if V == 1 else f"All {V} vehicles will be ready. "
    else:
        n_short = V - m_opt.vehicles_ready
        head = (
            f"{n_short} of {V} vehicles cannot be fully ready ({m_opt.shortfall_kwh:.1f} kWh short); "
            "this is the best partial plan. "
        )
    head += (
        f"Cost {money(m_opt.total_cost, cur)} vs {money(m_base.total_cost, cur)} when every vehicle "
        f"charges at full power on arrival"
    )
    if savings.cost_pct is not None and save > 0.005:
        head += f" ({savings.cost_pct:.0f}% less)"
    head += f". Peak charging {m_opt.peak_charging_kw:.0f} kW vs {m_base.peak_charging_kw:.0f} kW."

    notes = list(prob.notes)
    notes.append(
        f"Chargers: in each time step at most {prob.total_chargers} vehicles charge at once "
        "(and at most the number of chargers in each power group). Berth does not pick the exact "
        "charger; if vehicles share chargers, someone has to move the plugs between time steps."
    )
    if prob.demand_rate > 0:
        days = min(prob.plan_days, prob.billing_days)
        notes.append(
            f"Demand charge: billed monthly at {money(prob.demand_rate_monthly, cur)} per kW of peak. "
            f"This plan counts {days:g}/{prob.billing_days:g} of it, as if every day of the month has "
            "the same peak. Only the part of the peak caused by charging is counted."
        )
    policy = prob.shortfall_policy
    if not m_opt.all_ready:
        if policy == "most_ready":
            notes.append(
                "Not every vehicle can be ready. Berth first gets as many vehicles fully ready as it can "
                f"({m_opt.vehicles_ready} of {V}), then keeps the biggest shortfall of the others as small as "
                "possible, then the total, and only then looks at cost (shortfall_policy: most_ready). "
                "Use shortfall_policy: fair to share the shortfall over all vehicles instead."
            )
        else:
            notes.append(
                "Not every vehicle can be ready. Berth shares the shortfall: it gives energy first to the "
                "vehicles that would be most short (the biggest shortfall as small as possible, then the "
                "total), and only then looks at cost (shortfall_policy: fair). Use shortfall_policy: "
                "most_ready to get the most vehicles fully ready instead."
            )
    if not m_base.all_ready:
        worst = m_base.worst
        text = (
            f"Charging on arrival leaves {V - m_base.vehicles_ready} vehicle(s) short "
            f"(biggest shortfall {worst.shortfall_kwh:.1f} kWh, {worst.stop.vehicle_id})"
        )
        if m_base.grid_kwh < m_opt.grid_kwh - 0.05:
            text += (
                f", and it uses less energy ({m_base.grid_kwh:.1f} vs {m_opt.grid_kwh:.1f} kWh), "
                "so its cost is not directly comparable"
            )
        notes.append(text + ".")
        if m_base.vehicles_ready > m_opt.vehicles_ready:
            notes.append(
                f"Charging on arrival gets more vehicles fully ready ({m_base.vehicles_ready} vs "
                f"{m_opt.vehicles_ready}) by leaving others far behind; Berth keeps the biggest shortfall at "
                f"{m_opt.worst.shortfall_kwh:.1f} kWh instead."
            )
        elif m_opt.vehicles_ready > m_base.vehicles_ready and m_opt.worst and worst:
            text = (
                f"Berth gets more vehicles fully ready than charging on arrival "
                f"({m_opt.vehicles_ready} vs {m_base.vehicles_ready})"
            )
            if m_opt.worst.shortfall_kwh > worst.shortfall_kwh + 0.05:
                text += (
                    f"; the price is a bigger shortfall on {m_opt.worst.stop.vehicle_id} "
                    f"({m_opt.worst.shortfall_kwh:.1f} kWh vs {worst.shortfall_kwh:.1f} kWh at most)"
                )
            notes.append(text + ".")
    if opt.optimal and opt.gap is not None and opt.gap > 1e-3:
        notes.append(
            f"The cost of this plan is proven to be within {opt.gap * 100:.2f}% of the lowest possible cost "
            "(sharing few chargers between many vehicles is a hard puzzle to solve exactly)."
        )
    if not opt.optimal:
        gap = f" (gap {opt.gap * 100:.2f}%)" if opt.gap is not None else ""
        notes.append(
            f"The solver stopped at a time limit{gap}. The plan respects every limit, but it is not proven "
            "to be the best possible (readiness of the vehicles that fall short, or cost). Raise "
            "time_limit_s or use a longer time step for a better answer."
        )
    if opt.contested_steps:
        notes.append(
            f"There are more parked vehicles than chargers in {opt.contested_steps} time step(s), "
            "so the plan also decides which vehicles charge in those steps."
        )

    return PlanResult(
        name=prob.name,
        currency=cur,
        tariff=prob.tariff_name,
        step_minutes=prob.step_minutes,
        headline=head,
        all_ready=m_opt.all_ready,
        shortfall_policy=policy,
        times=list(prob.labels),
        end=prob.edge_labels[-1],
        price=[_r(x, 5) for x in prob.price],
        site_limit_kw=[_r(x, 3) for x in prob.limit_kw],
        other_load_kw=[_r(x, 3) for x in prob.other_kw],
        available_kw=[_r(x, 3) for x in prob.available_kw],
        total_charging_kw=[_r(x) for x in m_opt.total_kw],
        baseline_charging_kw=[_r(x) for x in m_base.total_kw],
        chargers=[{"count": c, "kw": kw} for c, kw in prob.chargers],
        optimized=t_opt,
        baseline=t_base,
        savings=savings,
        vehicles=vehicles,
        shortfalls=shortfalls,
        diagnosis=DiagnosisResult(
            binding_limits=diag.binding,
            extra_site_kw=diag.extra_site_kw,
            extra_chargers=diag.extra_chargers,
            extra_charger_kw=diag.extra_charger_kw,
            summary=diag.summary,
        )
        if diag
        else None,
        notes=notes,
        solver=SolverInfo(
            status=opt.status,
            optimal=opt.optimal,
            mip_gap=opt.gap,
            seconds=_r(opt.seconds, 3),
            variables=opt.variables,
            constraints=opt.rows,
            integer_variables=opt.binaries,
            stages=opt.stages,
        ),
    )
