"""Degradation primitives: rate limiter, circuit breaker, fallback."""

from __future__ import annotations

from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(self, *, max_calls: int, window_seconds: float, clock: Callable[[], float]) -> None:
        if max_calls <= 0 or window_seconds <= 0:
            raise ValueError("max_calls 与 window_seconds 必须为正数")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.clock = clock
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self._calls and now - self._calls[0] >= self.window_seconds:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        recovery_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        if failure_threshold <= 0 or recovery_seconds <= 0:
            raise ValueError("failure_threshold 与 recovery_seconds 必须为正数")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open = False

    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._half_open:
            return "half_open"
        if self.clock() - self._opened_at >= self.recovery_seconds:
            return "half_open"
        return "open"

    def call(self, fn: Callable[[], str], fallback: Callable[[], str] | None = None) -> str:
        state = self.state()
        if state == "open":
            if fallback is None:
                raise RuntimeError("circuit open, no fallback")
            return fallback()
        try:
            result = fn()
        except Exception:
            self._failures += 1
            self._half_open = False
            if self._failures >= self.failure_threshold:
                self._opened_at = self.clock()
                self._half_open = False
            if fallback is not None:
                return fallback()
            raise
        if state == "half_open":
            self._opened_at = None
            self._half_open = False
            self._failures = 0
        return result
