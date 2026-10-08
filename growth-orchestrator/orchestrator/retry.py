"""Retry policy for external calls: backoff on 5xx/timeouts, Retry-After on 429, never retry 4xx, and
uncertain outcomes are returned to the caller to RECONCILE (never retried blindly)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .mocks import ApiResult, Timeout


class Clock:
    """Simulated time: sleeping advances the clock instantly, so tests are fast and deterministic."""

    def __init__(self, now):
        self.now = now
        self.slept = 0.0

    def sleep(self, seconds: float):
        from datetime import timedelta
        self.slept += seconds
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class Outcome:
    status: str                 # ok | uncertain | pending | rejected | exhausted
    result: ApiResult | None
    attempts: int
    waits: list = field(default_factory=list)
    reason: str = ""


class Retrier:
    def __init__(self, retry_policy: dict, clock: Clock):
        self.p = retry_policy
        self.clock = clock

    def _backoff(self, attempt: int, key: str) -> float:
        base = self.p["backoff_base_seconds"] * (self.p["backoff_factor"] ** (attempt - 1))
        jitter = (int(hashlib.sha256(f"{key}{attempt}".encode()).hexdigest(), 16) % 1000) / 1000 if self.p.get("jitter") else 0
        return round(base + jitter, 3)

    def call(self, fn, key: str, is_valid=None) -> Outcome:
        waits = []
        last_reason = ""
        for attempt in range(1, self.p["max_attempts"] + 1):
            try:
                res = fn()
            except Timeout:
                last_reason = "timeout"
                w = self._backoff(attempt, key)
                waits.append(("timeout", w)); self.clock.sleep(w)
                continue
            if res.status == 429:
                last_reason = "rate_limited"
                w = float(res.retry_after or 1)
                waits.append(("429", w)); self.clock.sleep(w)
                continue
            if res.status >= 500:
                last_reason = f"http_{res.status}"
                w = self._backoff(attempt, key)
                waits.append((str(res.status), w)); self.clock.sleep(w)
                continue
            if res.status == 202:
                return Outcome("pending", res, attempt, waits)
            if 400 <= res.status < 500:                      # never retry client errors
                return Outcome("rejected", res, attempt, waits, f"http_{res.status}")
            if res.body.get("status") == "unknown":         # may or may not have applied: caller must reconcile
                return Outcome("uncertain", res, attempt, waits)
            if is_valid is not None and not is_valid(res):   # 200 but unparseable / incomplete
                last_reason = "malformed_response"
                w = self._backoff(attempt, key)
                waits.append(("malformed", w)); self.clock.sleep(w)
                continue
            return Outcome("ok", res, attempt, waits)
        return Outcome("exhausted", None, self.p["max_attempts"], waits, last_reason)
