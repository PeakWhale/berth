"""Input models: the fleet, the site, the tariff and the vehicles.

The same structure is used by the YAML files, the HTTP API, the web page and
the MCP server.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from berth.timeutil import MINUTES_PER_DAY, check_time, clock_label, parse_clock

PROFILE_HELP = (
    "must be a number, a list of 24 hourly values (starting at 00:00), "
    "or a list of periods like {from: '07:00', to: '16:00', value: 0.20}"
)


class Period(BaseModel):
    """A value that applies between two clock times. `to` may be before `from`
    to wrap past midnight (for example 21:00 to 07:00)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    start: str = Field(
        validation_alias=AliasChoices("from", "start"),
        serialization_alias="from",
        description="Start time HH:MM (included).",
        examples=["07:00"],
    )
    end: str = Field(
        validation_alias=AliasChoices("to", "end"),
        serialization_alias="to",
        description="End time HH:MM (not included). 24:00 is allowed.",
        examples=["16:00"],
    )
    value: float = Field(
        validation_alias=AliasChoices("value", "price", "kw"),
        description="The value in this period (price per kWh, or kW).",
    )

    @field_validator("start")
    @classmethod
    def _check_start(cls, v: str) -> str:
        parse_clock(v)
        return v.strip()

    @field_validator("end")
    @classmethod
    def _check_end(cls, v: str) -> str:
        parse_clock(v, allow_24=True)
        return v.strip()

    @model_validator(mode="after")
    def _not_empty(self) -> "Period":
        # 00:00 to 24:00 is the whole day; 07:00 to 07:00 is unclear
        if parse_clock(self.start) == parse_clock(self.end, allow_24=True):
            raise ValueError(f"period {self.start} to {self.end} has the same start and end")
        return self

    def minutes(self) -> tuple[int, int]:
        """(start, end) in minutes after midnight. end <= start means the
        period wraps past midnight; 00:00 to 24:00 gives (0, 0), the whole day."""
        return parse_clock(self.start), parse_clock(self.end, allow_24=True) % MINUTES_PER_DAY


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _finite(x: float) -> float:
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"must be a number, not {x!r} (NaN and infinity are not allowed)")
    return x


