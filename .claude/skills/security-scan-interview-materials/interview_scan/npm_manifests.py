import re
from urllib.parse import urlsplit

from .command_analysis import find_interpreter_running_non_code_file
from .context import Dependency, FileRecord, ScanContext
from .findings import Severity
from .textutil import first_line_containing, safe_for_display, try_parse_jsonc
from .typosquat import find_impersonated_package

LOCKFILE_URL = re.compile(r"https?://[^\s\"',]+")
NPM_ALIAS = re.compile(r"^npm:(@?[^@\s]+)")
GITHUB_SHORTHAND = re.compile(r"^[\w.-]+/[\w.-]+(#.*)?$")
PACKAGE_MANAGER_CONFIG_RULES = {
    ".npmrc": (
        (r"(?im)^\s*(@[\w-]+:)?registry\s*=\s*(?!https://registry\.(npmjs\.org|yarnpkg\.com)/?\s*$)", Severity.HIGH, "npm registry overridden"),
        (r"(?im)^\s*(node-options|script-shell|shell|git|onload-script)\s*=", Severity.HIGH, "npm config runs a custom program or preloads code"),
        (r"(?im)^\s*ignore-scripts\s*=\s*false", Severity.MEDIUM, "npm config explicitly enables install scripts"),
    ),
    ".yarnrc.yml": (
        (r"(?im)^\s*yarnPath\s*:", Severity.HIGH, "yarnPath runs a JavaScript file from the repo on every yarn command"),
        (r"(?im)^\s*plugins\s*:", Severity.HIGH, "Yarn plugins (JavaScript executed by yarn)"),
        (r"(?im)^\s*npmRegistryServer\s*:\s*(?!\"?https://registry\.(yarnpkg\.com|npmjs\.org))", Severity.HIGH, "Yarn registry overridden"),
    ),
    ".yarnrc": (
        (r"(?im)^\s*yarn-path\s", Severity.HIGH, "yarn-path runs a JavaScript file from the repo on every yarn command"),
        (r"(?im)^\s*registry\s+(?!\"?https://registry\.(yarnpkg\.com|npmjs\.org))", Severity.HIGH, "Yarn registry overridden"),
    ),
}


def analyze_npm_projects(ctx: ScanContext) -> None:
    for record in ctx.text_files():
        match record.name:
            case "package.json":
                analyze_package_json(ctx, record)
            case "package-lock.json" | "npm-shrinkwrap.json":
                analyze_package_lock(ctx, record)
            case "yarn.lock" | "pnpm-lock.yaml" | "bun.lock":
                analyze_text_lockfile(ctx, record)
            case ".npmrc" | ".yarnrc.yml" | ".yarnrc":
                analyze_package_manager_config(ctx, record)
    report_unused_runtime_dependencies(ctx)


def analyze_package_json(ctx: ScanContext, record: FileRecord) -> None:
    data = try_parse_jsonc(record.text)
    if not isinstance(data, dict):
        ctx.report("NPM-UNPARSEABLE", Severity.MEDIUM, "package.json could not be parsed", path=record.rel, category="coverage")
        return
    in_node_modules = record.is_inside("node_modules")
    scripts = data.get("scripts")
    for script_name, command in (scripts.items() if isinstance(scripts, dict) else []):
        check_npm_script(ctx, record, script_name, str(command), in_node_modules)
    if in_node_modules:
        return
    for field_name in ctx.config.section("npm")["dependency_fields"]:
        dependencies = data.get(field_name)
        for name, spec in (dependencies.items() if isinstance(dependencies, dict) else []):
            register_and_check_npm_dependency(ctx, record, name, str(spec), field_name)


