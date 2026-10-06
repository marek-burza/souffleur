import re
import tomllib
from pathlib import PurePosixPath

from .config import normalize_pypi_name
from .context import Dependency, FileRecord, ScanContext
from .findings import Severity
from .textutil import first_line_containing, safe_for_display
from .typosquat import find_impersonated_package

REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
INDEX_OPTION = re.compile(r"^\s*(--index-url|-i|--extra-index-url|--trusted-host|-f|--find-links)\b")
DIRECT_REFERENCE = re.compile(r"(git\+|hg\+|svn\+|bzr\+|https?://|file:|@\s*(https?|file|git)\b)")
LOCAL_INSTALL = re.compile(r"^\s*(-e\s+|--editable\s+)?(\.|/|\./|\.\./)")
REQUIREMENTS_FILE = re.compile(r"^(requirements.*|constraints.*|.*requirements)\.(txt|in)$")


def analyze_python_projects(ctx: ScanContext) -> None:
    for record in ctx.text_files():
        if REQUIREMENTS_FILE.match(record.name):
            analyze_requirements_file(ctx, record)
        elif record.name == "pyproject.toml":
            analyze_pyproject(ctx, record)
        elif record.name == "Pipfile":
            analyze_pipfile(ctx, record)
    report_vendored_popular_libraries(ctx)


def analyze_requirements_file(ctx: ScanContext, record: FileRecord) -> None:
    for number, raw_line in enumerate(record.text.splitlines(), start=1):
        line = raw_line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        check_requirement_line(ctx, record, number, line)


def check_requirement_line(ctx: ScanContext, record: FileRecord, number: int, line: str) -> None:
    if INDEX_OPTION.match(line):
        report_python_dependency(ctx, record, number, "DEP-INDEX", Severity.HIGH, "Requirements file changes the package index (dependency confusion risk)", line)
        return
    if LOCAL_INSTALL.match(line):
        report_python_dependency(ctx, record, number, "DEP-SOURCE", Severity.HIGH, "Requirements file installs a local path (runs its setup code)", line)
        return
    if line.startswith("-"):
        return
    if DIRECT_REFERENCE.search(line):
        report_python_dependency(ctx, record, number, "DEP-SOURCE", Severity.HIGH, "Requirement installs from a URL or VCS instead of PyPI", line)
    name_match = REQUIREMENT_NAME.match(line)
    if name_match:
        register_and_check_pypi_name(ctx, record, number, name_match.group(1), line)


def register_and_check_pypi_name(ctx: ScanContext, record: FileRecord, line: int | None, name: str, spec: str) -> None:
    ctx.dependencies.append(Dependency("pypi", name, spec, record.rel))
    normalized = normalize_pypi_name(name)
    if normalized in ctx.config.iocs.pypi_packages:
        report_python_dependency(ctx, record, line, "DEP-IOC", Severity.CRITICAL, f"Known malicious PyPI package '{name}'", spec)
    popular = {normalize_pypi_name(p) for p in ctx.config.section("popular")["pypi"]}
    impersonated = find_impersonated_package(normalized, popular, ctx.config.section("npm")["typosquat_suffixes"])
    if impersonated:
        report_python_dependency(ctx, record, line, "DEP-TYPOSQUAT", Severity.MEDIUM, f"Package name '{name}' resembles popular package '{impersonated}'", spec)


def report_python_dependency(ctx: ScanContext, record: FileRecord, line, rule: str, severity: Severity, title: str, snippet: str) -> None:
    ctx.report(rule, severity, title, path=record.rel, line=line, snippet=safe_for_display(snippet), category="dependency")


def load_toml_or_report(ctx: ScanContext, record: FileRecord) -> dict | None:
    try:
        return tomllib.loads(record.text)
    except tomllib.TOMLDecodeError as error:
        ctx.report("PY-UNPARSEABLE", Severity.MEDIUM, f"{record.name} could not be parsed: {error}", path=record.rel, category="coverage")
        return None


def analyze_pyproject(ctx: ScanContext, record: FileRecord) -> None:
    data = load_toml_or_report(ctx, record)
    if data is None:
        return
    check_build_backend(ctx, record, data.get("build-system", {}))
    check_build_hooks(ctx, record, data.get("tool", {}))
    check_custom_python_indexes(ctx, record, data.get("tool", {}))
    for name, spec in iterate_pyproject_dependencies(data):
        register_and_check_pypi_name(ctx, record, first_line_containing(record.text, name), name, spec)


def check_build_backend(ctx: ScanContext, record: FileRecord, build_system: dict) -> None:
    backend = build_system.get("build-backend")
    standard = ctx.config.section("python")["standard_build_backends"]
    line = first_line_containing(record.text, "build-backend")
    if backend and backend not in standard:
        report_python_dependency(ctx, record, line, "PY-BUILD-BACKEND", Severity.HIGH, "Non-standard build backend runs custom code on pip install", backend)
    if build_system.get("backend-path"):
        report_python_dependency(ctx, record, line, "PY-BUILD-BACKEND", Severity.HIGH, "In-tree build backend (backend-path) runs repo code on pip install", str(build_system["backend-path"]))


