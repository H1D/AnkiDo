# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tiny in-process counters/histograms, exported in Prometheus text format (admin scope only)."""

from __future__ import annotations

import threading
from collections import defaultdict

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = defaultdict(float)
        self._hist: dict[tuple[str, str], list[float]] = {}
        self._hist_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._hist_count: dict[tuple[str, str], int] = defaultdict(int)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        key = (name, _labels(labels))
        with self._lock:
            buckets = self._hist.setdefault(key, [0.0] * len(_BUCKETS))
            for i, b in enumerate(_BUCKETS):
                if seconds <= b:
                    buckets[i] += 1
            self._hist_sum[key] += seconds
            self._hist_count[key] += 1

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), v in sorted(self._counters.items()):
                lines.append(f"ankido_{name}{labels} {v:g}")
            for (name, labels), buckets in sorted(self._hist.items()):
                base = labels[1:-1] if labels else ""
                for i, b in enumerate(_BUCKETS):
                    lab = f'{{{base + "," if base else ""}le="{b}"}}'
                    lines.append(f"ankido_{name}_bucket{lab} {buckets[i]:g}")
                lab = f'{{{base + "," if base else ""}le="+Inf"}}'
                lines.append(f"ankido_{name}_bucket{lab} {self._hist_count[(name, labels)]}")
                lines.append(f"ankido_{name}_sum{labels} {self._hist_sum[(name, labels)]:g}")
                lines.append(f"ankido_{name}_count{labels} {self._hist_count[(name, labels)]}")
        return "\n".join(lines) + "\n"
