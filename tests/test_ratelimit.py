# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import time

import pytest

from ankido.config import RateLimit, RateLimits
from ankido.errors import RateLimited
from ankido.ratelimit import RateLimiter


def _limiter(burst: int = 2, per_minute: int = 600) -> RateLimiter:
    return RateLimiter(RateLimits(read=RateLimit(per_minute=per_minute, burst=burst)))


def test_burst_then_429_with_retry_after_then_refill() -> None:
    lim = _limiter(burst=2, per_minute=600)  # 10 tokens/s
    lim.check(token_id="t1", profile="alice", klass="read")
    lim.check(token_id="t1", profile="alice", klass="read")
    with pytest.raises(RateLimited) as ei:
        lim.check(token_id="t1", profile="alice", klass="read")
    err = ei.value
    assert err.status == 429 and err.retryable is True
    retry_after = err.details["retry_after_seconds"]
    assert 0 < retry_after <= 0.1
    time.sleep(0.15)  # one token refilled at 10/s
    lim.check(token_id="t1", profile="alice", klass="read")
    with pytest.raises(RateLimited):
        lim.check(token_id="t1", profile="alice", klass="read")


def test_profile_bucket_is_shared_across_tokens() -> None:
    lim = _limiter(burst=2, per_minute=60)
    lim.check(token_id="t1", profile="alice", klass="read")
    lim.check(token_id="t2", profile="alice", klass="read")
    with pytest.raises(RateLimited):
        lim.check(token_id="t3", profile="alice", klass="read")
    # another profile has its own bucket
    lim.check(token_id="t3", profile="bob", klass="read")


def test_classes_have_independent_buckets() -> None:
    lim = RateLimiter(
        RateLimits(
            read=RateLimit(per_minute=60, burst=1),
            write=RateLimit(per_minute=60, burst=1),
        )
    )
    lim.check(token_id="t1", profile="alice", klass="read")
    with pytest.raises(RateLimited, match="read operations"):
        lim.check(token_id="t1", profile="alice", klass="read")
    lim.check(token_id="t1", profile="alice", klass="write")
    with pytest.raises(RateLimited, match="write operations"):
        lim.check(token_id="t1", profile="alice", klass="write")
