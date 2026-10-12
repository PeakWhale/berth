"""Reading YAML and CSV, times, and clear error messages."""

from __future__ import annotations

import copy
import textwrap

import pytest

from berth.errors import BerthInputError
from berth.examples import list_examples, load_example
from berth.loaders import (
    fleet_to_yaml,
    load_fleet_file,
    load_fleet_text,
    parse_chargers,
    read_tariff_csv,
    read_vehicles_csv,
)
from berth.planner import parse_fleet, plan
from berth.problem import build_problem

from conftest import EXAMPLES, make_fleet, van


def problems_of(exc_info) -> str:
    return "\n".join(exc_info.value.problems)


# ---- YAML ------------------------------------------------------------------------


def test_yaml_syntax_error_has_line_number():
    with pytest.raises(BerthInputError) as e:
        load_fleet_text("name: x\nsite: [1, 2\nvehicles: []\n", "fleet.yaml")
    assert "not valid YAML" in str(e.value)
    assert "line " in str(e.value)
    assert "fleet.yaml" in str(e.value)


def test_unquoted_clock_times_in_yaml():
    # YAML 1.1 would read 17:30 as the number 1050; Berth keeps it as a time
    text = textwrap.dedent("""\
        start: 16:00
        site:
          power_limit_kw: 100
          other_load_kw: [{from: 00:00, to: 16:00, kw: 5}, {from: 16:00, to: 24:00, kw: 10}]
          chargers: [{count: 2, kw: 11}]
        tariff: {energy_price_per_kwh: 0.2}
        vehicles:
          - {id: A, battery_kwh: 60, max_kw: 11, arrival: 17:30, departure: 06:00, arrival_soc_pct: 20, required_soc_pct: 80}
        """)
    fleet = load_fleet_text(text)
    assert fleet.start == "16:00"
    assert fleet.vehicles[0].arrival == "17:30"
    assert fleet.site.other_load_kw[1].end == "24:00"
    assert fleet.site.power_limit_kw == 100


def test_empty_and_non_mapping_yaml():
    with pytest.raises(BerthInputError, match="empty"):
        load_fleet_text("", "f")
    with pytest.raises(BerthInputError, match="mapping"):
        load_fleet_text("- just\n- a list\n", "f")


def test_missing_and_misspelled_fields_name_the_vehicle():
    fleet = make_fleet([van(1), van(2)])
    del fleet["vehicles"][1]["battery_kwh"]
    fleet["vehicles"][0]["max_kW"] = 11
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet, "fleet.yaml")
    text = problems_of(e)
    assert "vehicle 2 (V2): battery_kwh: is required but missing" in text
    assert "vehicle 1 (V1): max_kW: is not a known field" in text


def test_missing_chargers_names_the_site():
    fleet = make_fleet([van(1)])
    del fleet["site"]["chargers"]
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert "site.chargers: is required but missing" in problems_of(e)
    fleet["site"]["chargers"] = [{"count": 0, "kw": 11}]
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert "site.chargers group 1: count" in problems_of(e)


def test_bad_numbers_and_ranges():
    fleet = make_fleet([van(1, required_soc_pct=120, battery_kwh=-5)])
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    text = problems_of(e)
    assert "required_soc_pct" in text and "less than or equal to 100" in text
    assert "battery_kwh" in text and "greater than 0" in text


def test_bad_time_is_explained():
    with pytest.raises(BerthInputError) as e:
        parse_fleet(make_fleet([van(1, arrival="25:00")]))
    assert "'25:00' is not a valid" in problems_of(e)


def test_shortfall_policy_values():
    assert parse_fleet(make_fleet([van(1)])).shortfall_policy == "most_ready"
    assert parse_fleet({**make_fleet([van(1)]), "shortfall_policy": "fair"}).shortfall_policy == "fair"
    with pytest.raises(BerthInputError) as e:
        parse_fleet({**make_fleet([van(1)]), "shortfall_policy": "best"})
    assert "shortfall_policy: Input should be 'most_ready' or 'fair'" in problems_of(e)


def test_currency_must_be_a_three_letter_code():
    fleet = make_fleet([van(1)])
    fleet["tariff"]["currency"] = " eur "
    assert parse_fleet(fleet).tariff.currency == "EUR"
    fleet["tariff"]["currency"] = "<img src=x onerror=alert(1)>"
    with pytest.raises(BerthInputError, match="three-letter currency code"):
        parse_fleet(fleet)


def test_duplicate_vehicle_ids():
    with pytest.raises(BerthInputError, match="unique"):
        parse_fleet(make_fleet([van(1), van(1)]))


