"""HTTP API round trips with the FastAPI test client."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from berth.api import app

from conftest import EXAMPLES, make_fleet, van


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_health_and_docs(client):
    assert client.get("/api/health").json()["status"] == "ok"
    spec = client.get("/openapi.json").json()
    assert "/api/plan" in spec["paths"]
    assert client.get("/docs").status_code == 200


def test_web_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "PeakWhale Berth" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/logo.svg").status_code == 200


def test_examples_list_and_get(client):
    names = {e["name"] for e in client.get("/api/examples").json()}
    assert "delivery-vans" in names
    fleet = client.get("/api/examples/delivery-vans").json()
    assert len(fleet["vehicles"]) == 12
    assert isinstance(fleet["tariff"], dict)  # tariff file is included
    text = client.get("/api/examples/delivery-vans?format=yaml").text
    assert text.startswith("name: Delivery vans")
    assert client.get("/api/examples/nope").status_code == 400


def test_plan_round_trip_json(client):
    fleet = client.get("/api/examples/delivery-vans").json()
    res = client.post("/api/plan", json=fleet)
    assert res.status_code == 200
    r = res.json()
    assert r["all_ready"] is True
    assert r["optimized"]["total_cost"] <= r["baseline"]["total_cost"]
    assert len(r["times"]) == len(r["vehicles"][0]["power_kw"])
    assert r["solver"]["solver"] == "HiGHS"


def test_plan_yaml_text(client):
    text = client.get("/api/examples/late-shift-infeasible?format=yaml").text
    res = client.post("/api/plan/yaml", content=text, headers={"Content-Type": "text/plain"})
    assert res.status_code == 200
    r = res.json()
    assert r["all_ready"] is False
    assert any("not_enough_time" in s["reason_codes"] for s in r["shortfalls"])
    assert r["diagnosis"]["summary"]


def test_schedule_csv_and_explain(client):
    fleet = make_fleet([van(1), van(2)])
    csv = client.post("/api/schedule.csv", json=fleet)
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].endswith("V1_kw,V2_kw")
    text = client.post("/api/explain", json=fleet).text
    assert "All 2 vehicles will be ready" in text


def test_validation_errors_are_clear(client):
    fleet = make_fleet([van(1), van(2)])
    del fleet["vehicles"][1]["max_kw"]
    res = client.post("/api/plan", json=fleet)
    assert res.status_code == 422
    assert "vehicle 2 (V2): max_kw: is required but missing" in res.json()["problems"]


def test_nan_in_json_is_refused(client):
    import json

    fleet = client.get("/api/examples/delivery-vans").json()
    fleet["tariff"]["energy_price_per_kwh"] = float("nan")  # json.dumps writes NaN
    res = client.post("/api/plan", content=json.dumps(fleet), headers={"Content-Type": "application/json"})
    assert res.status_code == 422
    assert any("energy_price_per_kwh" in p and "NaN" in p for p in res.json()["problems"])


def test_yaml_errors_are_clear(client):
    res = client.post("/api/plan/yaml", content="site: [1, 2\n", headers={"Content-Type": "text/plain"})
    assert res.status_code == 400
    assert "not valid YAML" in res.json()["error"]


@pytest.mark.parametrize("path", ["/api/plan", "/api/explain", "/api/schedule.csv", "/api/plan/yaml"])
def test_large_requests_are_refused(client, path):
    big = b'{"name": "' + b"a" * 2_100_000 + b'"}'
    res = client.post(path, content=big, headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert "too large" in res.json()["error"]

    def chunks():  # no Content-Length: counted while reading
        for _ in range(30):
            yield b"a" * 100_000

    res = client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
    assert res.status_code == 413


def test_api_does_not_read_server_files(client):
    text = (EXAMPLES / "delivery-vans.yaml").read_text()  # has "tariff: tariff-tou.yaml"
    res = client.post("/api/plan/yaml", content=text, headers={"Content-Type": "text/plain"})
    assert res.status_code == 400
    assert "file name" in res.json()["error"]


def test_convert_endpoints(client):
    csv_text = (EXAMPLES / "csv" / "vans.csv").read_text()
    vehicles = client.post("/api/convert/vehicles-csv", content=csv_text, headers={"Content-Type": "text/plain"}).json()
    assert len(vehicles) == 12 and vehicles[0]["id"] == "VAN-01"
    fleet = client.get("/api/examples/delivery-vans").json()
    fleet["vehicles"] = vehicles
    yaml_text = client.post("/api/convert/to-yaml", json=fleet).text
    back = client.post("/api/convert/from-yaml", content=yaml_text, headers={"Content-Type": "text/plain"}).json()
    assert back["vehicles"] == fleet["vehicles"]
    bad = client.post("/api/convert/vehicles-csv", content="id,battery_kwh\nV1,80\n", headers={"Content-Type": "text/plain"})
    assert bad.status_code == 400
    assert "missing column" in bad.json()["error"]


def test_shortfall_policy_in_body_and_query(client):
    fleet = client.get("/api/examples/late-shift-infeasible").json()
    assert fleet["shortfall_policy"] == "most_ready"
    most = client.post("/api/plan", json=fleet).json()
    fair = client.post("/api/plan?shortfall_policy=fair", json=fleet).json()
    assert most["shortfall_policy"] == "most_ready" and fair["shortfall_policy"] == "fair"
    assert most["optimized"]["vehicles_ready"] >= most["baseline"]["vehicles_ready"]
    assert most["optimized"]["vehicles_ready"] > fair["optimized"]["vehicles_ready"]
    fleet["shortfall_policy"] = "fair"
    assert client.post("/api/plan", json=fleet).json()["shortfall_policy"] == "fair"
    fleet["shortfall_policy"] = "best"
    bad = client.post("/api/plan", json=fleet)
    assert bad.status_code == 422
    assert any("shortfall_policy" in p for p in bad.json()["problems"])
