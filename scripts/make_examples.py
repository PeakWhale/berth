"""Write the example fleets in examples/.

All data here is made up with a fixed random seed. The vehicles, sites and
tariff are generic examples in a realistic shape. They do not come from any
utility, fleet or company.

Run:  uv run python scripts/make_examples.py
"""

from __future__ import annotations

import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "examples"
SEED = 2026


def hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def rand_time(rng: random.Random, start: str, end: str, step: int = 5) -> int:
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    a, b = h1 * 60 + m1, h2 * 60 + m2
    if b < a:
        b += 1440
    return rng.randrange(a, b + 1, step) % 1440


def flow(d: dict) -> str:
    """One-line YAML mapping with quoted times."""
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            parts.append(f'{k}: "{v}"')
        elif isinstance(v, float):
            parts.append(f"{k}: {v:g}")
        else:
            parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


TARIFF = """\
# Example time-of-use tariff for the Berth examples.
# The numbers are made up, in a shape that is common for commercial
# electricity tariffs. This is NOT a real utility tariff.
name: Example TOU (made up)
currency: USD
energy_price_per_kwh:
  - {from: "00:00", to: "06:00", price: 0.11}   # night, cheapest
  - {from: "06:00", to: "16:00", price: 0.19}   # day
  - {from: "16:00", to: "21:00", price: 0.38}   # evening peak
  - {from: "21:00", to: "24:00", price: 0.16}   # late evening
demand_charge_per_kw: 15.00   # per kW of the monthly peak
billing_days: 30              # a one-day plan carries 1/30 of the monthly demand charge
"""


def delivery_vans(rng: random.Random) -> tuple[str, list[dict]]:
    models = [  # (battery kWh, max kW) generic van sizes
        (70, 11.0),
        (90, 19.2),
        (113, 19.2),
    ]
    vans = []
    for i in range(1, 13):
        battery, max_kw = rng.choice(models)
        arrive = rand_time(rng, "17:00", "19:30")
        leave = rand_time(rng, "05:30", "07:30")
        vans.append(
            {
                "id": f"VAN-{i:02d}",
                "battery_kwh": battery,
                "max_kw": max_kw,
                "arrival": hhmm(arrive),
                "departure": hhmm(leave),
                "arrival_soc_pct": rng.randrange(15, 46),
                "required_soc_pct": rng.choice([80, 85, 90, 95]),
            }
        )
    head = """\
# Example fleet: 12 electric delivery vans at an overnight depot.
# Made-up data (fixed random seed in scripts/make_examples.py).
name: Delivery vans - overnight depot
description: >
  12 delivery vans come back between 17:00 and 19:30 and leave again
  between 05:30 and 07:30. One 19.2 kW charger per van, but the site
  connection is 150 kW and the warehouse also uses power.
start: "16:00"
step_minutes: 15
tariff: tariff-tou.yaml
site:
  power_limit_kw: 150
  other_load_kw:            # warehouse lights, offices, cold room
    - {from: "06:00", to: "20:00", kw: 35}
    - {from: "20:00", to: "06:00", kw: 12}
  chargers:
    - {count: 12, kw: 19.2, name: "AC 19.2 kW"}
  charging_efficiency: 0.92
vehicles:
"""
    body = "".join(f"  - {flow(v)}\n" for v in vans)
    return head + body, vans


def school_buses(rng: random.Random) -> str:
    buses = []
    for i in range(1, 31):
        battery = rng.choice([155, 196, 226])
        route_am = rng.randrange(55, 86)  # kWh already used on the morning route
        route_pm = rng.randrange(55, 86)  # kWh used on the afternoon route
        arrive_soc = 90 - round(route_am / battery * 100)
        midday_arrive = rand_time(rng, "09:00", "09:40")
        midday_leave = rand_time(rng, "14:00", "14:40")
        evening_arrive = rand_time(rng, "16:15", "17:15")
        morning_leave = rand_time(rng, "06:00", "06:45")
        # leave at midday with the afternoon route plus a 30 % reserve
        midday_need = min(95, round((route_pm + 0.3 * battery) / battery * 100))
        buses.append(
            "  - id: BUS-{:02d}\n"
            "    battery_kwh: {}\n"
            "    max_kw: 60\n"
            "    arrival_soc_pct: {}\n"
            "    stops:\n"
            "      - {}\n"
            "      - {}\n".format(
                i,
                battery,
                arrive_soc,
                flow({"arrival": hhmm(midday_arrive), "departure": hhmm(midday_leave),
                      "required_soc_pct": midday_need}),
                flow({"arrival": hhmm(evening_arrive), "departure": hhmm(morning_leave),
                      "required_soc_pct": 90, "trip_kwh": route_pm}),
            )
        )
    head = """\
# Example fleet: 30 electric school buses with a midday break.
# Made-up data (fixed random seed in scripts/make_examples.py).
name: School buses - midday break and night
description: >
  30 school buses park between the morning and afternoon routes
  (about 09:00 to 14:00) and again overnight. There are 24 chargers
  for 30 buses (20 AC at 19.2 kW and 4 DC at 60 kW) and a 400 kW site
  connection shared with the bus garage.
start: "09:00"
step_minutes: 15
tariff: tariff-tou.yaml
site:
  power_limit_kw: 400
  other_load_kw:            # bus garage and workshop
    - {from: "07:00", to: "17:00", kw: 60}
    - {from: "17:00", to: "07:00", kw: 20}
  chargers:
    - {count: 20, kw: 19.2, name: "AC 19.2 kW"}
    - {count: 4, kw: 60, name: "DC 60 kW"}
  charging_efficiency: 0.92
vehicles:
"""
    return head + "".join(buses)