def classify_npm_script(ctx: ScanContext, script_name: str, command: str, in_node_modules: bool) -> tuple[Severity, str] | None:
    hooks = ctx.config.section("npm")
    install_hooks = hooks["consumer_install_hooks"] if in_node_modules else hooks["install_hooks"]
    runs_at_install = script_name in install_hooks
    dangerous = ctx.regex("commands.dangerous").search(command)
    fetches = ctx.regex("commands.network_fetch").search(command)
    if find_interpreter_running_non_code_file(ctx, [command]):
        return Severity.CRITICAL, "npm script runs an interpreter on a non-code file"
    if dangerous and fetches and runs_at_install:
        return Severity.CRITICAL, f"Install-time npm script '{script_name}' downloads and runs code"
    if dangerous:
        return Severity.HIGH, f"npm script '{script_name}' downloads, decodes, or runs inline code"
    if runs_at_install:
        severity = Severity.HIGH if in_node_modules else Severity.MEDIUM
        return severity, f"npm '{script_name}' script runs automatically on npm install"
    if script_name in hooks["run_hooks"] and not in_node_modules:
        return Severity.INFO, f"'npm run {script_name}' executes this command"
    return None


def check_npm_script(ctx: ScanContext, record: FileRecord, script_name: str, command: str, in_node_modules: bool) -> None:
    classification = classify_npm_script(ctx, script_name, command, in_node_modules)
    if classification is None:
        return
    severity, title = classification
    ctx.report(
        "NPM-SCRIPT",
        severity,
        title,
        path=record.rel,
        line=first_line_containing(record.text, f'"{script_name}"'),
        snippet=safe_for_display(f"{script_name}: {command}", 300),
        explain="Remember the entry file of 'npm start' also runs. Payloads often hide in server bootstrap code, not in scripts.",
        category="install-hook",
    )


def register_and_check_npm_dependency(ctx: ScanContext, record: FileRecord, name: str, spec: str, field_name: str) -> None:
    ctx.dependencies.append(Dependency("npm", name, spec, f"{record.rel} ({field_name})"))
    line = first_line_containing(record.text, f'"{name}"')
    if name.lower() in ctx.config.iocs.npm_packages:
        report_dependency(ctx, record, line, "DEP-IOC", Severity.CRITICAL, f"Known malicious npm package '{name}'", f"{name}: {spec}")
    source_problem = describe_non_registry_npm_source(spec)
    if source_problem:
        severity = Severity.MEDIUM if spec.startswith("npm:") else Severity.HIGH
        report_dependency(ctx, record, line, "DEP-SOURCE", severity, f"Dependency '{name}' comes from {source_problem}", f"{name}: {spec}")
    aliased_name = npm_alias_target(spec)
    if aliased_name and aliased_name.lower() in ctx.config.iocs.npm_packages:
        report_dependency(ctx, record, line, "DEP-IOC", Severity.CRITICAL, f"Alias '{name}' installs known malicious package '{aliased_name}'", f"{name}: {spec}")
    impersonated = find_impersonated_package(name, set(ctx.config.section("popular")["npm"]), ctx.config.section("npm")["typosquat_suffixes"])
    if impersonated:
        report_dependency(ctx, record, line, "DEP-TYPOSQUAT", Severity.MEDIUM, f"Package name '{name}' resembles popular package '{impersonated}'", f"{name}: {spec}")


def describe_non_registry_npm_source(spec: str) -> str | None:
    spec = spec.strip()
    if spec.startswith(("git+", "git:", "git@", "github:", "gitlab:", "bitbucket:", "gist:")):
        return "a git repository instead of the registry"
    if spec.startswith(("http://", "https://")):
        return "a tarball URL instead of the registry"
    if spec.startswith(("file:", "link:", "portal:")):
        return "a local path bundled with the project"
    if spec.startswith("npm:"):
        return "an alias to a differently named package"
    if GITHUB_SHORTHAND.match(spec):
        return "a GitHub repository shorthand"
    return None


def npm_alias_target(spec: str) -> str | None:
    match = NPM_ALIAS.match(spec.strip())
    return match.group(1) if match else None


def report_dependency(ctx: ScanContext, record: FileRecord, line, rule: str, severity: Severity, title: str, snippet: str) -> None:
    ctx.report(rule, severity, title, path=record.rel, line=line, snippet=safe_for_display(snippet), category="dependency")


def analyze_package_lock(ctx: ScanContext, record: FileRecord) -> None:
    data = try_parse_jsonc(record.text)
    if not isinstance(data, dict):
        analyze_text_lockfile(ctx, record)
        return
    packages_with_install_scripts = []
    for name, entry in iterate_package_lock_entries(data):
        check_locked_package(ctx, record, name, entry)
        if entry.get("hasInstallScript"):
            packages_with_install_scripts.append(name)
    if packages_with_install_scripts:
        report_locked_install_scripts(ctx, record, packages_with_install_scripts)