def _normalize_profile(value):
    """Turn a profile into a float, a list of 24 floats, or a list of Periods."""
    if isinstance(value, bool):
        raise ValueError(PROFILE_HELP)
    if isinstance(value, (int, float)):
        return _finite(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            raise ValueError(PROFILE_HELP) from None
        return _finite(number)
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("is an empty list; " + PROFILE_HELP)
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
            if len(value) != 24:
                raise ValueError(
                    f"has {len(value)} numbers; a list of numbers must have exactly 24 values, "
                    "one for each hour starting at 00:00"
                )
            return [_finite(x) for x in value]
        if all(isinstance(x, (dict, Period)) for x in value):
            periods = []
            for i, item in enumerate(value, start=1):
                if isinstance(item, Period):
                    periods.append(item)
                    continue
                try:
                    periods.append(Period.model_validate(item))
                except ValidationError as exc:
                    raise ValueError(f"period {i}: {_short_errors(exc)}") from None
            _check_coverage(periods)
            return periods
    raise ValueError(PROFILE_HELP)


def _check_coverage(periods: list[Period]) -> None:
    """Every minute of the day must be covered by exactly one period."""
    cover = [0] * MINUTES_PER_DAY
    owner: list[Period | None] = [None] * MINUTES_PER_DAY
    for p in periods:
        start, end = p.minutes()
        span = range(start, end) if start < end else list(range(start, MINUTES_PER_DAY)) + list(range(0, end))
        for m in span:
            if cover[m]:
                other = owner[m]
                raise ValueError(
                    f"periods {other.start}-{other.end} and {p.start}-{p.end} overlap at {clock_label(m)}"
                )
            cover[m] = 1
            owner[m] = p
    for m, c in enumerate(cover):
        if not c:
            raise ValueError(f"no period covers {clock_label(m)}; the periods must cover the whole day")


Profile = Annotated[
    Union[float, list[float], list[Period]],
    BeforeValidator(_normalize_profile),
]


def profile_value(profile, minute_of_day: int) -> float:
    """Value of a profile at a minute of the day."""
    minute_of_day %= MINUTES_PER_DAY
    if isinstance(profile, (int, float)):
        return float(profile)
    if profile and isinstance(profile[0], Period):
        for p in profile:
            start, end = p.minutes()
            inside = start <= minute_of_day < end if start < end else (minute_of_day >= start or minute_of_day < end)
            if inside:
                return p.value
        raise ValueError(f"no value at {clock_label(minute_of_day)}")  # guarded by validation
    return float(profile[minute_of_day // 60])


def profile_values(profile) -> list[float]:
    """All distinct values a profile can take (used for range checks)."""
    if isinstance(profile, (int, float)):
        return [float(profile)]
    if profile and isinstance(profile[0], Period):
        return [p.value for p in profile]
    return [float(x) for x in profile]


Time = Annotated[str, BeforeValidator(lambda v: check_time(str(v)) if v is not None else v)]


class ChargerGroup(BaseModel):
    """A group of identical chargers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    count: int = Field(ge=1, le=10000, description="Number of chargers in this group.")
    kw: float = Field(gt=0, le=2000, description="Maximum power of each charger, kW.")
    name: str | None = Field(None, description="Optional label, like 'AC 19.2 kW'.")


class Site(BaseModel):
    """The depot: chargers and power limits."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    power_limit_kw: Profile = Field(
        description="Most power the whole site may import, kW. A number, or a value by time of day."
    )
    other_load_kw: Profile = Field(
        0.0,
        description="Other building load (lights, workshop, offices), kW. Charging can only use "
        "power_limit_kw minus this. A number, or a value by time of day.",
    )
    chargers: list[ChargerGroup] = Field(min_length=1, description="Charger groups at the site.")
    charging_efficiency: float = Field(
        1.0,
        gt=0.5,
        le=1.0,
        description="Share of grid energy that ends up in the battery (for example 0.92).",
    )

    @field_validator("power_limit_kw")
    @classmethod
    def _limit_positive(cls, v):
        if min(profile_values(v)) <= 0:
            raise ValueError("must be greater than 0 kW at all times")
        return v

    @field_validator("other_load_kw")
    @classmethod
    def _load_not_negative(cls, v):
        if min(profile_values(v)) < 0:
            raise ValueError("cannot be negative")
        return v


class Tariff(BaseModel):
    """Electricity prices."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field("Tariff", description="A name for this tariff.")
    currency: str = Field("USD", description="Currency code, only used for display.")
    energy_price_per_kwh: Profile = Field(
        description="Energy price per kWh. A number, 24 hourly values, or periods by time of day."
    )
    demand_charge_per_kw: float = Field(
        0.0,
        ge=0,
        description="Demand charge per kW of the monthly peak.",
    )
    billing_days: float = Field(
        30.0,
        ge=1,
        le=31,
        description="Days in a billing month. A one-day plan carries 1/billing_days of the monthly "
        "demand charge (as if every day has the same peak). Use 1 to count the full monthly charge.",
    )


class Stop(BaseModel):
    """One parked period at the depot."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    arrival: Time = Field(description="When the vehicle parks. HH:MM or a full date and time.")
    departure: Time = Field(description="When the vehicle must leave. HH:MM or a full date and time.")
    required_soc_pct: float = Field(ge=0, le=100, description="Battery level needed at departure, percent.")
    trip_kwh: float = Field(
        0.0,
        ge=0,
        description="Energy used on the road between the previous departure and this arrival, kWh. "
        "Ignored for the first stop.",
    )


class Vehicle(BaseModel):
    """A vehicle. Give arrival, departure and required_soc_pct for one stop, or a list of stops."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, description="Vehicle name or number.")
    battery_kwh: float = Field(gt=0, le=5000, description="Usable battery size, kWh.")
    max_kw: float = Field(gt=0, le=2000, description="Fastest charging power the vehicle accepts, kW.")
    arrival_soc_pct: float = Field(ge=0, le=100, description="Battery level when it first arrives, percent.")
    arrival: Time | None = Field(None, description="Arrival time (single-stop form).")
    departure: Time | None = Field(None, description="Departure time (single-stop form).")
    required_soc_pct: float | None = Field(
        None, ge=0, le=100, description="Battery level needed at departure, percent (single-stop form)."
    )
    stops: list[Stop] | None = Field(
        None, description="Several stops in one plan, for example a midday break and the night."
    )

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v):
        return str(v).strip() if v is not None else v

    @model_validator(mode="after")
    def _one_form(self) -> "Vehicle":
        single = {
            "arrival": self.arrival,
            "departure": self.departure,
            "required_soc_pct": self.required_soc_pct,
        }
        if self.stops:
            given = [k for k, v in single.items() if v is not None]
            if given:
                raise ValueError(
                    f"give either {', '.join(given)} or a stops list, not both"
                )
        else:
            missing = [k for k, v in single.items() if v is None]
            if missing:
                raise ValueError(f"missing {', '.join(missing)} (or give a stops list)")
        return self

    def all_stops(self) -> list[Stop]:
        if self.stops:
            return list(self.stops)
        return [
            Stop(
                arrival=self.arrival,
                departure=self.departure,
                required_soc_pct=self.required_soc_pct,
            )
        ]


ShortfallPolicy = Literal["most_ready", "fair"]
SHORTFALL_POLICIES: tuple[str, ...] = ("most_ready", "fair")


class Fleet(BaseModel):
    """Everything Berth needs to plan charging."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field("My fleet", description="A name for this plan.")
    description: str | None = Field(None, description="Optional notes about this fleet.")
    start: Time | None = Field(
        None,
        description="Plan start, HH:MM or a full date and time. Default: the earliest arrival. "
        "Clock times after this are read in order, so 06:00 after an 18:00 arrival means the next morning.",
    )
    hours: float | None = Field(
        None, gt=0, le=168, description="Plan length in hours. Default: until the last departure."
    )
    step_minutes: int = Field(15, description="Time step in minutes: 5, 10, 15, 20, 30 or 60.")
    time_limit_s: float = Field(30.0, gt=0, le=600, description="Time limit for the solver, seconds.")
    shortfall_policy: ShortfallPolicy = Field(
        "most_ready",
        description="What to do when not every vehicle can be ready. most_ready (default): get as many "
        "vehicles fully ready as possible, then keep the biggest shortfall of the others as small as "
        "possible. fair: keep the biggest shortfall as small as possible over all vehicles, even if "
        "fewer vehicles are fully ready.",
    )
    site: Site
    tariff: Tariff
    vehicles: list[Vehicle] = Field(min_length=1)

    @field_validator("step_minutes")
    @classmethod
    def _step_ok(cls, v: int) -> int:
        if v not in (5, 10, 15, 20, 30, 60):
            raise ValueError("must be 5, 10, 15, 20, 30 or 60")
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> "Fleet":
        seen: set[str] = set()
        dupes: list[str] = []
        for v in self.vehicles:
            if v.id in seen and v.id not in dupes:
                dupes.append(v.id)
            seen.add(v.id)
        if dupes:
            raise ValueError(
                "vehicle ids must be unique; repeated: " + ", ".join(dupes)
                + " (to give one vehicle several stops, use a stops list)"
            )
        return self
