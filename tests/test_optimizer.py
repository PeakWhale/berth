"""The optimized plan meets departures, keeps every limit, and costs no more
than charging on arrival."""

from __future__ import annotations

import numpy as np
import pytest

from berth.loaders import load_fleet_file
from berth.problem import build_problem
from berth.optimizer import optimize
from berth.evaluate import evaluate
from berth.baseline import baseline_power

from conftest import EXAMPLES, assert_valid, make_fleet, multi_stop_fleet, random_fleet, solve, van


def test_every_vehicle_reaches_required_level():
    fleet = make_fleet([van(1), van(2, arrival_soc_pct=10, required_soc_pct=100), van(3, battery_kwh=60)])
    prob, opt, m, _ = solve(fleet)
    assert m.all_ready
    for o in m.stops:
        assert o.departure_kwh >= o.stop.required_kwh - 0.01
    assert_valid(prob, opt.power)


def test_energy_is_counted_with_efficiency():
    fleet = make_fleet([van(1, battery_kwh=100, arrival_soc_pct=20, required_soc_pct=80)], charging_efficiency=0.9)
    prob, opt, m, _ = solve(fleet)
    # 60 kWh into the battery needs 60 / 0.9 kWh from the grid
    assert m.grid_kwh == pytest.approx(60 / 0.9, abs=0.02)
    assert m.stops[0].departure_kwh == pytest.approx(80, abs=0.02)


def test_charging_moves_to_cheap_hours():
    fleet = make_fleet([van(1)])
    fleet["tariff"]["demand_charge_per_kw"] = 0
    prob, opt, m, base = solve(fleet)
    expensive = prob.price > 0.15
    assert opt.power[:, expensive].sum() == pytest.approx(0, abs=1e-6)
    assert m.energy_cost < base.energy_cost


def test_never_charges_when_not_parked():
    fleet = make_fleet([van(1, arrival="20:00", departure="23:00", required_soc_pct=40)])
    prob, opt, _, _ = solve(fleet)
    s = prob.vehicles[0].stops[0]
    outside = np.ones(prob.n_steps, dtype=bool)
    outside[s.a : s.d] = False
    assert opt.power[0, outside].sum() == 0


def test_vehicle_and_charger_power_limits():
    fleet = make_fleet(
        [van(1, max_kw=7.4), van(2, max_kw=50), van(3, max_kw=50, arrival="23:00", departure="04:00")],
        chargers=[{"count": 3, "kw": 22}],
    )
    prob, opt, m, _ = solve(fleet)
    assert opt.power[0].max() <= 7.4 + 1e-6
    assert opt.power[1:].max() <= 22 + 1e-6  # chargers are 22 kW
    assert_valid(prob, opt.power)


def test_site_limit_with_time_varying_other_load():
    other = [{"from": "16:00", "to": "23:00", "kw": 50}, {"from": "23:00", "to": "16:00", "kw": 10}]
    fleet = make_fleet([van(i, max_kw=22) for i in range(5)], power_limit_kw=80, other_load_kw=other)
    prob, opt, m, _ = solve(fleet)
    total = opt.power.sum(axis=0)
    assert np.all(total <= prob.limit_kw - prob.other_kw + 1e-6)
    assert m.peak_site_kw <= 80 + 1e-6
    assert m.all_ready


def test_charger_count_is_respected():
    # 6 vans, 2 chargers: at most 2 charge in any time step
    fleet = make_fleet(
        [van(i, max_kw=22, battery_kwh=60, arrival_soc_pct=50, required_soc_pct=80) for i in range(6)],
        chargers=[{"count": 2, "kw": 22}],
    )
    prob, opt, m, _ = solve(fleet)
    assert m.all_ready
    assert ((opt.power > 1e-3).sum(axis=0) <= 2).all()
    assert opt.binaries > 0
    assert_valid(prob, opt.power)


def test_mixed_charger_groups_fast_chargers_limited():
    fleet = make_fleet(
        [van(i, max_kw=50, battery_kwh=100, arrival="22:00", departure="02:00", arrival_soc_pct=10, required_soc_pct=60)
         for i in range(4)],
        chargers=[{"count": 1, "kw": 50}, {"count": 3, "kw": 11}],
        power_limit_kw=500,
    )
    prob, opt, m, _ = solve(fleet)
    assert_valid(prob, opt.power)
    # never more than one vehicle above 11 kW at the same time
    assert ((opt.power > 11 + 1e-3).sum(axis=0) <= 1).all()


def test_battery_never_above_full():
    fleet = make_fleet([van(1, arrival_soc_pct=95, required_soc_pct=100)])
    fleet["tariff"]["energy_price_per_kwh"] = -0.05  # paid to use power: still cannot overfill
    prob, opt, m, _ = solve(fleet)
    assert m.stops[0].departure_kwh <= 80 + 1e-6
    assert_valid(prob, opt.power)


def test_multi_stop_vehicle_trip_energy():
    bus = {
        "id": "BUS",
        "battery_kwh": 200,
        "max_kw": 60,
        "arrival_soc_pct": 40,
        "stops": [
            {"arrival": "09:00", "departure": "14:00", "required_soc_pct": 70},
            {"arrival": "17:00", "departure": "06:00", "required_soc_pct": 90, "trip_kwh": 80},
        ],
    }
    fleet = make_fleet([bus], chargers=[{"count": 1, "kw": 60}])
    fleet["start"] = "09:00"
    prob, opt, m, _ = solve(fleet)
    assert m.all_ready
    first, second = m.stops
    assert first.departure_kwh >= 140 - 0.01
    # arrival at stop 2 = departure 1 - trip, then charged up to 90 %
    charged_2 = prob.efficiency * prob.dt * opt.power[0, second.stop.a : second.stop.d].sum()
    assert second.departure_kwh == pytest.approx(first.departure_kwh - 80 + charged_2)
    assert second.departure_kwh >= 180 - 0.01


