"""Small Prometheus registry. Labels contain identifiers, never message text."""

from collections import defaultdict
import json
import math


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[tuple, float] = defaultdict(float)
        self.observations: dict[tuple, list[float]] = defaultdict(list)

    def increment(self, name: str, value: float = 1, **labels: str) -> None:
        self.counters[(name, tuple(sorted(labels.items())))] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations[(name, tuple(sorted(labels.items())))].append(value)

    @staticmethod
    def _labels(labels: tuple) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f"{key}={json.dumps(str(value))}" for key, value in labels) + "}"

    def render(self) -> str:
        lines: list[str] = []
        declared: set[str] = set()
        for (name, labels), value in sorted(self.counters.items()):
            if name not in declared:
                lines.append(f"# TYPE {name} counter")
                declared.add(name)
            lines.append(f"{name}{self._labels(labels)} {value:g}")
        for (name, labels), values in sorted(self.observations.items()):
            if name not in declared:
                lines.append(f"# TYPE {name} summary")
                declared.add(name)
            for fraction in (0.5, 0.95, 0.99):
                tags = labels + (("quantile", str(fraction)),)
                lines.append(f"{name}{self._labels(tags)} {percentile(values, fraction):g}")
            lines.append(f"{name}_count{self._labels(labels)} {len(values)}")
            lines.append(f"{name}_sum{self._labels(labels)} {sum(values):g}")
        return "\n".join(lines) + "\n"
