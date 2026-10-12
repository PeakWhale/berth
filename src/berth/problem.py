"""Turn a Fleet into numbers on a fixed time grid.

All times become minutes after the plan start. A vehicle can charge in a time
step only if it is parked for the whole step: arrival is rounded up and
departure is rounded down to the step grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from berth.errors import BerthInputError
from berth.models import Fleet, profile_value
from berth.timeutil import MINUTES_PER_DAY, clock_label, parse_time

MAX_HOURS = 168


@dataclass
class StopData:
    vehicle: int
    vehicle_id: str
    number: int  # 1-based
    a: int  # first step the vehicle is parked (inclusive)
    d: int  # first step after it leaves (exclusive)
    arrival_min: float
    departure_min: float
    arrival_label: str
    departure_label: str
    required_soc_pct: float
    required_kwh: float  # what the plan must deliver by departure (may be raised to cover the next trip)
    trip_kwh: float  # energy used on the road before this arrival (0 for the first stop)

    @property
    def steps(self) -> range:
        return range(self.a, self.d)


@dataclass
class VehicleData:
    id: str
    battery_kwh: float
    max_kw: float
    arrival_kwh: float
    stops: list[StopData]


@dataclass
class Problem:
    name: str
    currency: str
    tariff_name: str
    step_minutes: int
    n_steps: int
    labels: list[str]  # start of each step
    edge_labels: list[str]  # n_steps + 1 step boundaries
    price: np.ndarray  # per kWh, per step
    limit_kw: np.ndarray  # site import limit, per step
    other_kw: np.ndarray  # other building load, per step
    chargers: list[tuple[int, float]]  # (count, kW), fastest first
    efficiency: float
    demand_rate: float  # per kW of peak, for this plan (daily share of the monthly charge)
    demand_rate_monthly: float
    plan_days: int
    billing_days: float
    vehicles: list[VehicleData]
    time_limit_s: float
    notes: list[str] = field(default_factory=list)
    shortfall_policy: str = "most_ready"  # or "fair"; see Fleet.shortfall_policy

    @property
    def dt(self) -> float:
        """Hours per step."""
        return self.step_minutes / 60.0

    @property
    def available_kw(self) -> np.ndarray:
        """Power left for charging after other building load."""
        return np.maximum(0.0, self.limit_kw - self.other_kw)

    @property
    def stops(self) -> list[StopData]:
        return [s for v in self.vehicles for s in v.stops]

    @property
    def total_chargers(self) -> int:
        return sum(c for c, _ in self.chargers)

    @property
    def max_charger_kw(self) -> float:
        return max(kw for _, kw in self.chargers)

    def charge_rate(self, v: int) -> float:
        """Fastest power vehicle v can take from the fastest charger."""
        return min(self.vehicles[v].max_kw, self.max_charger_kw)

    def alone_max_kwh(self, v: int) -> list[float]:
        """Battery level at each departure if this vehicle charged alone at
        full power the whole time it is parked (no site limit, its own
        charger). Anything it needs above this can never be delivered."""
        veh = self.vehicles[v]
        rate = self.charge_rate(v)
        energy = veh.arrival_kwh
        out = []
        for s in veh.stops:
            energy -= s.trip_kwh
            energy = min(veh.battery_kwh, energy + self.efficiency * self.dt * rate * len(s.steps))
            out.append(energy)
        return out

    def unavoidable_kwh(self) -> list[float]:
        """Per stop (in order): the shortfall no plan can avoid (not enough time)."""
        out = []
        for v, veh in enumerate(self.vehicles):
            for s, best in zip(veh.stops, self.alone_max_kwh(v)):
                out.append(max(0.0, s.required_kwh - best))
        return out

    def present(self) -> list[list[int]]:
        """For each step, the vehicles that are parked."""
        out: list[list[int]] = [[] for _ in range(self.n_steps)]
        for v, veh in enumerate(self.vehicles):
            for s in veh.stops:
                for t in s.steps:
                    out[t].append(v)
        return out


def build_problem(fleet: Fleet) -> Problem:
    """Check times and build the numeric problem. Raises BerthInputError."""
    step = fleet.step_minutes
    errors: list[str] = []
    notes: list[str] = []

    # ---- time zones ------------------------------------------------------------
    # A full date and time may carry a UTC offset (like +02:00 or Z). Berth
    # plans on one clock: the offset of the plan start, or else of the first
    # time that has one. Times with another offset are converted to it, so
    # the time each vehicle is parked is right; times without an offset are
    # read on that clock.
    texts = [fleet.start] if fleet.start is not None else []
    texts += [t for veh in fleet.vehicles for st in veh.all_stops() for t in (st.arrival, st.departure)]
    offsets: list[timedelta] = []
    for text in texts:
        p = parse_time(text)
        if isinstance(p, datetime) and p.tzinfo is not None and p.utcoffset() not in offsets:
            offsets.append(p.utcoffset())
    plan_tz = timezone(offsets[0]) if offsets else None
    if len(offsets) > 1:
        notes.append(
            f"The times use different UTC offsets ({', '.join(_offset_text(o) for o in offsets)}), for example "
            f"across a daylight-saving change. Berth converted them to {_offset_text(offsets[0])} so the time "
            "each vehicle is parked is right. Prices and limits by time of day follow that clock."
        )

    def ptime(text: str) -> int | datetime:
        p = parse_time(text)
        if isinstance(p, datetime) and p.tzinfo is not None:
            p = p.astimezone(plan_tz).replace(tzinfo=None)
        return p

    # ---- work out the plan start ------------------------------------------
    parsed_first = []
    any_abs = False
    for veh in fleet.vehicles:
        for st in veh.all_stops():
            for t in (st.arrival, st.departure):
                if isinstance(ptime(t), datetime):
                    any_abs = True
        parsed_first.append(ptime(veh.all_stops()[0].arrival))

    start_dt: datetime | None = None
    if fleet.start is not None:
        s = ptime(fleet.start)
        if isinstance(s, datetime):
            start_dt = s
            start_clock = s.hour * 60 + s.minute
        else:
            if any_abs:
                raise BerthInputError(
                    "some vehicle times have a date, so start must be a full date and time too "
                    "(like 2026-03-02T16:00)"
                )
            start_clock = s
    else:
        if all(isinstance(p, datetime) for p in parsed_first):
            first = min(parsed_first)
            start_dt = first.replace(second=0, microsecond=0)
            start_dt -= timedelta(minutes=start_dt.minute % step)
            start_clock = start_dt.hour * 60 + start_dt.minute
        elif any_abs:
            raise BerthInputError(
                "vehicle times mix clock times (HH:MM) and full dates; please set start "
                "as a full date and time (like 2026-03-02T16:00)"
            )
        else:
            start_clock = _earliest_clock([p for p in parsed_first])
            start_clock -= start_clock % step
            notes.append(f"Plan starts at {clock_label(start_clock)} (the earliest arrival). Set 'start' to change it.")

    def label(minutes_from_start: float) -> str:
        if start_dt is not None:
            return (start_dt + timedelta(minutes=minutes_from_start)).strftime("%Y-%m-%d %H:%M")
        total = start_clock + int(round(minutes_from_start))
        day = total // MINUTES_PER_DAY
        return clock_label(total) + (f" +{day}d" if day > 0 else "")

    def resolve(text: str, not_before: float) -> float:
        p = ptime(text)
        if isinstance(p, datetime):
            return (p - start_dt).total_seconds() / 60.0  # type: ignore[operator]
        off = float((p - start_clock) % MINUTES_PER_DAY)
        if off < not_before:  # the first time this clock time comes after not_before
            off += math.ceil((not_before - off) / MINUTES_PER_DAY) * MINUTES_PER_DAY
        return off

    def too_late(minutes: float) -> bool:
        return minutes > MAX_HOURS * 60

    # ---- vehicles and stops -----------------------------------------------
    vehicles: list[VehicleData] = []
    for v_index, veh in enumerate(fleet.vehicles):
        who = f"vehicle {veh.id}"
        cap = veh.battery_kwh
        stops_in = veh.all_stops()
        stops: list[StopData] = []
        cursor = 0.0
        for k, st in enumerate(stops_in, start=1):
            where = who if len(stops_in) == 1 else f"{who}, stop {k}"
            arr = resolve(st.arrival, cursor if k > 1 else 0.0)
            if too_late(arr):
                errors.append(
                    f"{where}: arrival {st.arrival} is more than {MAX_HOURS} hours after the plan start "
                    f"({label(0)}). Check the date, or set start."
                )
                continue
            if k == 1 and arr < 0:
                notes.append(f"{who} arrives before the plan start; it is treated as parked from the start.")
                arr = 0.0
            if k == 1 and arr >= 20 * 60:
                notes.append(
                    f"{who} first arrives at {label(arr)}, almost a day after the plan start. "
                    "If it is already parked at the start, set its arrival to the start time."
                )
            if k > 1 and arr <= cursor - 1 + 1e-9:
                errors.append(f"{where}: arrival {st.arrival} is not after the previous departure")
                continue
            dep = resolve(st.departure, arr + 1)
            if dep <= 0:
                errors.append(
                    f"{where}: departure {st.departure} is at or before the plan start ({label(0)}), so it cannot "
                    "charge. Set start earlier or remove this stop."
                )
                continue
            if too_late(dep):
                errors.append(
                    f"{where}: departure {st.departure} is more than {MAX_HOURS} hours after the plan start "
                    f"({label(0)}). Check the date, or set start."
                )
                continue
            if dep <= arr:
                errors.append(f"{where}: departure {st.departure} must be after arrival {st.arrival}")
                continue
            trip = st.trip_kwh if k > 1 else 0.0
            if k == 1 and st.trip_kwh:
                notes.append(f"{who}: trip_kwh on the first stop is ignored (the arrival level already includes it).")
            if trip > cap:
                errors.append(f"{where}: trip_kwh {trip:g} is more than the battery ({cap:g} kWh)")
            a = math.ceil(arr / step - 1e-9)
            d = math.floor(dep / step + 1e-9)
            d = max(d, a)
            stops.append(
                StopData(
                    vehicle=v_index,
                    vehicle_id=veh.id,
                    number=k,
                    a=a,
                    d=d,
                    arrival_min=arr,
                    departure_min=dep,
                    arrival_label=label(arr),
                    departure_label=label(dep),
                    required_soc_pct=st.required_soc_pct,
                    required_kwh=st.required_soc_pct / 100.0 * cap,
                    trip_kwh=trip,
                )
            )
            if d <= a:
                notes.append(f"{where} is parked for less than one time step, so it cannot charge then.")
            cursor = dep + 1
        # a stop must leave enough energy for the next trip
        for prev, nxt in zip(stops, stops[1:]):
            if nxt.trip_kwh > prev.required_kwh + 1e-9:
                notes.append(
                    f"{who}, stop {prev.number}: required level raised from {prev.required_soc_pct:g}% to "
                    f"{nxt.trip_kwh / cap * 100:.0f}% so it can drive the next trip ({nxt.trip_kwh:g} kWh)."
                )
                prev.required_kwh = nxt.trip_kwh
        vehicles.append(
            VehicleData(
                id=veh.id,
                battery_kwh=cap,
                max_kw=veh.max_kw,
                arrival_kwh=veh.arrival_soc_pct / 100.0 * cap,
                stops=stops,
            )
        )

    if errors:
        raise BerthInputError(errors)

    # ---- horizon -------------------------------------------------------------
    last_departure = max(s.departure_min for v in vehicles for s in v.stops)
    if fleet.hours is not None:
        end = fleet.hours * 60.0
        late = [
            f"{v.id} leaves at {s.departure_label}"
            for v in vehicles
            for s in v.stops
            if s.departure_min > end + 1e-9
        ]
        if late:
            raise BerthInputError(
                [f"{x}, after the plan ends ({fleet.hours:g} hours after {label(0)}). "
                 "Increase 'hours' or remove it." for x in late]
            )
    else:
        end = last_departure
    if end > MAX_HOURS * 60:
        raise BerthInputError(
            f"the plan would be {end / 60:.0f} hours long (more than {MAX_HOURS}). "
            "Check the arrival and departure times, or set 'start'."
        )
    n_steps = max(1, math.ceil(end / step - 1e-9))

    # ---- per-step site and price values -----------------------------------
    price = np.zeros(n_steps)
    limit = np.zeros(n_steps)
    other = np.zeros(n_steps)
    for t in range(n_steps):
        minutes = [start_clock + t * step + j for j in range(step)]
        price[t] = float(np.mean([profile_value(fleet.tariff.energy_price_per_kwh, m) for m in minutes]))
        limit[t] = min(profile_value(fleet.site.power_limit_kw, m) for m in minutes)
        other[t] = max(profile_value(fleet.site.other_load_kw, m) for m in minutes)

    blocked = [t for t in range(n_steps) if other[t] >= limit[t]]
    if blocked:
        t0 = blocked[0]
        notes.append(
            f"At {label(t0 * step)} the other building load ({other[t0]:g} kW) uses the whole site limit "
            f"({limit[t0]:g} kW), so no charging is possible in {len(blocked)} time step(s)."
        )

    chargers = sorted(((g.count, g.kw) for g in fleet.site.chargers), key=lambda c: -c[1])
    plan_days = max(1, math.ceil(n_steps * step / MINUTES_PER_DAY - 1e-6))
    billing_days = fleet.tariff.billing_days

    return Problem(
        name=fleet.name,
        currency=fleet.tariff.currency,
        tariff_name=fleet.tariff.name,
        step_minutes=step,
        n_steps=n_steps,
        labels=[label(t * step) for t in range(n_steps)],
        edge_labels=[label(t * step) for t in range(n_steps + 1)],
        price=price,
        limit_kw=limit,
        other_kw=other,
        chargers=chargers,
        efficiency=fleet.site.charging_efficiency,
        demand_rate=fleet.tariff.demand_charge_per_kw * min(plan_days, billing_days) / billing_days,
        demand_rate_monthly=fleet.tariff.demand_charge_per_kw,
        plan_days=plan_days,
        billing_days=billing_days,
        vehicles=vehicles,
        time_limit_s=fleet.time_limit_s,
        notes=notes,
        shortfall_policy=fleet.shortfall_policy,
    )


def _offset_text(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _earliest_clock(clocks: list[int]) -> int:
    """Earliest arrival on a 24-hour clock: the time just after the longest
    gap, so arrivals at 22:00 and 00:30 start at 22:00."""
    uniq = sorted(set(clocks))
    if len(uniq) == 1:
        return uniq[0]
    gaps = [((uniq[(i + 1) % len(uniq)] - uniq[i]) % MINUTES_PER_DAY, i) for i in range(len(uniq))]
    _, i = max(gaps)
    return uniq[(i + 1) % len(uniq)]
