"""Clear, human-readable input errors."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError


class BerthInputError(ValueError):
    """The input cannot be used. `problems` lists each issue in plain words."""

    def __init__(self, problems: list[str] | str, source: str | None = None):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = problems
        self.source = source
        head = f"{source}: " if source else ""
        if len(problems) == 1:
            text = head + problems[0]
        else:
            text = head + "found {} problems:\n".format(len(problems)) + "\n".join(f"  - {p}" for p in problems)
        super().__init__(text)


class BerthSolverError(RuntimeError):
    """The solver could not produce a plan (for example it ran out of time)."""


_MESSAGES = {
    "missing": "is required but missing",
    "extra_forbidden": "is not a known field (check the spelling)",
}


def _vehicle_label(data: Any, index: int) -> str:
    try:
        vid = data["vehicles"][index].get("id")
    except (KeyError, IndexError, TypeError, AttributeError):
        vid = None
    return f"vehicle {index + 1} ({vid})" if vid not in (None, "") else f"vehicle {index + 1}"


def format_validation_error(exc: ValidationError, data: Any = None) -> list[str]:
    """Turn a pydantic error into short lines like
    "vehicle 3 (VAN-03): battery_kwh: Input should be greater than 0"."""
    return format_error_list(exc.errors(), data)


def format_error_list(
    errors: list[dict], data: Any = None, drop_prefix: tuple = (), prefix: tuple = ()
) -> list[str]:
    lines: list[str] = []
    for err in errors:
        loc = list(err.get("loc", ()))
        if drop_prefix and tuple(loc[: len(drop_prefix)]) == tuple(drop_prefix):
            loc = loc[len(drop_prefix):]
        loc = list(prefix) + loc
        parts: list[str] = []
        i = 0
        while i < len(loc):
            key = loc[i]
            nxt = loc[i + 1] if i + 1 < len(loc) else None
            if key == "vehicles" and isinstance(nxt, int):
                parts.append(_vehicle_label(data, nxt))
                i += 2
                continue
            if key == "stops" and isinstance(nxt, int):
                parts.append(f"stop {nxt + 1}")
                i += 2
                continue
            if key == "chargers" and isinstance(nxt, int):
                parts.append(f"site.chargers group {nxt + 1}")
                i += 2
                continue
            if isinstance(key, int):
                parts.append(f"item {key + 1}")
            elif key == "site" and nxt == "chargers" and i + 2 < len(loc) and isinstance(loc[i + 2], int):
                pass  # shown as "site.chargers group N"
            else:
                parts.append(str(key))
            i += 1
        where = ": ".join(_join_dotted(parts))
        msg = _MESSAGES.get(err.get("type", ""), err.get("msg", "invalid value"))
        msg = msg.removeprefix("Value error, ")
        line = f"{where}: {msg}" if where else msg
        if line not in lines:
            lines.append(line)
    return lines


def _join_dotted(parts: list[str]) -> list[str]:
    """Join plain field names with dots but keep labelled parts separate."""
    out: list[str] = []
    buf: list[str] = []
    for p in parts:
        if " " in p:
            if buf:
                out.append(".".join(buf))
                buf = []
            out.append(p)
        else:
            buf.append(p)
    if buf:
        out.append(".".join(buf))
    return out