def test_tariff_periods_must_cover_the_day_without_overlap():
    fleet = make_fleet([van(1)])
    fleet["tariff"]["energy_price_per_kwh"] = [{"from": "00:00", "to": "06:00", "price": 0.1},
                                               {"from": "07:00", "to": "24:00", "price": 0.2}]
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert "no period covers 06:00" in problems_of(e)
    fleet["tariff"]["energy_price_per_kwh"] = [{"from": "00:00", "to": "12:00", "price": 0.1},
                                               {"from": "11:00", "to": "00:00", "price": 0.2}]
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert "overlap at 11:00" in problems_of(e)


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("tariff", "energy_price_per_kwh", float("nan")),
        ("tariff", "energy_price_per_kwh", "nan"),
        ("site", "power_limit_kw", float("inf")),
        ("site", "power_limit_kw", [{"from": "00:00", "to": "24:00", "kw": float("nan")}]),
        ("site", "other_load_kw", [5.0] * 23 + [float("nan")]),
        ("tariff", "demand_charge_per_kw", float("nan")),
    ],
)
def test_nan_and_infinity_are_refused(section, key, value):
    fleet = make_fleet([van(1)])
    fleet[section][key] = value
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert key in problems_of(e)
    assert "finite" in problems_of(e) or "NaN" in problems_of(e)


def test_nan_in_csv_is_refused():
    with pytest.raises(BerthInputError, match="price_per_kwh 'nan' is not a number"):
        read_tariff_csv("hour,price_per_kwh\n" + "\n".join(f"{h},{'nan' if h == 3 else 0.1}" for h in range(24)), "t.csv")


def test_whole_day_period():
    fleet = make_fleet([van(1)])
    fleet["site"]["power_limit_kw"] = [{"from": "00:00", "to": "24:00", "kw": 100}]
    prob = build_problem(parse_fleet(fleet))
    assert (prob.limit_kw == 100).all()
    fleet["site"]["power_limit_kw"] = [{"from": "07:00", "to": "07:00", "kw": 100}]
    with pytest.raises(BerthInputError, match="same start and end"):
        parse_fleet(fleet)


def test_hourly_list_needs_24_values():
    fleet = make_fleet([van(1)])
    fleet["tariff"]["energy_price_per_kwh"] = [0.1] * 23
    with pytest.raises(BerthInputError) as e:
        parse_fleet(fleet)
    assert "exactly 24 values" in problems_of(e)
    fleet["tariff"]["energy_price_per_kwh"] = [0.1] * 6 + [0.3] * 18
    f = parse_fleet(fleet)
    prob = build_problem(f)
    assert prob.price[0] == pytest.approx(0.3)  # 16:00


def test_wrapping_periods_and_single_vs_stops_form():
    fleet = make_fleet([van(1)])
    fleet["site"]["other_load_kw"] = [{"from": "22:00", "to": "06:00", "kw": 5}, {"from": "06:00", "to": "22:00", "kw": 30}]
    prob = build_problem(parse_fleet(fleet))
    assert prob.other_kw[0] == 30 and prob.other_kw[-1] == 5
    both = make_fleet([dict(van(1), stops=[{"arrival": "18:00", "departure": "06:00", "required_soc_pct": 90}])])
    with pytest.raises(BerthInputError, match="not both"):
        parse_fleet(both)


def test_yaml_alias_bomb_is_refused_quickly():
    import time

    # 16 KB of YAML that stands for 2000 vehicles with 2000 stops each
    n = 2000
    stops = '[&s {arrival: "00:00", departure: "01:00", required_soc_pct: 0}' + ", *s" * (n - 1) + "]"
    vehicles = "[&v {id: a, battery_kwh: 60, max_kw: 11, arrival_soc_pct: 10, stops: " + stops + "}" + ", *v" * (n - 1) + "]"
    text = "site: {power_limit_kw: 10, chargers: [{count: 1, kw: 7}]}\ntariff: {energy_price_per_kwh: 0.1}\nvehicles: " + vehicles
    started = time.perf_counter()
    with pytest.raises(BerthInputError, match="aliases"):
        load_fleet_text(text)
    assert time.perf_counter() - started < 2


def test_yaml_aliases_still_work_for_normal_files():
    text = textwrap.dedent("""\
        site: {power_limit_kw: 50, chargers: [{count: 2, kw: 11}]}
        tariff: {energy_price_per_kwh: 0.2}
        vehicles:
          - &van {id: A, battery_kwh: 60, max_kw: 11, arrival: "18:00", departure: "06:00", arrival_soc_pct: 20, required_soc_pct: 80}
          - {<<: *van, id: B}
        """)
    assert [v.id for v in load_fleet_text(text).vehicles] == ["A", "B"]


