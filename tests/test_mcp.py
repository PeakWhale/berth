"""MCP server tools, called the way an MCP client would."""

from __future__ import annotations

import asyncio
import json

from berth.mcp_server import explain_plan, plan_charging, server

from conftest import make_fleet, van


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def test_tools_are_listed():
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"plan_charging", "explain_plan"} <= names
    plan_tool = next(t for t in tools if t.name == "plan_charging")
    assert {"fleet", "fleet_yaml", "example", "include_schedule"} <= set(plan_tool.input_schema["properties"])


def test_plan_charging_through_server():
    result = asyncio.run(server.call_tool("plan_charging", {"example": "delivery-vans"}))
    data = json.loads(_text(result))
    assert data["all_ready"] is True
    assert data["savings"]["cost"] >= 0
    assert "schedule" not in data


def test_plan_charging_with_fleet_object_and_schedule():
    data = plan_charging(fleet=make_fleet([van(1), van(2)]), include_schedule=True)
    assert data["all_ready"] is True
    assert set(data["schedule"]["power_kw"]) == {"V1", "V2"}


def test_explain_plan_through_server():
    result = asyncio.run(server.call_tool("explain_plan", {"example": "late-shift-infeasible"}))
    text = _text(result)
    assert "Why some vehicles fall short" in text
    assert "SHORT-STOP" in text


def test_errors_come_back_as_text_not_crashes():
    data = plan_charging(fleet_yaml="name: x\n")
    assert "error" in data and data["problems"]
    assert "give exactly one" in plan_charging()["error"]
    assert explain_plan(example="nope").startswith("Berth could not plan this fleet")
