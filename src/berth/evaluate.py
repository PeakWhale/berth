"""Measure any charging schedule the same way: cost, peak, readiness, and
any broken limits. Used for both the optimized plan and the baseline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from berth.problem import Problem, StopData

READY_TOL_KWH = 0.01


@dataclass
class StopOutcome:
    stop: StopData
    battery_kwh: float
    departure_kwh: float
    shortfall_kwh: float

    @property
    def ready(self) -> bool:
        return self.shortfall_kwh <= READY_TOL_KWH

    @property
    def departure_soc_pct(self) -> float:
        return self.departure_kwh / self.battery_kwh * 100.0


@dataclass
class Metrics:
    energy_cost: float
    demand_cost: float
    grid_kwh: float
    battery_kwh: float
    peak_charging_kw: float
    peak_site_kw: float
    stops: list[StopOutcome]
    vehicle_ready: list[bool]
    total_kw: np.ndarray

    @property
    def total_cost(self) -> float:
        return self.energy_cost + self.demand_cost

    @property
    def vehicles_ready(self) -> int:
        return sum(self.vehicle_ready)

    @property
    def vehicles_total(self) -> int:
        return len(self.vehicle_ready)

    @property
    def all_ready(self) -> bool:
        return all(self.vehicle_ready)

    @property
    def shortfall_kwh(self) -> float:
        return sum(o.shortfall_kwh for o in self.stops if not o.ready)

    @property
    def worst(self) -> StopOutcome | None:
        """The stop with the biggest shortfall (None if all are ready)."""
        short = [o for o in self.stops if not o.ready]
        return max(short, key=lambda o: o.shortfall_kwh) if short else None


def evaluate(prob: Problem, power: np.ndarray) -> Metrics:
    total = power.sum(axis=0)
    site = prob.other_kw + total
    energy_cost = float((prob.price * total).sum() * prob.dt)
    peak_site = float(site.max()) if len(site) else 0.0
    # demand charge caused by charging: the peak above what other load sets anyway
    demand_cost = prob.demand_rate * max(0.0, peak_site - float(prob.other_kw.max()))
    outcomes: list[StopOutcome] = []
    ready: list[bool] = []
    for v, veh in enumerate(prob.vehicles):
        energy = veh.arrival_kwh
        ok = True
        for s in veh.stops:
            energy -= s.trip_kwh
            energy += prob.efficiency * prob.dt * float(power[v, s.a : s.d].sum())
            short = max(0.0, s.required_kwh - energy)
            o = StopOutcome(stop=s, battery_kwh=veh.battery_kwh, departure_kwh=energy, shortfall_kwh=short)
            ok = ok and o.ready
            outcomes.append(o)
        ready.append(ok)
    return Metrics(
        energy_cost=energy_cost,
        demand_cost=demand_cost,
        grid_kwh=float(total.sum() * prob.dt),
        battery_kwh=float(total.sum() * prob.dt * prob.efficiency),
        peak_charging_kw=float(total.max()) if len(total) else 0.0,
        peak_site_kw=peak_site,
        stops=outcomes,
        vehicle_ready=ready,
        total_kw=total,
    )


def find_violations(prob: Problem, power: np.ndarray, tol: float = 1e-3) -> list[str]:
    """Independent check of every physical limit. Returns plain-English
    problems; an empty list means the schedule is valid."""
    out: list[str] = []
    V, T = power.shape
    parked = np.zeros((V, T), dtype=bool)
    for v, veh in enumerate(prob.vehicles):
        for s in veh.stops:
            parked[v, s.a : s.d] = True
    avail = prob.available_kw
    slots_all = sorted((kw for count, kw in prob.chargers for _ in range(min(count, V))), reverse=True)
    for v, veh in enumerate(prob.vehicles):
        for t in range(T):
            p = power[v, t]
            if p < -tol:
                out.append(f"{veh.id} has negative power at {prob.labels[t]}")
            if p > tol and not parked[v, t]:
                out.append(f"{veh.id} charges at {prob.labels[t]} but is not parked")
            if p > veh.max_kw + tol:
                out.append(f"{veh.id} charges at {p:.2f} kW, above its max {veh.max_kw:g} kW, at {prob.labels[t]}")
            if p > prob.max_charger_kw + tol:
                out.append(f"{veh.id} charges at {p:.2f} kW, above the fastest charger, at {prob.labels[t]}")
        energy = veh.arrival_kwh
        for s in veh.stops:
            energy -= s.trip_kwh
            energy += prob.efficiency * prob.dt * float(power[v, s.a : s.d].sum())
            if energy > veh.battery_kwh + tol:
                out.append(f"{veh.id} goes above 100% battery at stop {s.number}")
    for t in range(T):
        col = power[:, t]
        if col.sum() > avail[t] + tol:
            out.append(
                f"site limit broken at {prob.labels[t]}: {col.sum():.2f} kW for charging, "
                f"{avail[t]:.2f} kW available"
            )
        active = sorted((p for p in col if p > tol), reverse=True)
        if len(active) > prob.total_chargers:
            out.append(f"{len(active)} vehicles charge at {prob.labels[t]} but there are {prob.total_chargers} chargers")
        else:
            for p, kw in zip(active, slots_all):
                if p > kw + tol:
                    out.append(f"not enough fast chargers at {prob.labels[t]} for the planned power")
                    break
    return out
