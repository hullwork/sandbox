"""Minimal implementation of Prometheus text format.

Responsibility: Only counting and rendering; not deciding what to collect, that is in core.py.
Constraints: Do not introduce third-party dependencies. Control Plane has only made one exception so far for psycopg (it needs to carry relational data).
     The indicator is not worth breaking again - the text format itself is only a few dozen lines long.

Thread safety: Every time ThreadingHTTPServer requests a thread, all counters are changed in the lock."""
from __future__ import annotations

import threading
from typing import Callable, Iterable


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + inner + "}"


class _Metric:
    def __init__(self, name: str, help_text: str, kind: str) -> None:
        self.name = name
        self.help_text = help_text
        self.kind = kind
        self._lock = threading.Lock()

    def header(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.kind}",
        ]


class Counter(_Metric):
    """Only increase, not decrease. Tag values ​​must come from a finite enumeration - high cardinality tags will bog down the crawler."""

    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name, help_text, "counter")
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def ensure(self, **labels: str) -> None:
        """Register a label combination at zero.

        🔴 A counter only appears once something increments it, so on the
        scraper side "this has never failed" and "this build does not have that
        metric at all" are the same observation: both are an absent series.
        An alert written as `rate(..._failures_total[10m]) > 0` cannot tell
        them apart, and `absent(...)` misfires on the instance that genuinely
        never failed. Registering the known combinations at startup makes zero
        a real reading rather than a missing one.
        """
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values.setdefault(key, 0.0)

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        if not items:
            return []
        lines = self.header()
        for key, value in items:
            lines.append(f"{self.name}{_format_labels(key)} {value:g}")
        return lines


class Gauge(_Metric):
    """The instantaneous value calculated at the time of collection.

    🔴 Constraint: When the value function throws an exception, the entire indicator will not be output and will never fall back to 0.
       "Can't count" and "really 0" must be distinguished - the former is reported as the latter, and the consumer (alarm,
       Capacity dashboard) will read "data source down" as "system is idle", which is the worst kind of misleading."""

    def __init__(
        self, name: str, help_text: str, source: Callable[[], float]
    ) -> None:
        super().__init__(name, help_text, "gauge")
        self._source = source

    def render(self) -> list[str]:
        try:
            value = self._source()
        except Exception:
            return []
        return self.header() + [f"{self.name} {value:g}"]


class Histogram(_Metric):
    """Cumulative bucket. The bucket boundary is given according to the actual magnitude of the measurement, do not copy the default value."""

    def __init__(
        self, name: str, help_text: str, buckets: Iterable[float]
    ) -> None:
        super().__init__(name, help_text, "histogram")
        self._bounds = tuple(sorted(buckets))
        self._values: dict[
            tuple[tuple[str, str], ...], tuple[list[int], float, int]
        ] = {}

    def observe(self, value: float, **labels: str) -> None:
        """Remember a sample.

        Constraint: Buckets are cumulative - a sample must be included in all le >= its buckets, not just
             The narrowest one. Prometheus's histogram has such semantics that it no longer accumulates twice when rendering."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            counts, summed, total = self._values.get(
                key, ([0] * len(self._bounds), 0.0, 0)
            )
            summed += value
            total += 1
            for index, bound in enumerate(self._bounds):
                if value <= bound:
                    counts[index] += 1
            self._values[key] = (counts, summed, total)

    def render(self) -> list[str]:
        with self._lock:
            items = [
                (key, list(counts), summed, total)
                for key, (counts, summed, total) in sorted(self._values.items())
            ]
        if not items:
            return []
        lines = self.header()
        for key, counts, summed, total in items:
            base = dict(key)
            for bound, count in zip(self._bounds, counts):
                labels = tuple(sorted({**base, "le": f"{bound:g}"}.items()))
                lines.append(
                    f"{self.name}_bucket{_format_labels(labels)} {count}"
                )
            labels = tuple(sorted({**base, "le": "+Inf"}.items()))
            lines.append(f"{self.name}_bucket{_format_labels(labels)} {total}")
            lines.append(f"{self.name}_sum{_format_labels(key)} {summed:g}")
            lines.append(f"{self.name}_count{_format_labels(key)} {total}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._metrics: list[_Metric] = []

    def register(self, metric: _Metric):
        self._metrics.append(metric)
        return metric

    def render(self) -> bytes:
        lines: list[str] = []
        for metric in self._metrics:
            lines.extend(metric.render())
        return ("\n".join(lines) + "\n").encode("utf-8")


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
