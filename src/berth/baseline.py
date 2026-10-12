"""The simple baseline: plug in and charge at full power right away.

Each vehicle charges at its full rate as soon as it arrives and stops when it
reaches its required level. Chargers and site power go first come, first
served (earlier arrivals first, fastest chargers first). This is what many
depots do today without smart charging, so the difference to the optimized
plan is the value of planning.
"""

from __future__ import annotations

import numpy as np

from berth.problem import Problem, StopData


def baseline_power(prob: Problem) -> np.ndarray:
    return baseline_schedule(prob)[0]


def baseline_schedule(prob: Problem) -> tuple[np.ndarray, dict[tuple[int, int], int]]:
    """Power per vehicle and step, and which charger group each vehicle
    used in each step: {(vehicle, step): group index}."""
    V, T = len(prob.vehicles), prob.n_steps
    power = np.zeros((V, T))
    assign: dict[tuple[int, int], int] = {}
    energy = [veh.arrival_kwh for veh in prob.vehicles]
    trips_applied = [0] * V  # stop number whose trip has been subtracted

    at: dict[tuple[int, int], StopData] = {}
    for v, veh in enumerate(prob.vehicles):
        for s in veh.stops:
            for t in s.steps:
                at[(v, t)] = s

    avail = prob.available_kw
    step_kwh = prob.efficiency * prob.dt
    for t in range(T):
        waiting: list[tuple[float, int, StopData]] = []
        for v, veh in enumerate(prob.vehicles):
            s = at.get((v, t))
            if s is None:
                continue
            while trips_applied[v] < s.number:  # entering a new stop: subtract trips on the way
                trips_applied[v] += 1
                energy[v] -= veh.stops[trips_applied[v] - 1].trip_kwh
            if s.required_kwh - energy[v] > 1e-9:
                waiting.append((s.arrival_min, v, s))
        waiting.sort(key=lambda w: (w[0], w[1]))
        slots = sorted(
            ((kw, g) for g, (count, kw) in enumerate(prob.chargers) for _ in range(min(count, len(waiting)))),
            key=lambda x: (-x[0], x[1]),
        )
        left = float(avail[t])
        for (_, v, s), (charger_kw, g) in zip(waiting, slots):
            assign[(v, t)] = g
            veh = prob.vehicles[v]
            need_kw = (s.required_kwh - energy[v]) / step_kwh
            p = max(0.0, min(veh.max_kw, charger_kw, need_kw, left))
            power[v, t] = p
            left -= p
            energy[v] += p * step_kwh
    return power, assign
