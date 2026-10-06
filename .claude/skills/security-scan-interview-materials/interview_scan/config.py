import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .findings import Severity


@dataclass
class PatternRule:
    id: str
    severity: Severity
    title: str
    category: str
    regex: re.Pattern
    files: list[str]
    exclude: list[str]
    min_count: int
    explain: str


@dataclass
class ComboRule:
    id: str
    severity: Severity
    title: str
    category: str
    required: list[re.Pattern]
    files: list[str]


@dataclass
class HostClass:
    id: str
    severity: Severity
    title: str
    regex: re.Pattern


@dataclass
class SurfaceRule:
    path: str
    severity: Severity
    title: str
    exclude: str | None
    once: bool


@dataclass
class SettingRule:
    key: re.Pattern
    value: re.Pattern | None
    severity: Severity
    title: str


@dataclass
class IocStringRule:
    regex: re.Pattern
    severity: Severity
    label: str


@dataclass
class Iocs:
    updated: dt.date
    domains: set[str]
    domain_patterns: list[re.Pattern]
    urls: list[str]
    url_patterns: list[re.Pattern]
    ips: set[str]
    c2_ports: set[int]
    c2_paths: list[re.Pattern]
    npm_packages: set[str]
    pypi_packages: set[str]
    hashes: dict[str, str]
    wallet_extension_ids: dict[str, str]
    strings: list[IocStringRule]
    repo_names: set[str]
    fake_companies: list[str]
    host_artifacts: list[str]


@dataclass
class ScanConfig:
    raw: dict
    patterns: list[PatternRule]
    combos: list[ComboRule]
    host_classes: list[HostClass]
    surfaces: list[SurfaceRule]
    vscode_settings: list[SettingRule]
    agent_files: list[str]
    iocs: Iocs
    compiled: dict[str, re.Pattern] = field(default_factory=dict)

    def limit(self, name: str) -> int:
        return self.raw["limits"][name]

    def section(self, name: str) -> dict:
        return self.raw[name]

    def regex(self, dotted_name: str) -> re.Pattern:
        if dotted_name not in self.compiled:
            section, key = dotted_name.split(".")
            self.compiled[dotted_name] = re.compile(self.raw[section][key], re.MULTILINE)
        return self.compiled[dotted_name]


def compile_multiline(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.MULTILINE)


def load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def build_pattern_rule(entry: dict) -> PatternRule:
    return PatternRule(
        id=entry["id"],
        severity=Severity.parse(entry["severity"]),
        title=entry["title"],
        category=entry.get("category", ""),
        regex=compile_multiline(entry["regex"]),
        files=entry.get("files", []),
        exclude=entry.get("exclude", []),
        min_count=entry.get("min_count", 1),
        explain=entry.get("explain", ""),
    )


def build_combo_rule(entry: dict) -> ComboRule:
    return ComboRule(
        id=entry["id"],
        severity=Severity.parse(entry["severity"]),
        title=entry["title"],
        category=entry.get("category", ""),
        required=[compile_multiline(p) for p in entry["all"]],
        files=entry.get("files", []),
    )


def build_host_class(entry: dict) -> HostClass:
    return HostClass(entry["id"], Severity.parse(entry["severity"]), entry["title"], re.compile(entry["regex"]))


def build_surface_rule(entry: dict) -> SurfaceRule:
    return SurfaceRule(
        path=entry["path"],
        severity=Severity.parse(entry["severity"]),
        title=entry["title"],
        exclude=entry.get("exclude"),
        once=entry.get("once", False),
    )


def build_setting_rule(entry: dict) -> SettingRule:
    value = entry.get("value")
    return SettingRule(
        key=re.compile(entry["key"]),
        value=re.compile(value) if value else None,
        severity=Severity.parse(entry["severity"]),
        title=entry["title"],
    )


def build_iocs(data: dict) -> Iocs:
    packages = data.get("packages", {})
    return Iocs(
        updated=data["updated"],
        domains={d.lower() for d in data.get("domains", [])},
        domain_patterns=[re.compile(p, re.IGNORECASE) for p in data.get("domain_patterns", [])],
        urls=[u.lower() for u in data.get("urls", [])],
        url_patterns=[re.compile(p, re.IGNORECASE) for p in data.get("url_patterns", [])],
        ips=set(data.get("ips", [])),
        c2_ports=set(data.get("c2_ports", [])),
        c2_paths=[re.compile(p) for p in data.get("c2_paths", [])],
        npm_packages={p.lower() for p in packages.get("npm", [])},
        pypi_packages={normalize_pypi_name(p) for p in packages.get("pypi", [])},
        hashes={h.lower(): label for h, label in data.get("hashes", {}).items()},
        wallet_extension_ids=dict(data.get("wallet_extension_ids", {})),
        strings=[
            IocStringRule(compile_multiline(s["value"]), Severity.parse(s["severity"]), s["label"])
            for s in data.get("strings", [])
        ],
        repo_names={n.lower() for n in data.get("repo_names", [])},
        fake_companies=data.get("fake_companies", []),
        host_artifacts=data.get("host_artifacts", {}).get("paths", []),
    )


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_config(rules_path: Path, iocs_path: Path) -> ScanConfig:
    rules = load_toml(rules_path)
    return ScanConfig(
        raw=rules,
        patterns=[build_pattern_rule(e) for e in rules.get("pattern", [])],
        combos=[build_combo_rule(e) for e in rules.get("combo", [])],
        host_classes=[build_host_class(e) for e in rules.get("host_class", [])],
        surfaces=[build_surface_rule(e) for e in rules.get("surface", [])],
        vscode_settings=[build_setting_rule(e) for e in rules.get("vscode_setting", [])],
        agent_files=[e["path"] for e in rules.get("agent_file", [])],
        iocs=build_iocs(load_toml(iocs_path)),
    )
