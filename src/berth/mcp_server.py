"""MCP server: lets AI assistants plan and explain fleet charging.

Tools:
  plan_charging  - plan charging and return the key numbers as JSON
  explain_plan   - plan charging and explain the result in plain words

Run it with the `berth-mcp` command (stdio transport).
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import PlainValidator

from berth import __version__
from berth.errors import BerthInputError, BerthSolverError
from berth.examples import list_examples, load_example
from berth.loaders import load_fleet_text
from berth.models import Fleet, ShortfallPolicy
from berth.planner import parse_fleet, plan, with_policy
from berth.report import explain
from berth.results import PlanResult

MAX_TIME_LIMIT = 60.0

INSTRUCTIONS = """\
Berth plans EV fleet charging. Give it vehicles (battery kWh, arrival and
departure times, battery level on arrival and needed at departure, max
charging kW), the site (chargers, power limit, other building load) and a
tariff (price per kWh by time of day, optional demand charge per kW). It
returns a charging schedule that has every vehicle ready at the lowest cost,
compared with charging at full power on arrival. If not every vehicle can be
ready it says which ones fall short, by how many kWh, and why. By default it
then gets as many vehicles fully ready as possible (shortfall_policy
most_ready); shortfall_policy fair shares the shortfall instead.

Pass the fleet as `fleet` (an object), as `fleet_yaml` (YAML text), or name a
bundled example with `example` (delivery-vans, school-buses,
late-shift-infeasible). Times are HH:MM; a departure earlier than the arrival
means the next day.
"""

server = MCPServer("berth", instructions=INSTRUCTIONS, version=__version__)

# The tools publish the full Fleet schema, but take the fleet as it comes and
# check it with parse_fleet, so mistakes come back as Berth's plain problems
# list (like fleet_yaml and the HTTP API) instead of raw pydantic text.
FleetArg = Annotated[Any, PlainValidator(lambda value: value, json_schema_input_type=Fleet | None)]


def _get_fleet(
    fleet: Fleet | dict | None,
    fleet_yaml: str | None,
    example: str | None,
    shortfall_policy: str | None = None,
) -> Fleet:
    given = [x for x in (fleet, fleet_yaml, example) if x not in (None, "", {})]
    if len(given) != 1:
        names = ", ".join(e["name"] for e in list_examples())
        raise BerthInputError(
            "give exactly one of: fleet (object), fleet_yaml (text) or example (one of: " + names + ")"
        )
    if example:
        f = load_example(example)
    elif fleet_yaml:
        f = load_fleet_text(fleet_yaml, "fleet_yaml")
    else:
        f = parse_fleet(fleet.model_dump(by_alias=True, exclude_none=True) if isinstance(fleet, Fleet) else fleet,
                        "fleet")
    if f.time_limit_s > MAX_TIME_LIMIT:
        f = f.model_copy(update={"time_limit_s": MAX_TIME_LIMIT})
    if shortfall_policy:
        f = with_policy(f, shortfall_policy)
    return f


def _compact(r: PlanResult, include_schedule: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "headline": r.headline,
        "all_ready": r.all_ready,
        "shortfall_policy": r.shortfall_policy,
        "currency": r.currency,
        "optimized": r.optimized.model_dump(),
        "charge_on_arrival": r.baseline.model_dump(),
        "savings": r.savings.model_dump(),
        "vehicles": [
            {
                "id": v.id,
                "ready": v.ready,
                "shortfall_kwh": v.shortfall_kwh,
                "grid_kwh": v.grid_kwh,
                "charging_windows": [w.model_dump() for w in v.windows],
                "stops": [
                    {
                        "stop": s.stop,
                        "arrival": s.arrival,
                        "departure": s.departure,
                        "required_soc_pct": s.required_soc_pct,
                        "planned_soc_pct": s.departure_soc_pct,
                        "ready": s.ready,
                        "shortfall_kwh": s.shortfall_kwh,
                        "reasons": s.reasons,
                    }
                    for s in v.stops
                ],
            }
            for v in r.vehicles
        ],
        "shortfalls": [s.model_dump() for s in r.shortfalls],
        "diagnosis": r.diagnosis.model_dump() if r.diagnosis else None,
        "notes": r.notes,
        "solver": r.solver.model_dump(),
    }
    if include_schedule:
        out["schedule"] = {
            "step_minutes": r.step_minutes,
            "times": r.times,
            "price_per_kwh": r.price,
            "available_kw": r.available_kw,
            "total_charging_kw": r.total_charging_kw,
            "power_kw": {v.id: v.power_kw for v in r.vehicles},
        }
    return out


def _error(exc: Exception) -> dict[str, Any]:
    problems = exc.problems if isinstance(exc, BerthInputError) else [str(exc)]
    return {"error": str(exc), "problems": problems}


@server.tool()
def plan_charging(
    fleet: FleetArg = None,
    fleet_yaml: str | None = None,
    example: str | None = None,
    include_schedule: bool = False,
    shortfall_policy: ShortfallPolicy | None = None,
) -> dict[str, Any]:
    """Plan EV fleet charging at the lowest energy + demand cost so every
    vehicle is ready at departure, within charger count, charger power,
    vehicle charging rate and site power limit. Returns cost, peak and
    readiness for the plan and for charging at full power on arrival, the
    charging windows per vehicle, and any shortfalls with reasons.
    Set include_schedule=true to also get the kW per vehicle per time step.
    shortfall_policy (only matters when not every vehicle can be ready):
    most_ready (default) gets the most vehicles fully ready; fair keeps the
    biggest shortfall as small as possible."""
    try:
        result = plan(_get_fleet(fleet, fleet_yaml, example, shortfall_policy))
    except (BerthInputError, BerthSolverError) as exc:
        return _error(exc)
    return _compact(result, include_schedule)


@server.tool()
def explain_plan(
    fleet: FleetArg = None,
    fleet_yaml: str | None = None,
    example: str | None = None,
    shortfall_policy: ShortfallPolicy | None = None,
) -> str:
    """Plan EV fleet charging and explain the result in plain English: when
    vehicles charge and at what price, the savings compared with charging on
    arrival, the peak power, and why any vehicle falls short with what would
    help (more site power, more chargers, or more time). shortfall_policy:
    most_ready (default, most vehicles fully ready) or fair (smallest
    biggest shortfall)."""
    try:
        return explain(plan(_get_fleet(fleet, fleet_yaml, example, shortfall_policy)))
    except (BerthInputError, BerthSolverError) as exc:
        return "Berth could not plan this fleet:\n" + "\n".join(f"- {p}" for p in _error(exc)["problems"])


def main() -> None:
    server.run()  # stdio


if __name__ == "__main__":
    main()
