# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from ankido.metrics import Metrics


def test_render_counters_and_histograms() -> None:
    m = Metrics()
    assert m.render() == "\n"
    m.inc("http_requests_total", route="/healthz", status="200")
    m.inc("http_requests_total", route="/healthz", status="200")
    m.inc("sync_total", 2.5, profile="alice", outcome="merged")
    m.inc("plain")
    m.observe("http_request_seconds", 0.03, route="/healthz")
    m.observe("http_request_seconds", 7.0, route="/healthz")
    m.observe("unlabelled_seconds", 0.001)
    text = m.render()
    lines = text.splitlines()
    assert text.endswith("\n")
    assert 'ankido_http_requests_total{route="/healthz",status="200"} 2' in lines
    assert 'ankido_sync_total{outcome="merged",profile="alice"} 2.5' in lines
    assert "ankido_plain 1" in lines
    # histogram: cumulative buckets, +Inf, sum, count
    assert 'ankido_http_request_seconds_bucket{route="/healthz",le="0.025"} 0' in lines
    assert 'ankido_http_request_seconds_bucket{route="/healthz",le="0.05"} 1' in lines
    assert 'ankido_http_request_seconds_bucket{route="/healthz",le="10.0"} 2' in lines
    assert 'ankido_http_request_seconds_bucket{route="/healthz",le="+Inf"} 2' in lines
    assert 'ankido_http_request_seconds_sum{route="/healthz"} 7.03' in lines
    assert 'ankido_http_request_seconds_count{route="/healthz"} 2' in lines
    assert 'ankido_unlabelled_seconds_bucket{le="0.005"} 1' in lines
    assert 'ankido_unlabelled_seconds_bucket{le="+Inf"} 1' in lines
    assert "ankido_unlabelled_seconds_count 1" in lines
