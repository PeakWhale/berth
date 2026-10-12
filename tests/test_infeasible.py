"""When not every vehicle can be ready: who falls short, by how much, why,
and the best partial plan."""

from __future__ import annotations

import copy

import pytest

from berth.diagnose import CHARGERS, SITE, TIME
from berth.loaders import load_fleet_file
from berth.planner import plan

from conftest import EXAMPLES, make_fleet, van


def test_not_enough_time_is_reported_with_kwh():
    # parked 1 hour at 11 kW: at most 11 kWh, but needs 0.9*80 - 0.2*80 = 56 kWh
    fleet = make_fleet([van(1, arrival="20:00", departure="21:00")])
    r = plan(fleet)
    assert not r.all_ready
    assert r.headline.startswith("1 of 1 vehicles cannot be fully ready")
    (item,) = r.shortfalls
    assert item.vehicle_id == "V1"
    assert item.shortfall_kwh == pytest.approx(56 - 11, abs=0.01)
    assert item.reason_codes == [TIME]
    assert "Not enough time" in item.reasons[0]
    # it still gets everything it can take
    assert r.vehicles[0].grid_kwh == pytest.approx(11, abs=0.01)


def test_site_limit_is_reported_and_extra_kw_fixes_it():
    # 3 vans need 3 x 56 = 168 kWh in 12 h, the site gives only 10 kW (120 kWh)
    fleet = make_fleet([van(i) for i in range(3)], power_limit_kw=10)
    r = plan(fleet)
    assert not r.all_ready
    assert {s.vehicle_id for s in r.shortfalls} == {"V0", "V1", "V2"}
    for s in r.shortfalls:
        assert s.reason_codes == [SITE]
        assert "Site power limit" in s.reasons[0]
    # best partial plan uses all available energy and shares the shortfall evenly
    assert r.optimized.grid_kwh == pytest.approx(120, abs=0.05)
    shorts = [s.shortfall_kwh for s in r.shortfalls]
    assert max(shorts) - min(shorts) < 0.05
    assert r.diagnosis.binding_limits == [SITE]
    extra = r.diagnosis.extra_site_kw
    assert extra is not None and extra > 0
    # applying the suggested extra capacity makes every van ready
    bigger = copy.deepcopy(fleet)
    bigger["site"]["power_limit_kw"] = 10 + extra
    assert plan(bigger).all_ready


def test_too_few_chargers_is_reported_and_extra_chargers_fix_it():
    # 4 vans need 56 kWh each at 11 kW = 5.1 h of charger time; 12 h x 1 charger is not enough
    fleet = make_fleet([van(i) for i in range(4)], power_limit_kw=500, chargers=[{"count": 1, "kw": 11}])
    r = plan(fleet)
    assert not r.all_ready
    for s in r.shortfalls:
        assert s.reason_codes == [CHARGERS]
        assert "Too few chargers" in s.reasons[0]
    assert r.diagnosis.binding_limits == [CHARGERS]
    n = r.diagnosis.extra_chargers
    assert n is not None and n >= 1
    more = copy.deepcopy(fleet)
    more["site"]["chargers"] = [{"count": 1 + n, "kw": 11}]
    assert plan(more).all_ready
    fewer = copy.deepcopy(fleet)
    fewer["site"]["chargers"] = [{"count": n, "kw": 11}]
    assert not plan(fewer).all_ready  # the suggestion is the smallest number


def test_both_limits_reported_when_neither_alone_is_enough():
    # tight site and few chargers at the same time
    fleet = make_fleet([van(i, max_kw=22) for i in range(6)], power_limit_kw=25, chargers=[{"count": 1, "kw": 22}])
    r = plan(fleet)
    assert not r.all_ready
    assert sorted(r.diagnosis.binding_limits) == sorted([SITE, CHARGERS])
    assert any("both" in line for line in r.diagnosis.summary)


def test_feasible_vehicles_stay_ready_when_others_fall_short():
    # V1 cannot make it (short stop); V2 has plenty of time and must still be ready
    fleet = make_fleet([van(1, arrival="20:00", departure="20:30"), van(2)])
    r = plan(fleet)
    ready = {v.id: v.ready for v in r.vehicles}
    assert ready == {"V1": False, "V2": True}


def test_example_late_shift():
    r = plan(load_fleet_file(EXAMPLES / "late-shift-infeasible.yaml"))
    assert not r.all_ready
    short = {s.vehicle_id: s for s in r.shortfalls}
    assert TIME in short["SHORT-STOP"].reason_codes
    for vid in ("EARLY-1", "EARLY-2", "EARLY-3", "EARLY-4", "EARLY-5"):
        assert SITE in short[vid].reason_codes
    for v in r.vehicles:
        if v.id.startswith("DAY-"):
            assert v.ready
    # Berth keeps the biggest shortfall smaller than charging on arrival
    assert r.optimized.max_shortfall_kwh < r.baseline.max_shortfall_kwh
