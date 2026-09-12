"""Rate limiting — in-process token bucket.

Single-server MVP (spec §16): no Redis. Buckets keyed by (bucket_name, key).
Each key has `capacity` tokens, refilled at `refill_per_second`. On empty, the
request is rejected with HTTP 429 (clear JSON response, never crashes the app).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass
class BucketRule:
    capacity: int
    refill_per_second: float


class TokenBucketLimiter:
    def __init__(self) -> None:
        self._rules: dict[str, BucketRule] = {}
        self._buckets: dict[Tuple[str, str], Tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    def add_rule(self, name: str, capacity: int, refill_per_second: float) -> None:
        self._rules[name] = BucketRule(capacity=capacity, refill_per_second=refill_per_second)

    def check(self, rule_name: str, key: str) -> bool:
        """Try to consume one token. Returns False if rate limited."""
        rule = self._rules.get(rule_name)
        if rule is None:
            return True  # unknown rule -> allow (fail-open for unconfigured buckets)
        now = time.monotonic()
        with self._lock:
            bucket_key = (rule_name, key)
            state = self._buckets.get(bucket_key)
            if state is None:
                self._buckets[bucket_key] = (rule.capacity - 1.0, now)
                return True
            tokens, last = state
            tokens = min(rule.capacity, tokens + (now - last) * rule.refill_per_second)
            if tokens >= 1.0:
                self._buckets[bucket_key] = (tokens - 1.0, now)
                return True
            self._buckets[bucket_key] = (tokens, now)
            return False

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def setup_default_rules(limiter: TokenBucketLimiter) -> None:
    limiter.add_rule("login_ip", capacity=10, refill_per_second=1 / 6)
    limiter.add_rule("login_token", capacity=5, refill_per_second=1 / 12)
    limiter.add_rule("chat_ip", capacity=30, refill_per_second=1 / 2)
    limiter.add_rule("chat_token", capacity=20, refill_per_second=1 / 3)
    limiter.add_rule("messages_ip", capacity=60, refill_per_second=1.0)
    limiter.add_rule("sessions_ip", capacity=30, refill_per_second=0.5)
