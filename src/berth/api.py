"""HTTP API (FastAPI) and the web page.

Start it with `berth serve`, then open http://127.0.0.1:8000/ for the page
and http://127.0.0.1:8000/docs for the API documentation.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from berth import __version__
from berth.errors import BerthInputError, BerthSolverError, format_error_list
from berth.examples import list_examples, load_example
from berth.loaders import fleet_to_yaml, load_fleet_text, read_vehicles_csv
from berth.models import Fleet, Vehicle
from berth.planner import parse_fleet, plan
from berth.report import explain, schedule_csv
from berth.results import PlanResult

WEB = Path(__file__).parent / "web"
MAX_TIME_LIMIT = float(os.environ.get("BERTH_MAX_TIME_LIMIT", "60"))
MAX_BODY_BYTES = 2_000_000

TEXT_BODY = {"requestBody": {"content": {"text/plain": {"schema": {"type": "string"}}}, "required": True}}

app = FastAPI(
    title="PeakWhale Berth",
    version=__version__,
    description=(
        "Free EV fleet charging planner. Send your vehicles, chargers, site power limit and tariff; "
        "get back a charging schedule that has every vehicle ready at departure at the lowest energy "
        "and demand cost, compared with charging at full power on arrival. If not every vehicle can "
        "be ready, the answer says which ones fall short, by how many kWh, and why."
    ),
)


# ---- errors ---------------------------------------------------------------


@app.exception_handler(BerthInputError)
async def _input_error(_: Request, exc: BerthInputError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc), "problems": exc.problems})


@app.exception_handler(BerthSolverError)
async def _solver_error(_: Request, exc: BerthSolverError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": str(exc), "problems": [str(exc)]})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    body = exc.body if isinstance(exc.body, dict) else None
    problems = format_error_list(exc.errors(), body, drop_prefix=("body",))
    return JSONResponse(
        status_code=422,
        content={"error": "the fleet is not valid", "problems": problems, "detail": jsonable_encoder(exc.errors())},
    )


async def _text(request: Request) -> str:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise BerthInputError(f"the request is too large (more than {MAX_BODY_BYTES // 1_000_000} MB)")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BerthInputError("the request body is not UTF-8 text") from None


def _capped(fleet: Fleet) -> Fleet:
    if fleet.time_limit_s > MAX_TIME_LIMIT:
        fleet = fleet.model_copy(update={"time_limit_s": MAX_TIME_LIMIT})
    return fleet


# ---- API --------------------------------------------------------------------


@app.get("/api/health", tags=["info"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/examples", tags=["examples"])
def examples() -> list[dict]:
    """The bundled example fleets (made-up data)."""
    return list_examples()


@app.get("/api/examples/{name}", tags=["examples"])
def example(name: str, format: str = "json"):
    """One example fleet, with its tariff included. `format=yaml` returns YAML text."""
    fleet = load_example(name)
    if format == "yaml":
        return PlainTextResponse(fleet_to_yaml(fleet), media_type="text/yaml")
    return fleet.model_dump(by_alias=True, exclude_none=True)


@app.post("/api/plan", response_model=PlanResult, tags=["plan"])
def plan_json(fleet: Fleet) -> PlanResult:
    """Plan charging for a fleet given as JSON."""
    return plan(_capped(fleet))


@app.post("/api/plan/yaml", response_model=PlanResult, tags=["plan"], openapi_extra=TEXT_BODY)
async def plan_yaml(request: Request) -> PlanResult:
    """Plan charging for a fleet given as YAML (or JSON) text, like the files in examples/.
    A tariff must be included in the text (not a file name)."""
    fleet = load_fleet_text(await _text(request), "request")
    return await run_in_threadpool(plan, _capped(fleet))


@app.post("/api/explain", response_class=PlainTextResponse, tags=["plan"])
def explain_json(fleet: Fleet) -> str:
    """Plan charging and explain the result in plain words."""
    return explain(plan(_capped(fleet)))


@app.post("/api/schedule.csv", response_class=PlainTextResponse, tags=["plan"])
def schedule(fleet: Fleet) -> PlainTextResponse:
    """Plan charging and return the schedule as CSV (one row per time step, kW per vehicle)."""
    result = plan(_capped(fleet))
    return PlainTextResponse(
        schedule_csv(result),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="berth-schedule.csv"'},
    )


@app.post("/api/convert/from-yaml", tags=["convert"], openapi_extra=TEXT_BODY)
async def from_yaml(request: Request) -> dict:
    """Check fleet YAML text and return it as JSON."""
    fleet = load_fleet_text(await _text(request), "request")
    return fleet.model_dump(by_alias=True, exclude_none=True)


@app.post("/api/convert/to-yaml", response_class=PlainTextResponse, tags=["convert"])
def to_yaml(fleet: Fleet) -> PlainTextResponse:
    """Turn a fleet (JSON) into readable YAML."""
    return PlainTextResponse(fleet_to_yaml(fleet), media_type="text/yaml")


@app.post("/api/convert/vehicles-csv", tags=["convert"], openapi_extra=TEXT_BODY)
async def vehicles_csv(request: Request) -> list[dict]:
    """Read a vehicles CSV (one row per stop) and return the vehicles as JSON."""
    rows = read_vehicles_csv(await _text(request), "vehicles CSV")
    out = []
    for i, row in enumerate(rows):
        try:
            out.append(Vehicle.model_validate(row).model_dump(exclude_none=True))
        except Exception as exc:  # pydantic ValidationError
            problems = format_error_list(exc.errors(), {"vehicles": rows}, prefix=("vehicles", i))
            raise BerthInputError(problems, "vehicles CSV") from None
    return out


# ---- web page -------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
