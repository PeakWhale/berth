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