def check_build_hooks(ctx: ScanContext, record: FileRecord, tool: dict) -> None:
    hatch_hooks = tool.get("hatch", {}).get("build", {})
    if "hooks" in hatch_hooks or any("hooks" in t for t in hatch_hooks.get("targets", {}).values() if isinstance(t, dict)):
        report_python_dependency(ctx, record, first_line_containing(record.text, "hooks"), "PY-BUILD-HOOK", Severity.HIGH, "Hatch build hook runs custom code at build/install time", "tool.hatch.build hooks")
    if tool.get("setuptools", {}).get("cmdclass"):
        report_python_dependency(ctx, record, first_line_containing(record.text, "cmdclass"), "PY-BUILD-HOOK", Severity.HIGH, "setuptools cmdclass overrides build commands", str(tool["setuptools"]["cmdclass"]))
    if tool.get("poetry", {}).get("build"):
        report_python_dependency(ctx, record, first_line_containing(record.text, "[tool.poetry.build]"), "PY-BUILD-HOOK", Severity.HIGH, "Poetry build script runs at install time", str(tool["poetry"]["build"]))


def check_custom_python_indexes(ctx: ScanContext, record: FileRecord, tool: dict) -> None:
    sources = [*tool.get("poetry", {}).get("source", []), *tool.get("uv", {}).get("index", []), *tool.get("pdm", {}).get("source", [])]
    for source in sources:
        if isinstance(source, dict) and not is_pypi_url(ctx, str(source.get("url", ""))):
            report_python_dependency(ctx, record, first_line_containing(record.text, str(source.get("url", ""))), "DEP-INDEX", Severity.HIGH, "Custom Python package index configured", str(source))
    for name, source in tool.get("uv", {}).get("sources", {}).items():
        if isinstance(source, dict) and any(k in source for k in ("git", "url", "path")):
            report_python_dependency(ctx, record, first_line_containing(record.text, name), "DEP-SOURCE", Severity.HIGH, f"Dependency '{name}' comes from git, a URL, or a local path", str(source))


def is_pypi_url(ctx: ScanContext, url: str) -> bool:
    return any(host in url for host in ctx.config.section("registries")["pypi_hosts"])


def iterate_pyproject_dependencies(data: dict):
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs.extend(group)
    for group in data.get("dependency-groups", {}).values():
        specs.extend(s for s in group if isinstance(s, str))
    for spec in specs:
        match = REQUIREMENT_NAME.match(str(spec))
        if match:
            yield match.group(1), str(spec)
    poetry = data.get("tool", {}).get("poetry", {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in poetry.get(section, {}).items():
            if name.lower() != "python":
                yield name, str(spec)


def analyze_pipfile(ctx: ScanContext, record: FileRecord) -> None:
    data = load_toml_or_report(ctx, record)
    if data is None:
        return
    for source in data.get("source", []):
        if not is_pypi_url(ctx, str(source.get("url", ""))):
            report_python_dependency(ctx, record, first_line_containing(record.text, "[[source]]"), "DEP-INDEX", Severity.HIGH, "Pipfile uses a custom package index", str(source))
    for section in ("packages", "dev-packages"):
        for name, spec in data.get(section, {}).items():
            register_and_check_pypi_name(ctx, record, first_line_containing(record.text, name), name, f"{name} {spec}")


def report_vendored_popular_libraries(ctx: ScanContext) -> None:
    watchlist = set(ctx.config.section("vendored")["watchlist"])
    reported = set()
    for record in ctx.files:
        parts = PurePosixPath(record.rel).parts
        for depth, directory in enumerate(parts[:-1]):
            vendored_root = "/".join(parts[: depth + 1])
            if directory in watchlist and is_vendored_location(parts, depth) and is_package_directory(ctx, vendored_root):
                if vendored_root not in reported:
                    reported.add(vendored_root)
                    report_vendored_library(ctx, vendored_root, directory)


def is_package_directory(ctx: ScanContext, directory: str) -> bool:
    markers = ("__init__.py", "package.json", "setup.py", "pyproject.toml")
    return any(ctx.find_file(f"{directory}/{marker}") for marker in markers)


def is_vendored_location(parts: tuple[str, ...], depth: int) -> bool:
    return depth == 0 or parts[depth - 1] != "node_modules"


def report_vendored_library(ctx: ScanContext, vendored_root: str, library: str) -> None:
    ctx.report(
        "DEP-VENDORED",
        Severity.HIGH,
        f"Project ships its own copy of popular library '{library}'",
        path=vendored_root + "/",
        explain="The Capital One-themed Lazarus test hid malware in modified copies of pyperclip and pyrebase. Diff against the upstream release.",
        category="dependency",
    )
