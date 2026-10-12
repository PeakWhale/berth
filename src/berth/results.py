"""Output models: what a plan returns (JSON in the API, MCP and web page)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChargingWindow(BaseModel):
    start: str = Field(description="First step with charging.")
    end: str = Field(description="End of the last step with charging.")
    kwh: float = Field(description="Energy from the grid in this window, kWh.")
    max_kw: float


class StopResult(BaseModel):
    stop: int
    arrival: str
    departure: str
    required_soc_pct: float
    required_kwh: float = Field(description="Energy needed in the battery at departure, kWh.")
    departure_kwh: float = Field(description="Planned battery energy at departure, kWh.")
    departure_soc_pct: float
    shortfall_kwh: float
    ready: bool
    baseline_departure_soc_pct: float
    baseline_ready: bool
    reason_codes: list[str] = Field(default_factory=list, description="not_enough_time, site_limit, too_few_chargers")
    reasons: list[str] = Field(default_factory=list)


class VehicleResult(BaseModel):
    id: str
    battery_kwh: float
    max_kw: float
    ready: bool
    baseline_ready: bool
    shortfall_kwh: float
    grid_kwh: float
    stops: list[StopResult]
    windows: list[ChargingWindow]
    power_kw: list[float] = Field(description="Planned charging power per time step, kW.")


class Totals(BaseModel):
    energy_cost: float
    demand_cost: float = Field(description="Demand charge caused by charging (peak above other load x rate).")
    total_cost: float
    grid_kwh: float
    peak_charging_kw: float
    peak_site_kw: float
    vehicles_ready: int
    vehicles_total: int
    all_ready: bool
    shortfall_kwh: float = Field(description="Total energy missing at departures, kWh.")
    max_shortfall_kwh: float = Field(description="Biggest shortfall of one vehicle at one departure, kWh.")


class Savings(BaseModel):
    cost: float
    cost_pct: float | None
    peak_kw: float
    peak_pct: float | None


class ShortItem(BaseModel):
    vehicle_id: str
    stop: int
    departure: str
    shortfall_kwh: float
    reason_codes: list[str]
    reasons: list[str]


class DiagnosisResult(BaseModel):
    binding_limits: list[str]
    extra_site_kw: float | None = None
    extra_chargers: int | None = None
    extra_charger_kw: float | None = None
    summary: list[str]


class SolverInfo(BaseModel):
    solver: str = "HiGHS"
    status: str
    optimal: bool
    mip_gap: float | None
    seconds: float
    variables: int
    constraints: int
    integer_variables: int
    stages: list[str]


class PlanResult(BaseModel):
    name: str
    currency: str
    tariff: str
    step_minutes: int
    headline: str = Field(description="One-line summary in plain words.")
    all_ready: bool
    shortfall_policy: str = Field(
        description="most_ready (most vehicles fully ready first) or fair (smallest biggest avoidable "
        "shortfall). Only matters when not every vehicle can be ready."
    )
    times: list[str] = Field(description="Start label of each time step.")
    end: str = Field(description="When the last time step ends.")
    price: list[float] = Field(description="Energy price per kWh in each step.")
    site_limit_kw: list[float]
    other_load_kw: list[float]
    available_kw: list[float] = Field(description="Site limit minus other load: what charging may use.")
    total_charging_kw: list[float]
    baseline_charging_kw: list[float]
    chargers: list[dict]
    optimized: Totals
    baseline: Totals
    savings: Savings
    vehicles: list[VehicleResult]
    shortfalls: list[ShortItem]
    diagnosis: DiagnosisResult | None
    notes: list[str]
    solver: SolverInfo
