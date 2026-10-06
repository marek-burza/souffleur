from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, name: str) -> "Severity":
        return cls[name.strip().upper()]


@dataclass
class Finding:
    rule: str
    severity: Severity
    title: str
    path: str = ""
    line: int | None = None
    snippet: str = ""
    explain: str = ""
    category: str = ""
    decode_chain: list[str] = field(default_factory=list)
    more_lines: list[int] = field(default_factory=list)
    count: int = 1

    def location(self) -> str:
        if not self.path:
            return "(scan)"
        if self.line is None:
            return self.path
        return f"{self.path}:{self.line}"

    def to_json(self) -> dict:
        data = asdict(self)
        data["severity"] = self.severity.name
        return data


class FindingCollector:
    MAX_EXTRA_LINES = 5

    def __init__(self) -> None:
        self._by_key: dict[tuple, Finding] = {}

    def add(self, finding: Finding) -> None:
        key = (finding.rule, finding.path, tuple(finding.decode_chain))
        existing = self._by_key.get(key)
        if existing is None:
            self._by_key[key] = finding
            return
        self._merge_repeat(existing, finding)

    def _merge_repeat(self, existing: Finding, repeat: Finding) -> None:
        existing.count += 1
        existing.severity = max(existing.severity, repeat.severity)
        is_new_line = repeat.line is not None and repeat.line != existing.line
        if is_new_line and len(existing.more_lines) < self.MAX_EXTRA_LINES:
            existing.more_lines.append(repeat.line)

    def all_sorted(self) -> list[Finding]:
        return sorted(
            self._by_key.values(),
            key=lambda f: (-f.severity, f.rule, f.path, f.line or 0),
        )

    def highest_severity(self) -> Severity | None:
        return max((f.severity for f in self._by_key.values()), default=None)

    def count_by_severity(self) -> dict[Severity, int]:
        counts = {severity: 0 for severity in Severity}
        for finding in self._by_key.values():
            counts[finding.severity] += 1
        return counts