def _bus(arrival_soc_pct, first_stop):
    return {
        "id": "BUS",
        "battery_kwh": 60,
        "max_kw": 11,
        "arrival_soc_pct": arrival_soc_pct,
        "stops": [
            {"arrival": first_stop[0], "departure": first_stop[1], "required_soc_pct": 0},
            {"arrival": "18:00", "departure": "06:00", "required_soc_pct": 90, "trip_kwh": 50},
        ],
    }


def _levels(prob, power):
    """Battery level at each departure, computed by hand: never below empty."""
    out = []
    for v, veh in enumerate(prob.vehicles):
        level = veh.arrival_kwh
        for s in veh.stops:
            level = max(0.0, level - s.trip_kwh) + prob.efficiency * prob.dt * power[v, s.a : s.d].sum()
            out.append(level)
    return out


def test_vehicle_that_cannot_make_its_trip_arrives_empty():
    # 30 minutes at 11 kW cannot give the 50 kWh trip: the bus arrives empty,
    # so the night stop needs 54 kWh (90 %), not 54 kWh plus what it lacked
    fleet = make_fleet([_bus(10, ("08:00", "08:30"))], chargers=[{"count": 2, "kw": 11}])
    fleet["start"] = "08:00"
    prob, opt, m, _ = solve(fleet)
    assert_valid(prob, opt.power)
    first, second = m.stops
    assert not first.ready and second.ready
    night = prob.efficiency * prob.dt * opt.power[0, second.stop.a : second.stop.d].sum()
    assert night == pytest.approx(54, abs=0.01)  # never more than the battery holds
    assert [o.departure_kwh for o in m.stops] == pytest.approx(_levels(prob, opt.power))


@pytest.mark.parametrize("policy", ["most_ready", "fair"])
def test_running_out_does_not_take_power_from_others(policy):
    # the site gives 8 kW at night (96 kWh): the empty bus needs 54, the van 42
    van_ = {"id": "VAN", "battery_kwh": 60, "max_kw": 11, "arrival": "18:00", "departure": "06:00",
            "arrival_soc_pct": 20, "required_soc_pct": 90}
    for first_stop, other_load in [
        (("08:00", "08:30"), 0),  # the bus can never make its trip
        (("08:00", "12:00"), [{"from": "08:00", "to": "12:00", "kw": 6}, {"from": "12:00", "to": "08:00", "kw": 0}]),
    ]:  # the second time the bus could make it alone, but the site gives it too little
        fleet = make_fleet([_bus(10 if other_load == 0 else 50, first_stop), van_], power_limit_kw=8,
                           other_load_kw=other_load, chargers=[{"count": 2, "kw": 11}])
        fleet["start"] = "08:00"
        fleet["shortfall_policy"] = policy
        prob, opt, m, _ = solve(fleet)
        assert_valid(prob, opt.power)
        assert [o.ready for o in m.stops] == [False, True, True]
        assert [o.departure_kwh for o in m.stops] == pytest.approx(_levels(prob, opt.power))


def test_demand_charge_flattens_peak():
    vehicles = [van(i, max_kw=22, battery_kwh=100, arrival_soc_pct=10) for i in range(4)]
    fleet = make_fleet(vehicles, power_limit_kw=200)
    fleet["tariff"]["energy_price_per_kwh"] = 0.15
    fleet["tariff"]["demand_charge_per_kw"] = 20
    prob, opt, m, base = solve(fleet)
    # 4 x 80 kWh spread over 12 hours is about 26.7 kW; on arrival it is 88 kW
    assert m.peak_charging_kw == pytest.approx(320 / 12, abs=0.5)
    assert base.peak_charging_kw == pytest.approx(88)
    assert m.demand_cost < base.demand_cost


@pytest.mark.parametrize("seed", range(12))
def test_random_fleets_valid_and_not_worse_than_baseline(seed):
    # Some of these small fleets share very few chargers and are hard to
    # prove optimal. A short time limit keeps the tests fast; the promises
    # below hold even when the solver stops early.
    prob, opt, m, base = solve(random_fleet(seed), time_limit=10)
    assert_valid(prob, opt.power)
    assert_valid(prob, baseline_power(prob))
    # never fewer vehicles ready than charging on arrival (default policy most_ready)
    assert m.vehicles_ready >= base.vehicles_ready
    if base.all_ready:
        assert m.all_ready
        assert m.total_cost <= base.total_cost + 1e-6


@pytest.mark.parametrize("seed", range(8))
def test_random_multi_stop_fleets(seed):
    prob, opt, m, base = solve(multi_stop_fleet(seed), time_limit=10)
    assert_valid(prob, opt.power)
    assert [o.departure_kwh for o in m.stops] == pytest.approx(_levels(prob, opt.power))
    if prob.shortfall_policy == "most_ready":
        assert m.vehicles_ready >= base.vehicles_ready
    if base.all_ready:
        assert m.all_ready
        assert m.total_cost <= base.total_cost + 1e-6


@pytest.mark.parametrize("name", ["delivery-vans", "school-buses", "late-shift-infeasible"])
def test_examples_valid(name):
    prob = build_problem(load_fleet_file(EXAMPLES / f"{name}.yaml"))
    opt = optimize(prob)
    m = evaluate(prob, opt.power)
    base = evaluate(prob, baseline_power(prob))
    assert_valid(prob, opt.power)
    assert_valid(prob, baseline_power(prob))
    if name != "late-shift-infeasible":
        assert m.all_ready
        assert m.total_cost <= base.total_cost + 1e-6
    assert opt.optimal
