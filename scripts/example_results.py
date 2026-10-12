"""Plan every example fleet and print a results table (Markdown).

The numbers in the README "Results on the example fleets" section come from
this script. All example data is made up. When not every vehicle in an
example can be ready, the script also plans it with shortfall_policy fair.

Run:  uv run python scripts/example_results.py
"""

from __future__ import annotations

import time

from berth.examples import list_examples, load_example
from berth.planner import plan
from berth.report import money


def main() -> None:
    rows = []
    for ex in list_examples():
        fleet = load_example(ex["name"])
        runs = [(ex["name"], None)]
        for label, policy in runs:
            started = time.perf_counter()
            r = plan(fleet, shortfall_policy=policy)
            seconds = time.perf_counter() - started
            if policy is None and not r.all_ready:
                # not every vehicle can be ready: also show the other policy
                runs.append((f"{ex['name']} (shortfall_policy: fair)", "fair"))
            rows.append(_row(label, r, seconds))
    head = [
        "Example",
        "Vehicles",
        "Chargers",
        "Ready (Berth / on arrival)",
        "Cost (Berth / on arrival)",
        "Saving",
        "Peak charging kW (Berth / on arrival)",
        "Biggest shortfall kWh (Berth / on arrival)",
        "Solve time",
    ]
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join("---" for _ in head) + "|")
    for row in rows:
        print("| " + " | ".join(row) + " |")


def _row(label: str, r, seconds: float) -> list[str]:
    o, b, s = r.optimized, r.baseline, r.savings
    cur = r.currency
    pct = f"{s.cost_pct:.1f}%" if s.cost_pct is not None else "-"
    chargers = " + ".join(f"{c['count']} x {c['kw']:g} kW" for c in r.chargers)
    return [
        label,
        f"{o.vehicles_total}",
        chargers,
        f"{o.vehicles_ready} / {b.vehicles_ready}",
        f"{money(o.total_cost, cur)} / {money(b.total_cost, cur)}",
        f"{money(s.cost, cur)} ({pct})",
        f"{o.peak_charging_kw:.0f} / {b.peak_charging_kw:.0f}",
        f"{o.max_shortfall_kwh:.1f} / {b.max_shortfall_kwh:.1f}",
        f"{seconds:.1f} s",
    ]


if __name__ == "__main__":
    main()
