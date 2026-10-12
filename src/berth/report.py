"""Text output: summary tables, plain-English explanation, schedule CSV."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from berth.results import PlanResult

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥", "AUD": "A$", "CAD": "C$"}


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 charger', '3 chargers' (many: the plural when it is not word + 's')."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def how_many_short(n_short: int, total: int) -> str:
    """'The vehicle cannot', '1 of 3 vehicles cannot', ..."""
    if total == 1:
        return "The vehicle cannot"
    return f"{n_short} of {total} vehicles cannot"


def money(x: float, currency: str = "USD") -> str:
    sym = SYMBOLS.get(currency.upper())
    sign = "-" if x < -0.005 else ""
    body = f"{abs(x):,.2f}"
    return f"{sign}{sym}{body}" if sym else f"{sign}{body} {currency}"


def price_text(x: float, currency: str = "USD") -> str:
    sym = SYMBOLS.get(currency.upper())
    return f"{sym}{x:.3f}" if sym else f"{x:.3f} {currency}"


def _table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> str:
    right = right or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()

    line = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(headers), line] + [fmt(r) for r in rows])


def summary_text(r: "PlanResult", max_vehicles: int = 40) -> str:
    cur = r.currency
    o, b, s = r.optimized, r.baseline, r.savings
    out: list[str] = []
    out.append(f"PeakWhale Berth  |  {r.name}")
    out.append(
        f"Tariff: {r.tariff}  |  step {r.step_minutes} min  |  {r.times[0]} to "
        f"{r.end}  |  chargers: {_chargers_text(r)}"
    )
    out.append("")
    out.append(r.headline)
    out.append("")
    rows = [
        ["Total cost", money(o.total_cost, cur), money(b.total_cost, cur)],
        ["  energy", money(o.energy_cost, cur), money(b.energy_cost, cur)],
        ["  demand charge", money(o.demand_cost, cur), money(b.demand_cost, cur)],
        ["Grid energy", f"{o.grid_kwh:,.1f} kWh", f"{b.grid_kwh:,.1f} kWh"],
        ["Peak charging", f"{o.peak_charging_kw:,.1f} kW", f"{b.peak_charging_kw:,.1f} kW"],
        ["Peak site load", f"{o.peak_site_kw:,.1f} kW", f"{b.peak_site_kw:,.1f} kW"],
        ["Vehicles ready", f"{o.vehicles_ready} / {o.vehicles_total}", f"{b.vehicles_ready} / {b.vehicles_total}"],
        ["Energy short", f"{o.shortfall_kwh:,.1f} kWh", f"{b.shortfall_kwh:,.1f} kWh"],
        ["Biggest shortfall", f"{o.max_shortfall_kwh:,.1f} kWh", f"{b.max_shortfall_kwh:,.1f} kWh"],
    ]
    out.append(_table(["", "Berth plan", "Charge on arrival"], rows, right={1, 2}))
    out.append("")
    pct = f" ({s.cost_pct:.1f}%)" if s.cost_pct is not None else ""
    out.append(f"Savings vs charge on arrival: {money(s.cost, cur)}{pct}, peak charging {s.peak_kw:,.1f} kW lower.")
    out.append("")

    vrows = []
    for v in r.vehicles[:max_vehicles]:
        for st in v.stops:
            win = ", ".join(span(w.start, w.end) for w in v.windows) if st.stop == v.stops[0].stop else ""
            vrows.append(
                [
                    v.id if st.stop == v.stops[0].stop else "",
                    str(st.stop) if len(v.stops) > 1 else "",
                    st.arrival,
                    st.departure,
                    f"{st.required_soc_pct:.0f}%",
                    f"{st.departure_soc_pct:.0f}%",
                    "yes" if st.ready else f"NO (-{st.shortfall_kwh:.1f} kWh)",
                    _short(win, 60),
                ]
            )
    out.append(_table(["Vehicle", "Stop", "Arrive", "Leave", "Need", "Plan", "Ready", "Charging"], vrows, right={4, 5}))
    if len(r.vehicles) > max_vehicles:
        out.append(f"... and {len(r.vehicles) - max_vehicles} more vehicles (see the schedule CSV)")

    if r.shortfalls:
        out.append("")
        out.append("Vehicles that fall short:")
        out.append(f"  ({policy_text(r)})")
        stops = {v.id: len(v.stops) for v in r.vehicles}
        for item in r.shortfalls:
            where = f"stop {item.stop}, leaves" if stops.get(item.vehicle_id, 1) > 1 else "leaves"
            out.append(f"  {item.vehicle_id} ({where} {item.departure}): {item.shortfall_kwh:.1f} kWh short")
            for reason in item.reasons:
                out.append(f"      - {reason}")
        if r.diagnosis:
            out.append("")
            out.append("What would help:")
            for line in r.diagnosis.summary:
                out.append(f"  - {line}")

    if r.notes:
        out.append("")
        out.append("Notes:")
        for n in r.notes:
            out.append(f"  - {n}")
    out.append("")
    out.append(
        f"Solver: {r.solver.solver}, {r.solver.status}, {r.solver.seconds:.2f} s, "
        f"{r.solver.variables} variables ({r.solver.integer_variables} on/off)."
    )
    return "\n".join(out)


def span(start: str, end: str) -> str:
    """"01:30 +1d" to "06:00 +1d" becomes "01:30-06:00 +1d"."""
    for suffix in (" +1d", " +2d", " +3d", " +4d", " +5d", " +6d", " +7d"):
        if start.endswith(suffix) and end.endswith(suffix):
            return f"{start.removesuffix(suffix)}-{end}"
    return f"{start}-{end}"


def kwh_text(x: float) -> str:
    """Whole kWh, but one decimal below 1 kWh (so a small window does not read '0 kWh')."""
    return f"{x:.1f} kWh" if x < 0.95 else f"{x:.0f} kWh"


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _chargers_text(r: "PlanResult") -> str:
    return ", ".join(f"{c['count']} x {c['kw']:g} kW" for c in r.chargers)


def policy_text(r: "PlanResult") -> str:
    """One sentence on how Berth chose who falls short."""
    o, b = r.optimized, r.baseline
    if r.shortfall_policy == "fair":
        return (
            f"Policy fair: Berth shares the shortfall. Biggest shortfall {o.max_shortfall_kwh:.1f} kWh "
            f"(charging on arrival: {b.max_shortfall_kwh:.1f} kWh); {o.vehicles_ready} of {o.vehicles_total} "
            "vehicles fully ready."
        )
    return (
        f"Policy most_ready: Berth gets as many vehicles fully ready as it can, {o.vehicles_ready} of "
        f"{o.vehicles_total} (charging on arrival: {b.vehicles_ready}), then keeps the shortfall of the "
        "others as small as possible."
    )


def explain(r: "PlanResult", max_vehicles: int = 15) -> str:
    """A short plain-English explanation of a plan."""
    cur = r.currency
    lines: list[str] = [r.headline, ""]

    # when does charging happen, and at what price
    energy_by_price: dict[float, float] = defaultdict(float)
    hours = r.step_minutes / 60
    for p, kw in zip(r.price, r.total_charging_kw):
        energy_by_price[round(p, 4)] += kw * hours
    total = sum(energy_by_price.values())
    if total > 0:
        cheapest = min(r.price)
        dearest = max(r.price)
        share_cheap = energy_by_price.get(round(cheapest, 4), 0.0) / total * 100
        lines.append(
            f"Timing: {share_cheap:.0f}% of the energy is charged at the lowest price "
            f"({price_text(cheapest, cur)}/kWh). Prices in this plan range from {price_text(cheapest, cur)} "
            f"to {price_text(dearest, cur)} per kWh."
        )
        base_total = sum(r.baseline_charging_kw) * hours
        if base_total > 0:
            base_cheap = sum(
                kw * hours for p, kw in zip(r.price, r.baseline_charging_kw) if round(p, 4) == round(cheapest, 4)
            )
            lines.append(
                f"Charging on arrival would put only {base_cheap / base_total * 100:.0f}% of the energy at the lowest price."
            )
    o, b = r.optimized, r.baseline
    lines.append(
        f"Power: peak charging is {o.peak_charging_kw:.0f} kW (charge on arrival: {b.peak_charging_kw:.0f} kW). "
        f"The site can give charging up to {max(r.available_kw):.0f} kW."
    )
    if o.demand_cost > 0 or b.demand_cost > 0:
        lines.append(
            f"Demand charge from charging: {money(o.demand_cost, cur)} (charge on arrival: {money(b.demand_cost, cur)})."
        )
    lines.append("")
    lines.append("Vehicles:")
    for v in r.vehicles[:max_vehicles]:
        win = ", ".join(f"{span(w.start, w.end)} ({kwh_text(w.kwh)})" for w in v.windows) or "no charging needed"
        last = v.stops[-1]
        status = (
            f"ready with {last.departure_soc_pct:.0f}% at {last.departure}"
            if v.ready
            else f"SHORT by {v.shortfall_kwh:.1f} kWh"
        )
        lines.append(f"- {v.id}: charges {win}; {status}.")
    if len(r.vehicles) > max_vehicles:
        lines.append(f"- ... and {len(r.vehicles) - max_vehicles} more vehicles.")
    if r.shortfalls:
        lines.append("")
        lines.append("Why some vehicles fall short:")
        lines.append(policy_text(r))
        for item in r.shortfalls:
            lines.append(f"- {item.vehicle_id} (leaves {item.departure}) is {item.shortfall_kwh:.1f} kWh short.")
            for reason in item.reasons:
                lines.append(f"  {reason}")
        if r.diagnosis:
            lines.append("")
            lines.append("What would help:")
            for s in r.diagnosis.summary:
                lines.append(f"- {s}")
    if r.notes:
        lines.append("")
        lines.append("Assumptions and notes:")
        for n in r.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


def schedule_csv(r: "PlanResult") -> str:
    """Wide CSV: one row per time step, one column per vehicle (kW)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    ids = [v.id for v in r.vehicles]
    w.writerow(
        [
            "step",
            "time",
            "price_per_kwh",
            "site_limit_kw",
            "other_load_kw",
            "available_kw",
            "total_charging_kw",
            "baseline_charging_kw",
        ]
        + [f"{i}_kw" for i in ids]
    )
    for t, label in enumerate(r.times):
        w.writerow(
            [
                t,
                label,
                f"{r.price[t]:.5g}",
                f"{r.site_limit_kw[t]:g}",
                f"{r.other_load_kw[t]:g}",
                f"{r.available_kw[t]:g}",
                f"{r.total_charging_kw[t]:.3f}",
                f"{r.baseline_charging_kw[t]:.3f}",
            ]
            + [f"{v.power_kw[t]:.3f}" for v in r.vehicles]
        )
    return buf.getvalue()