def late_shift(rng: random.Random) -> str:
    vans = []
    for i in range(1, 6):  # early route: back late, out again before 05:00
        vans.append(
            {
                "id": f"EARLY-{i}",
                "battery_kwh": 90,
                "max_kw": 22.0,
                "arrival": hhmm(rand_time(rng, "22:30", "23:15")),
                "departure": hhmm(rand_time(rng, "04:30", "05:00")),
                "arrival_soc_pct": rng.randrange(15, 26),
                "required_soc_pct": 90,
            }
        )
    for i in range(1, 6):  # day route: leave mid-morning, lots of time
        vans.append(
            {
                "id": f"DAY-{i}",
                "battery_kwh": 70,
                "max_kw": 22.0,
                "arrival": hhmm(rand_time(rng, "23:00", "23:59")),
                "departure": hhmm(rand_time(rng, "08:30", "09:00")),
                "arrival_soc_pct": rng.randrange(30, 41),
                "required_soc_pct": 80,
            }
        )
    # one van comes in for a short stop only: it cannot be ready
    vans.append(
        {
            "id": "SHORT-STOP",
            "battery_kwh": 90,
            "max_kw": 22.0,
            "arrival": "02:30",
            "departure": "04:00",
            "arrival_soc_pct": 15,
            "required_soc_pct": 90,
        }
    )
    head = """\
# Example fleet that CANNOT be fully charged (an infeasible case).
# Made-up data (fixed random seed in scripts/make_examples.py).
name: Late-shift vans - not enough for everyone
description: >
  11 vans share 4 chargers and a 60 kW site connection. Five EARLY vans
  are back late and leave again before 05:00, five DAY vans leave after
  08:30, and SHORT-STOP is parked for only 90 minutes. Berth shows who
  falls short, by how much, and why.
start: "22:00"
step_minutes: 15
tariff: tariff-tou.yaml
site:
  power_limit_kw: 60
  other_load_kw: 5
  chargers:
    - {count: 4, kw: 22, name: "AC 22 kW"}
  charging_efficiency: 0.92
vehicles:
"""
    return head + "".join(f"  - {flow(v)}\n" for v in vans)


def vans_csv(vans: list[dict]) -> str:
    lines = [
        "# The 12 delivery vans as CSV (one row per vehicle).",
        "# Made-up data. Used by: berth plan --vehicles examples/csv/vans.csv ...",
        "id,battery_kwh,max_kw,arrival,departure,arrival_soc_pct,required_soc_pct",
    ]
    for v in vans:
        lines.append(
            f"{v['id']},{v['battery_kwh']},{v['max_kw']:g},{v['arrival']},{v['departure']},"
            f"{v['arrival_soc_pct']},{v['required_soc_pct']}"
        )
    return "\n".join(lines) + "\n"


def tariff_csv() -> str:
    lines = ["# The example TOU tariff as 24 hourly prices (made up).", "hour,price_per_kwh"]
    for h in range(24):
        price = 0.11 if h < 6 else 0.19 if h < 16 else 0.38 if h < 21 else 0.16
        lines.append(f"{h},{price:.2f}")
    return "\n".join(lines) + "\n"


def main() -> None:
    rng = random.Random(SEED)
    OUT.mkdir(exist_ok=True)
    (OUT / "csv").mkdir(exist_ok=True)
    (OUT / "tariff-tou.yaml").write_text(TARIFF)
    vans_text, vans = delivery_vans(rng)
    (OUT / "delivery-vans.yaml").write_text(vans_text)
    (OUT / "school-buses.yaml").write_text(school_buses(rng))
    (OUT / "late-shift-infeasible.yaml").write_text(late_shift(rng))
    (OUT / "csv" / "vans.csv").write_text(vans_csv(vans))
    (OUT / "csv" / "tariff-tou.csv").write_text(tariff_csv())
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print("wrote", p.relative_to(OUT.parent))


if __name__ == "__main__":
    main()
