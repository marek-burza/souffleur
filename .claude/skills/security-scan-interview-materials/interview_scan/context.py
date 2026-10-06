from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import ScanConfig
from .findings import Finding, FindingCollector, Severity
from .textutil import path_matches_any


@dataclass
class FileRecord:
    rel: str
    disk: Path
    size: int
    sha256: str
    head: bytes
    text: str | None
    truncated: bool
    magic: str | None

    @property
    def name(self) -> str:
        return PurePosixPath(self.rel).name

    @property
    def ext(self) -> str:
        suffix = PurePosixPath(self.rel).suffix
        return suffix[1:].lower() if suffix else ""

    @property
    def is_text(self) -> bool:
        return self.text is not None

    def is_inside(self, directory_name: str) -> bool:
        return directory_name in PurePosixPath(self.rel).parts[:-1]


@dataclass
class Dependency:
    ecosystem: str
    name: str
    spec: str
    declared_in: str


@dataclass
class ScanOptions:
    target: str
    work_dir: Path
    registry_lookup: bool = False
    archive_password: str | None = None


@dataclass
class ScanContext:
    config: ScanConfig
    options: ScanOptions
    root: Path | None = None
    files: list[FileRecord] = field(default_factory=list)
    findings: FindingCollector = field(default_factory=FindingCollector)
    dependencies: list[Dependency] = field(default_factory=list)
    hosts_seen: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    summary: dict = field(default_factory=dict)

    def report(
        self,
        rule: str,
        severity: Severity,
        title: str,
        path: str = "",
        line: int | None = None,
        snippet: str = "",
        explain: str = "",
        category: str = "",
        decode_chain: list[str] | None = None,
    ) -> None:
        self.findings.add(Finding(
            rule=rule,
            severity=severity,
            title=title,
            path=path,
            line=line,
            snippet=snippet,
            explain=explain,
            category=category,
            decode_chain=list(decode_chain or []),
        ))

    def regex(self, dotted_name: str):
        return self.config.regex(dotted_name)

    def text_files(self):
        return (record for record in self.files if record.is_text)

    def files_matching(self, patterns: list[str]):
        return (record for record in self.files if path_matches_any(record.rel, patterns))

    def find_file(self, rel: str) -> FileRecord | None:
        return next((record for record in self.files if record.rel == rel), None)

    def is_lockfile(self, rel: str) -> bool:
        return path_matches_any(rel, self.config.section("files")["lockfiles"])

    def is_doc(self, rel: str) -> bool:
        return path_matches_any(rel, self.config.section("files")["docs"])

    def is_minified(self, rel: str) -> bool:
        return path_matches_any(rel, self.config.section("files")["minified"])
