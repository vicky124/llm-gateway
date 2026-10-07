"""Per-tenant token-bucket limits on requests/minute and tokens/minute."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    clock: Callable[[], float] = time.monotonic
    tokens: float = -1.0
    updated: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            self.tokens = self.capacity
        self.updated = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_second)
        self.updated = now

    def try_consume(self, amount: float) -> float:
        """Consume `amount`; return 0 on success, else seconds until it would fit."""
        self._refill()
        if amount <= self.tokens:
            self.tokens -= amount
            return 0.0
        return (amount - self.tokens) / self.refill_per_second

    def adjust(self, delta: float) -> None:
        """Credit (+) or debit (-) the bucket; debt is allowed and repaid by refill."""
        self._refill()
        self.tokens = min(self.capacity, self.tokens + delta)


class RateLimited(Exception):
    def __init__(self, reason: str, retry_after: float):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._buckets: dict[str, tuple[TokenBucket, TokenBucket]] = {}

    def _get(self, tenant: str, rpm: int, tpm: int) -> tuple[TokenBucket, TokenBucket]:
        if tenant not in self._buckets:
            self._buckets[tenant] = (
                TokenBucket(rpm, rpm / 60, self.clock),
                TokenBucket(tpm, tpm / 60, self.clock),
            )
        return self._buckets[tenant]

    def acquire(self, tenant: str, rpm: int, tpm: int, estimated_tokens: int) -> None:
        requests, tokens = self._get(tenant, rpm, tpm)
        wait = requests.try_consume(1)
        if wait:
            raise RateLimited("requests per minute exceeded", wait)
        wait = tokens.try_consume(min(estimated_tokens, tpm))
        if wait:
            requests.adjust(+1)  # refund: the request never ran
            raise RateLimited("tokens per minute exceeded", wait)

    def reconcile(self, tenant: str, estimated_tokens: int, actual_tokens: int, tpm: int) -> None:
        if tenant in self._buckets:
            self._buckets[tenant][1].adjust(min(estimated_tokens, tpm) - actual_tokens)
