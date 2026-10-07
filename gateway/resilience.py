"""Circuit breakers and latency tracking per routing target."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.state is CircuitState.OPEN:
            if self.clock() - self.opened_at >= self.reset_timeout:
                self.state = CircuitState.HALF_OPEN  # let one probe through
                return True
            return False
        return True

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state is CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = self.clock()


@dataclass
class CircuitRegistry:
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def get(self, key: str) -> CircuitBreaker:
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(
                self.failure_threshold, self.reset_timeout, self.clock
            )
        return self._breakers[key]

    def snapshot(self) -> dict[str, dict]:
        return {k: {"state": b.state.value, "failures": b.failures} for k, b in self._breakers.items()}


@dataclass
class LatencyTracker:
    """Exponentially weighted moving average of latency per target."""

    alpha: float = 0.3
    _ewma: dict[str, float] = field(default_factory=dict)

    def record(self, key: str, latency_ms: float) -> None:
        prev = self._ewma.get(key)
        self._ewma[key] = latency_ms if prev is None else self.alpha * latency_ms + (1 - self.alpha) * prev

    def get(self, key: str) -> float | None:
        return self._ewma.get(key)
