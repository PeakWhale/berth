"""plan(): the one function behind the CLI, API, web page and MCP server."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import ValidationError

from berth.baseline import baseline_power
from berth.diagnose import diagnose
from berth.errors import BerthInputError, format_validation_error
from berth.evaluate import Metrics, evaluate
from berth.models import Fleet
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


def plan(fleet: Fleet | dict, time_limit: float | None = None) -> PlanResult:
    """Plan charging for a fleet. Raises BerthInputError for bad input."""
    fleet = parse_fleet(fleet)
    prob = build_problem(fleet)
    limit = time_limit if time_limit is not None else prob.time_limit_s
    opt = optimize(prob, limit)
    m_opt = evaluate(prob, opt.power)
    base_power = baseline_power(prob)
    m_base = evaluate(prob, base_power)
    diag = diagnose(prob, m_opt, limit) if not m_opt.all_ready else None
    return _build_result(prob, opt, m_opt, base_power, m_base, diag)


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
        head = f"All {V} vehicles will be ready. "
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
    if not m_base.all_ready:
        notes.append(
            f"The baseline leaves {V - m_base.vehicles_ready} vehicle(s) short, so its cost covers less energy."
        )
    if not opt.optimal:
        gap = f" (gap {opt.gap * 100:.2f}%)" if opt.gap is not None else ""
        notes.append(
            f"The solver stopped at its time limit{gap}. The plan respects every limit but may not be "
            "the cheapest possible. Raise time_limit_s or use a longer time step for a better answer."
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