def test_fleet_size_limits():
    fleet = make_fleet([van(i) for i in range(2001)])
    with pytest.raises(BerthInputError, match="at most 2000"):
        parse_fleet(fleet)


def test_file_references_only_on_the_command_line(tmp_path):
    text = "name: x\ntariff: /etc/passwd\nsite: {power_limit_kw: 10, chargers: [{count: 1, kw: 7}]}\nvehicles: []\n"
    with pytest.raises(BerthInputError, match="file name"):
        load_fleet_text(text, "request")


def test_missing_referenced_file(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text("name: x\ntariff: nope.yaml\nsite: {power_limit_kw: 10, chargers: [{count: 1, kw: 7}]}\nvehicles: []\n")
    with pytest.raises(BerthInputError, match="file not found.*nope.yaml"):
        load_fleet_file(p)


# ---- times -----------------------------------------------------------------------


def test_departure_before_arrival_means_next_day():
    prob = build_problem(parse_fleet(make_fleet([van(1, arrival="18:00", departure="06:00")])))
    s = prob.vehicles[0].stops[0]
    assert s.departure_min - s.arrival_min == 12 * 60
    assert s.departure_label == "06:00 +1d"


def test_default_start_handles_midnight():
    fleet = make_fleet([van(1, arrival="22:00", departure="06:00"), van(2, arrival="00:30", departure="07:00")])
    del fleet["start"]
    prob = build_problem(parse_fleet(fleet))
    assert prob.labels[0] == "22:00"


def test_full_dates_are_supported():
    fleet = make_fleet([van(1, arrival="2026-03-02T18:00", departure="2026-03-03T06:30")])
    del fleet["start"]
    prob = build_problem(parse_fleet(fleet))
    assert prob.labels[0] == "2026-03-02 18:00"
    assert prob.n_steps == 50


def test_utc_offsets_are_converted_to_one_clock():
    # the same hour written in two time zones is the same hour
    fleet = make_fleet([
        van(1, arrival="2026-03-02T18:00Z", departure="2026-03-02T19:00Z"),
        van(2, arrival="2026-03-02T20:00+02:00", departure="2026-03-02T21:00+02:00"),
    ])
    del fleet["start"]
    prob = build_problem(parse_fleet(fleet))
    assert prob.labels[0] == "2026-03-02 18:00"
    assert [(s.a, s.d) for v in prob.vehicles for s in v.stops] == [(0, 4), (0, 4)]
    assert any("different UTC offsets" in n for n in prob.notes)


def test_daylight_saving_change_gives_the_real_parked_time():
    # 00:00 at UTC-8 to 04:00 at UTC-7 is 3 hours, not 4
    fleet = make_fleet([van(1, max_kw=10, arrival_soc_pct=0, required_soc_pct=100,
                            arrival="2026-03-08T00:00-08:00", departure="2026-03-08T04:00-07:00")])
    del fleet["start"]
    prob = build_problem(parse_fleet(fleet))
    stop = prob.vehicles[0].stops[0]
    assert stop.d - stop.a == 12  # 15-minute steps
    r = plan(fleet)
    assert r.vehicles[0].stops[0].departure_kwh == pytest.approx(30, abs=0.01)
    assert not r.all_ready


def test_departure_before_the_plan_start_is_explained():
    fleet = make_fleet([van(1, arrival="2026-03-02T13:00", departure="2026-03-02T15:00")])
    fleet["start"] = "2026-03-02T16:00"
    with pytest.raises(BerthInputError) as e:
        build_problem(parse_fleet(fleet))
    assert "departure 2026-03-02T15:00 is at or before the plan start (2026-03-02 16:00)" in problems_of(e)


def test_times_far_after_the_start_are_refused_quickly():
    import time

    # a far-future date with a clock-time departure used to loop once per day
    fleet = make_fleet([van(i, arrival="9999-12-31T00:00", departure="06:00") for i in range(50)])
    fleet["start"] = "2026-03-02T16:00"
    started = time.perf_counter()
    with pytest.raises(BerthInputError) as e:
        build_problem(parse_fleet(fleet))
    assert time.perf_counter() - started < 1.0
    assert "more than 168 hours after the plan start" in problems_of(e)


def test_hours_too_short_is_explained():
    fleet = make_fleet([van(1)])
    fleet["hours"] = 6
    with pytest.raises(BerthInputError, match="after the plan ends"):
        build_problem(parse_fleet(fleet))


def test_rounding_to_steps_is_conservative():
    prob = build_problem(parse_fleet(make_fleet([van(1, arrival="18:07", departure="18:58")])))
    s = prob.vehicles[0].stops[0]
    assert prob.labels[s.a] == "18:15"
    assert prob.edge_labels[s.d] == "18:45"


# ---- CSV ---------------------------------------------------------------------------


def test_vehicles_csv_single_and_multi_stop():
    text = textwrap.dedent("""\
        # comment line
        id,battery_kwh,max_kw,arrival,departure,arrival_soc_pct,required_soc_pct,trip_kwh
        V1,80,11,18:00,06:00,20,90,
        B1,200,60,09:00,14:00,50,70,
        B1,,,17:00,06:00,,90,80
        """)
    rows = read_vehicles_csv(text, "v.csv")
    assert rows[0]["arrival"] == "18:00"
    assert rows[1]["stops"][1]["trip_kwh"] == 80
    fleet = make_fleet(rows)
    fleet["start"] = "09:00"
    assert parse_fleet(fleet).vehicles[1].all_stops()[1].trip_kwh == 80


def test_vehicles_csv_errors_are_clear():
    with pytest.raises(BerthInputError) as e:
        read_vehicles_csv("id,battery_kwh,arrival\nV1,80,18:00\n", "v.csv")
    assert "missing column(s): max_kw, departure, required_soc_pct" in str(e.value)
    with pytest.raises(BerthInputError) as e:
        read_vehicles_csv(
            "id,battery_kwh,max_kw,arrival,departure,arrival_soc_pct,required_soc_pct\nV1,abc,11,18:00,06:00,20,90\n",
            "v.csv",
        )
    assert "line 2: battery_kwh 'abc' is not a number" in str(e.value)
    with pytest.raises(BerthInputError, match="unknown column"):
        read_vehicles_csv("id,battery_kwh,max_kw,arrival,departure,required_soc_pct,colour\n", "v.csv")
    with pytest.raises(BerthInputError, match="arrival_soc_pct is needed on the first row"):
        read_vehicles_csv(
            "id,battery_kwh,max_kw,arrival,departure,arrival_soc_pct,required_soc_pct\nV1,80,11,18:00,06:00,,90\n",
            "v.csv",
        )


def test_tariff_csv_hourly_and_periods():
    hourly = "hour,price_per_kwh\n" + "\n".join(f"{h},{0.1 if h < 7 else 0.3}" for h in range(24))
    t = read_tariff_csv(hourly, "t.csv")
    assert len(t["energy_price_per_kwh"]) == 24
    periods = "from,to,price\n00:00,07:00,0.1\n07:00,24:00,0.3\n"
    assert read_tariff_csv(periods, "t.csv")["energy_price_per_kwh"][1]["value"] == 0.3
    with pytest.raises(BerthInputError, match="no price for hour"):
        read_tariff_csv("hour,price_per_kwh\n0,0.1\n", "t.csv")


def test_parse_chargers():
    assert parse_chargers("8x19.2, 2x50kW") == [{"count": 8, "kw": 19.2}, {"count": 2, "kw": 50.0}]
    with pytest.raises(BerthInputError, match="COUNTxKW"):
        parse_chargers("eight chargers")


# ---- examples ------------------------------------------------------------------------


def test_examples_load_and_round_trip_through_yaml():
    names = {e["name"] for e in list_examples()}
    assert names == {"delivery-vans", "school-buses", "late-shift-infeasible"}
    for name in names:
        fleet = load_example(name)
        assert load_fleet_text(fleet_to_yaml(fleet)) == fleet


@pytest.mark.parametrize(
    "name, description",
    [("Depot\tNorth", "line one\nline two"), ("A\u2028B", "x\x85y"), (".5", "+1"), ("yes", "back\\slash")],
)
def test_yaml_output_quotes_special_text(name, description):
    fleet = load_example("delivery-vans").model_copy(update={"name": name, "description": description})
    back = load_fleet_text(fleet_to_yaml(fleet))
    assert (back.name, back.description) == (name, description)
    assert back == fleet


def test_csv_example_matches_yaml_example():
    yaml_fleet = load_fleet_file(EXAMPLES / "delivery-vans.yaml")
    rows = read_vehicles_csv((EXAMPLES / "csv" / "vans.csv").read_text(), "vans.csv")
    data = yaml_fleet.model_dump(by_alias=True, exclude_none=True)
    data["vehicles"] = rows
    assert parse_fleet(copy.deepcopy(data)).vehicles == yaml_fleet.vehicles
