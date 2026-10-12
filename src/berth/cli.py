"""Command line: berth plan, berth explain, berth examples, berth serve."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from berth import __version__
from berth.errors import BerthInputError, BerthSolverError

EXIT_INPUT, EXIT_SOLVER, EXIT_SHORT = 1, 3, 4


def _number(text: str) -> float:
    """argparse type: a finite number (NaN and infinity are refused)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"'{text}' is not allowed (use a finite number)")
    return value


def _add_input_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("fleet", nargs="?", help="fleet file (.yaml or .json)")
    p.add_argument("--example", metavar="NAME", help="use a bundled example fleet (see: berth examples)")
    g = p.add_argument_group("CSV input (instead of a fleet file)")
    g.add_argument("--vehicles", metavar="CSV", help="vehicles CSV, one row per stop")
    g.add_argument("--tariff", metavar="FILE", help="tariff file (.yaml or .csv)")
    g.add_argument("--chargers", metavar="SPEC", help="charger groups, like 8x19.2 or 8x19.2,2x50")
    g.add_argument("--site-limit-kw", type=_number, metavar="KW", help="site power limit, kW")
    g.add_argument("--other-load-kw", type=_number, default=0.0, metavar="KW", help="other building load, kW (default 0)")
    g.add_argument("--demand-charge", type=_number, metavar="RATE", help="demand charge per kW of monthly peak")
    g.add_argument("--efficiency", type=_number, default=1.0, help="charging efficiency, 0.5 to 1 (default 1)")
    g.add_argument("--name", default=None, help="name for this plan")
    o = p.add_argument_group("options")
    o.add_argument("--start", help="plan start, HH:MM or date and time (default: earliest arrival)")
    o.add_argument("--hours", type=_number, help="plan length in hours (default: until the last departure)")
    o.add_argument("--step", type=int, metavar="MIN", help="time step in minutes (default 15)")
    o.add_argument("--time-limit", type=_number, metavar="S", help="solver time limit in seconds (default 30)")
    o.add_argument(
        "--shortfall-policy",
        choices=["most_ready", "fair"],
        help="when not every vehicle can be ready: most_ready gets the most vehicles fully ready "
        "(default), fair keeps the biggest shortfall as small as possible",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="berth",
        description="PeakWhale Berth: plan EV fleet charging so every vehicle is ready at the lowest cost.",
    )
    parser.add_argument("--version", action="version", version=f"berth {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("plan", help="plan charging, print a summary and write the schedule CSV")
    _add_input_args(p)
    p.add_argument("-o", "--output", metavar="CSV", help="schedule CSV to write (default: <name>-schedule.csv)")
    p.add_argument("--json", metavar="FILE", help="also write the full result as JSON")
    p.add_argument("--no-csv", action="store_true", help="do not write the schedule CSV")
    p.add_argument("--fail-if-short", action="store_true", help=f"exit with code {EXIT_SHORT} if a vehicle falls short")

    e = sub.add_parser("explain", help="plan and explain the result in plain words")
    _add_input_args(e)

    sub.add_parser("examples", help="list the bundled example fleets")

    s = sub.add_parser("serve", help="start the web page and HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    return parser


def load_input(args: argparse.Namespace):
    """Return (fleet, default output stem)."""
    from berth.examples import example_path
    from berth.loaders import load_fleet_file, load_tariff_file, read_vehicles_csv, parse_chargers, _read
    from berth.planner import parse_fleet

    sources = [x for x in (args.fleet, args.example, args.vehicles) if x]
    if len(sources) != 1:
        raise BerthInputError(
            "give one input: a fleet file (berth plan fleet.yaml), --example NAME, "
            "or --vehicles vehicles.csv with --tariff, --chargers and --site-limit-kw"
        )
    if args.fleet or args.example:
        path = Path(args.fleet) if args.fleet else example_path(args.example)
        fleet = load_fleet_file(path)
        stem = path.stem
    else:
        missing = [
            flag
            for flag, value in (("--tariff", args.tariff), ("--chargers", args.chargers),
                                ("--site-limit-kw", args.site_limit_kw))
            if value is None
        ]
        if missing:
            raise BerthInputError(f"with --vehicles you also need {', '.join(missing)}")
        vpath = Path(args.vehicles)
        tariff = load_tariff_file(args.tariff)
        if args.demand_charge is not None:
            tariff["demand_charge_per_kw"] = args.demand_charge
        data = {
            "name": args.name or vpath.stem,
            "site": {
                "power_limit_kw": args.site_limit_kw,
                "other_load_kw": args.other_load_kw,
                "chargers": parse_chargers(args.chargers),
                "charging_efficiency": args.efficiency,
            },
            "tariff": tariff,
            "vehicles": read_vehicles_csv(_read(vpath), str(vpath)),
        }
        fleet = parse_fleet(data, str(vpath))
        stem = vpath.stem
    overrides = {}
    if args.start:
        overrides["start"] = args.start
    if args.hours is not None:
        overrides["hours"] = args.hours
    if args.step is not None:
        overrides["step_minutes"] = args.step
    if args.time_limit is not None:
        overrides["time_limit_s"] = args.time_limit
    if args.shortfall_policy is not None:
        overrides["shortfall_policy"] = args.shortfall_policy
    if overrides:
        fleet = parse_fleet({**fleet.model_dump(by_alias=True, exclude_none=True), **overrides})
    return fleet, stem


def cmd_plan(args: argparse.Namespace) -> int:
    from berth.planner import plan
    from berth.report import schedule_csv, summary_text

    fleet, stem = load_input(args)
    result = plan(fleet)
    print(summary_text(result))
    if not args.no_csv:
        out = Path(args.output) if args.output else Path(f"{stem}-schedule.csv")
        out.write_text(schedule_csv(result), encoding="utf-8")
        print(f"\nSchedule written to {out}")
    if args.json:
        Path(args.json).write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(f"Full result written to {args.json}")
    if args.fail_if_short and not result.all_ready:
        return EXIT_SHORT
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from berth.planner import plan
    from berth.report import explain

    fleet, _ = load_input(args)
    print(explain(plan(fleet)))
    return 0


def cmd_examples(_: argparse.Namespace) -> int:
    from berth.examples import examples_dir, list_examples

    items = list_examples()
    if not items:
        print("No examples found.")
        return 0
    print(f"Examples in {examples_dir()}:\n")
    for e in items:
        print(f"  {e['name']:<24} {e['title']} ({e['vehicles']} vehicles)")
    print("\nRun one with:  berth plan --example NAME")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"Berth web page: http://{args.host}:{args.port}/   API docs: http://{args.host}:{args.port}/docs")
    uvicorn.run("berth.api:app", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {"plan": cmd_plan, "explain": cmd_explain, "examples": cmd_examples, "serve": cmd_serve}
    try:
        return handlers[args.command](args)
    except BerthInputError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except BerthSolverError as exc:
        print(f"Solver error: {exc}", file=sys.stderr)
        return EXIT_SOLVER
    except BrokenPipeError:  # output piped into head, less, ...
        sys.stderr.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
