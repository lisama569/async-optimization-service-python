"""求解相关的业务异常（骨架）。"""

from __future__ import annotations

from .models import FailureReason


class SolveError(Exception):
    """求解业务异常基类。"""

    reason: FailureReason = FailureReason.NO_SOLUTION

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class InvalidInputError(SolveError):
    reason = FailureReason.INVALID_INPUT


class InfeasibleError(SolveError):
    reason = FailureReason.INFEASIBLE


class NoSolutionError(SolveError):
    reason = FailureReason.NO_SOLUTION
