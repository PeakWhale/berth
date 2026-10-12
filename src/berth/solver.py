"""A very small wrapper around the HiGHS solver (highspy).

We build the model once as plain lists (variables and rows) and then solve it
several times with different objectives and extra limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import highspy
import numpy as np

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

    def solve(
        self,
        cost: dict[int, float],
        *,
        bounds: dict[int, tuple[float, float]] | None = None,
        extra_rows: list[tuple[list[int], list[float], float, float]] | None = None,
        time_limit: float = 30.0,
        mip_gap: float = 1e-4,
        start: np.ndarray | None = None,
        relax: bool = False,
    ) -> SolveResult:
        """Solve. `relax=True` ignores integrality (the LP relaxation)."""
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

        lp = highspy.HighsLp()
        lp.num_col_ = n
        lp.num_row_ = len(row_lb)
        lp.col_cost_ = c
        lp.col_lower_ = lb
        lp.col_upper_ = ub
        lp.row_lower_ = np.array(row_lb, dtype=float)
        lp.row_upper_ = np.array(row_ub, dtype=float)
        lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
        lp.a_matrix_.start_ = np.array(starts, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(index, dtype=np.int32)
        lp.a_matrix_.value_ = np.array(value, dtype=float)
        is_mip = any(self.integer) and not relax
        if is_mip:
            lp.integrality_ = [
                highspy.HighsVarType.kInteger if flag else highspy.HighsVarType.kContinuous
                for flag in self.integer
            ]

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("time_limit", float(max(time_limit, 0.5)))
        h.setOptionValue("mip_rel_gap", float(mip_gap))
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
        )