def iterate_package_lock_entries(data: dict):
    for path, entry in (data.get("packages") or {}).items():
        if path and isinstance(entry, dict):
            yield path.rsplit("node_modules/", 1)[-1], entry
    yield from iterate_legacy_lock_dependencies(data.get("dependencies") or {})


def iterate_legacy_lock_dependencies(dependencies: dict):
    for name, entry in dependencies.items():
        if isinstance(entry, dict):
            yield name, entry
            yield from iterate_legacy_lock_dependencies(entry.get("dependencies") or {})


def check_locked_package(ctx: ScanContext, record: FileRecord, name: str, entry: dict) -> None:
    line = first_line_containing(record.text, f'"{name}"')
    if name.lower() in ctx.config.iocs.npm_packages:
        report_dependency(ctx, record, line, "DEP-IOC", Severity.CRITICAL, f"Lockfile contains known malicious package '{name}'", name)
    resolved = str(entry.get("resolved", ""))
    if resolved.startswith("http") and not is_allowed_registry_url(ctx, resolved, "npm_hosts"):
        report_dependency(ctx, record, line, "LOCK-SOURCE", Severity.HIGH, f"Locked package '{name}' resolves outside the npm registry", resolved)


def is_allowed_registry_url(ctx: ScanContext, url: str, registry_key: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ctx.config.section("registries")[registry_key]


def report_locked_install_scripts(ctx: ScanContext, record: FileRecord, names: list[str]) -> None:
    ctx.report(
        "LOCK-INSTALL-SCRIPTS",
        Severity.MEDIUM,
        f"{len(names)} locked packages run install scripts on npm install",
        path=record.rel,
        snippet=safe_for_display(", ".join(names), 400),
        explain="Install with --ignore-scripts, and check these packages before allowing their scripts.",
        category="dependency",
    )


def analyze_text_lockfile(ctx: ScanContext, record: FileRecord) -> None:
    for match in LOCKFILE_URL.finditer(record.text):
        if not is_allowed_registry_url(ctx, match.group(0), "npm_hosts"):
            report_dependency(ctx, record, record.text.count("\n", 0, match.start()) + 1, "LOCK-SOURCE", Severity.HIGH,
                              "Lockfile resolves a package outside the npm registry", match.group(0))
    for name in ctx.config.iocs.npm_packages:
        match = re.search(r"(?<![\w@/.-])" + re.escape(name) + r"@", record.text)
        if match:
            report_dependency(ctx, record, record.text.count("\n", 0, match.start()) + 1, "DEP-IOC", Severity.CRITICAL,
                              f"Lockfile contains known malicious package '{name}'", name)


def analyze_package_manager_config(ctx: ScanContext, record: FileRecord) -> None:
    for pattern, severity, title in PACKAGE_MANAGER_CONFIG_RULES[record.name]:
        match = re.search(pattern, record.text)
        if match:
            line = record.text.count("\n", 0, match.start()) + 1
            line_text = record.text.splitlines()[line - 1] if record.text else ""
            ctx.report("NPM-CONFIG", severity, title, path=record.rel, line=line, snippet=safe_for_display(line_text), category="dependency")


def report_unused_runtime_dependencies(ctx: ScanContext) -> None:
    runtime = [d for d in ctx.dependencies if d.ecosystem == "npm" and d.declared_in.endswith("(dependencies)")]
    if not runtime:
        return
    code = "\n".join(
        r.text for r in ctx.text_files()
        if r.name != "package.json" and not ctx.is_lockfile(r.rel) and not r.is_inside("node_modules")
    )
    for dependency in runtime:
        if not any(quote + dependency.name in code for quote in ("'", '"', "`")):
            ctx.report(
                "DEP-UNUSED",
                Severity.MEDIUM,
                f"Dependency '{dependency.name}' is declared but never imported",
                path=dependency.declared_in.split(" ")[0],
                snippet=safe_for_display(f"{dependency.name}: {dependency.spec}"),
                explain="Unused dependencies can exist only to run install scripts or pull in a malicious sub-dependency.",
                category="dependency",
            )
