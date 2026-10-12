"""A very small wrapper around the HiGHS solver (highspy).

We build the model once as plain lists (variables and rows) and then solve it
several times with different objectives and extra limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import highspy
import numpy as np

from berth.errors import BerthInputError

INF = highspy.kHighsInf


@dataclass
class SolveResult:
    has_solution: bool
    optimal: bool
    status: str
    x: np.ndarray
    objective: float
    gap: float | None
    seconds: float
    nodes: int = 0
    from_start: bool = False  # the solver did not improve on the starting plan, which is returned


class LinearModel:
    """Variables with bounds plus rows `lb <= sum(coef * var) <= ub`."""

    def __init__(self) -> None:
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.integer: list[bool] = []
        self.row_lb: list[float] = []
        self.row_ub: list[float] = []
        self.row_start: list[int] = [0]
        self.row_index: list[int] = []
        self.row_value: list[float] = []

    @property
    def n_vars(self) -> int:
        return len(self.lb)

    @property
    def n_integer(self) -> int:
        return sum(self.integer)

    def var(self, lb: float = 0.0, ub: float = INF, integer: bool = False) -> int:
        self.lb.append(float(lb))
        self.ub.append(float(ub))
        self.integer.append(integer)
        return len(self.lb) - 1

    def row(self, index: list[int], value: list[float], lb: float = -INF, ub: float = INF) -> None:
        self.row_index.extend(index)
        self.row_value.extend(value)
        self.row_start.append(len(self.row_index))
        self.row_lb.append(float(lb))
        self.row_ub.append(float(ub))

    def feasible(
        self,
        x: np.ndarray,
        bounds: dict[int, tuple[float, float]] | None = None,
        extra_rows: list[tuple[list[int], list[float], float, float]] | None = None,
        tol: float = 1e-6,
    ) -> bool:
        """True if x keeps every bound, integer and row (within tol)."""
        x = np.asarray(x, dtype=float)
        if len(x) != self.n_vars or not np.isfinite(x).all():
            return False
        lb = np.array(self.lb, dtype=float)
        ub = np.array(self.ub, dtype=float)
        for j, (lo, hi) in (bounds or {}).items():
            lb[j], ub[j] = lo, hi
        if (x < lb - tol).any() or (x > ub + tol).any():
            return False
        ints = np.array(self.integer, dtype=bool)
        if ints.any() and (np.abs(x[ints] - np.round(x[ints])) > tol).any():
            return False
        counts = np.diff(np.array(self.row_start))
        rows = np.repeat(np.arange(len(counts)), counts)
        activity = np.bincount(rows, weights=np.array(self.row_value) * x[np.array(self.row_index, dtype=int)],
                               minlength=len(counts))
        lo = np.array(self.row_lb, dtype=float)
        hi = np.array(self.row_ub, dtype=float)
        slack = tol * np.maximum(1.0, np.abs(activity))
        if (activity < lo - slack).any() or (activity > hi + slack).any():
            return False
        for idx, val, lo_, hi_ in extra_rows or []:
            act = float(np.dot(val, x[idx])) if idx else 0.0
            if act < lo_ - tol * max(1.0, abs(act)) or act > hi_ + tol * max(1.0, abs(act)):
                return False
        return True

    def solve(
        self,
        cost: dict[int, float],
        *,
        bounds: dict[int, tuple[float, float]] | None = None,
        extra_rows: list[tuple[list[int], list[float], float, float]] | None = None,
        time_limit: float = 30.0,
        mip_gap: float = 1e-4,
        abs_gap: float | None = None,
        max_nodes: int | None = None,
        start: np.ndarray | None = None,
        relax: bool = False,
    ) -> SolveResult:
        """Solve. `relax=True` ignores integrality (the LP relaxation).
        `max_nodes` stops a mixed-integer search after that many
        branch-and-bound nodes (unlike a time limit, the result does not
        depend on how fast the computer is)."""
        started = time.perf_counter()
        n = self.n_vars
        lb = np.array(self.lb, dtype=float)
        ub = np.array(self.ub, dtype=float)
        for j, (lo, hi) in (bounds or {}).items():
            lb[j], ub[j] = lo, hi
        c = np.zeros(n)
        for j, v in cost.items():
            c[j] += v

        row_lb = list(self.row_lb)
        row_ub = list(self.row_ub)
        starts = list(self.row_start)
        index = list(self.row_index)
        value = list(self.row_value)
        for idx, val, lo, hi in extra_rows or []:
            index.extend(idx)
            value.extend(val)
            starts.append(len(index))
            row_lb.append(lo)
            row_ub.append(hi)

        row_lb_a = np.array(row_lb, dtype=float)
        row_ub_a = np.array(row_ub, dtype=float)
        value_a = np.array(value, dtype=float)
        # Bad numbers (NaN, or infinite costs and coefficients) can make
        # HiGHS ignore its time limit. Input checks refuse them; this is the
        # last line of defence.
        if (
            np.isnan(lb).any() or np.isnan(ub).any() or not np.isfinite(c).all()
            or np.isnan(row_lb_a).any() or np.isnan(row_ub_a).any() or not np.isfinite(value_a).all()
        ):
            raise BerthInputError("a price or limit is not a number (NaN or infinity); check the tariff and the site")

        lp = highspy.HighsLp()
        lp.num_col_ = n
        lp.num_row_ = len(row_lb)
        lp.col_cost_ = c
        lp.col_lower_ = lb
        lp.col_upper_ = ub
        lp.row_lower_ = row_lb_a
        lp.row_upper_ = row_ub_a
        lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
        lp.a_matrix_.start_ = np.array(starts, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(index, dtype=np.int32)
        lp.a_matrix_.value_ = value_a
        is_mip = any(self.integer) and not relax
        if is_mip:
            lp.integrality_ = [
                highspy.HighsVarType.kInteger if flag else highspy.HighsVarType.kContinuous
                for flag in self.integer
            ]

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("time_limit", float(max(time_limit, 0.01)))
        h.setOptionValue("mip_rel_gap", float(mip_gap))
        if abs_gap is not None:
            h.setOptionValue("mip_abs_gap", float(abs_gap))
        if max_nodes is not None:
            h.setOptionValue("mip_max_nodes", int(max_nodes))
        h.setOptionValue("random_seed", 0)
        h.passModel(lp)
        if start is not None and is_mip:
            sol = highspy.HighsSolution()
            sol.col_value = [float(v) for v in start]
            sol.value_valid = True
            h.setSolution(sol)
        h.run()

        status = h.getModelStatus()
        info = h.getInfo()
        has_solution = info.primal_solution_status == 2  # feasible point available
        optimal = status == highspy.HighsModelStatus.kOptimal
        x = np.array(h.getSolution().col_value) if has_solution else np.zeros(n)
        gap = float(info.mip_gap) if is_mip and has_solution else None
        return SolveResult(
            has_solution=has_solution,
            optimal=optimal,
            status=h.modelStatusToString(status),
            x=x,
            objective=float(info.objective_function_value) if has_solution else float("nan"),
            gap=gap,
            seconds=time.perf_counter() - started,
            nodes=int(info.mip_node_count) if is_mip else 0,
        )
