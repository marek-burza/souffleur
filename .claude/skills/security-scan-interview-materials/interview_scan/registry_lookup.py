import datetime as dt
import json
import urllib.error
import urllib.request
from urllib.parse import quote

from .context import Dependency, ScanContext
from .findings import Severity
from .npm_manifests import describe_non_registry_npm_source

NPM_PACKAGE_URL = "https://registry.npmjs.org/{name}"
NPM_DOWNLOADS_URL = "https://api.npmjs.org/downloads/point/last-week/{name}"
PYPI_PACKAGE_URL = "https://pypi.org/pypi/{name}/json"
REQUEST_HEADERS = {"User-Agent": "security-scan-interview-materials", "Accept": "application/json"}
REQUEST_TIMEOUT_SECONDS = 20


def run_registry_lookups(ctx: ScanContext) -> None:
    looked_up = set()
    for dependency in ctx.dependencies:
        key = (dependency.ecosystem, dependency.name.lower())
        if key in looked_up or not is_registry_dependency(dependency):
            continue
        looked_up.add(key)
        for severity, title in issues_for_dependency(ctx, dependency):
            ctx.report("REG-METADATA", severity, title, path=dependency.declared_in.split(" ")[0],
                       snippet=f"{dependency.ecosystem}: {dependency.name}", category="dependency")
    ctx.summary["registry_lookups"] = len(looked_up)


def is_registry_dependency(dependency: Dependency) -> bool:
    if dependency.ecosystem == "npm":
        return describe_non_registry_npm_source(dependency.spec) is None
    return "://" not in dependency.spec and not dependency.spec.lstrip().startswith((".", "/", "-"))


def issues_for_dependency(ctx: ScanContext, dependency: Dependency) -> list[tuple[Severity, str]]:
    match dependency.ecosystem:
        case "npm":
            return npm_package_issues(ctx, dependency.name)
        case "pypi":
            return pypi_package_issues(ctx, dependency.name)
    return []


def fetch_registry_json(url: str) -> tuple[str, dict | None]:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return "ok", json.load(response)
    except urllib.error.HTTPError as error:
        return ("missing", None) if error.code == 404 else ("error", None)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return "error", None


def days_since(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    moment = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - moment).days


def npm_package_issues(ctx: ScanContext, name: str) -> list[tuple[Severity, str]]:
    status, document = fetch_registry_json(NPM_PACKAGE_URL.format(name=quote(name, safe="@")))
    match status:
        case "missing":
            return [(Severity.HIGH, f"npm package '{name}' does not exist on the registry (removed as malware, or dependency confusion)")]
        case "error":
            return [(Severity.INFO, f"npm lookup for '{name}' failed")]
    return npm_document_issues(ctx, name, document) + npm_download_issues(ctx, name)


def npm_document_issues(ctx: ScanContext, name: str, document: dict) -> list[tuple[Severity, str]]:
    limits = ctx.config.section("limits")
    latest = document.get("dist-tags", {}).get("latest", "")
    times = document.get("time", {})
    issues = []
    if "security" in latest:
        issues.append((Severity.CRITICAL, f"npm replaced '{name}' with a security placeholder (it was removed as malware)"))
    age = days_since(times.get("created"))
    if age is not None and age < limits["registry_new_package_days"]:
        issues.append((Severity.HIGH, f"npm package '{name}' was first published {age} days ago"))
    latest_age = days_since(times.get(latest))
    if latest_age is not None and latest_age < limits["registry_fresh_version_days"]:
        issues.append((Severity.MEDIUM, f"Latest version of '{name}' was published {latest_age} days ago"))
    scripts = document.get("versions", {}).get(latest, {}).get("scripts", {})
    hooks = [h for h in ctx.config.section("npm")["install_hooks"] if h in scripts]
    if hooks:
        issues.append((Severity.HIGH, f"Latest version of '{name}' runs install scripts: {', '.join(hooks)}"))
    return issues


def npm_download_issues(ctx: ScanContext, name: str) -> list[tuple[Severity, str]]:
    status, document = fetch_registry_json(NPM_DOWNLOADS_URL.format(name=quote(name, safe="@")))
    if status != "ok":
        return []
    downloads = document.get("downloads", 0)
    if downloads < ctx.config.limit("registry_low_weekly_downloads"):
        return [(Severity.MEDIUM, f"npm package '{name}' has only {downloads} downloads last week")]
    return []


def pypi_package_issues(ctx: ScanContext, name: str) -> list[tuple[Severity, str]]:
    status, document = fetch_registry_json(PYPI_PACKAGE_URL.format(name=quote(name)))
    match status:
        case "missing":
            return [(Severity.HIGH, f"PyPI package '{name}' does not exist (removed as malware, or dependency confusion)")]
        case "error":
            return [(Severity.INFO, f"PyPI lookup for '{name}' failed")]
    issues = []
    age = days_since(earliest_pypi_upload(document))
    if age is not None and age < ctx.config.limit("registry_new_package_days"):
        issues.append((Severity.HIGH, f"PyPI package '{name}' was first published {age} days ago"))
    if not any(f.get("packagetype") == "bdist_wheel" for f in document.get("urls", [])):
        issues.append((Severity.MEDIUM, f"Latest '{name}' release has no wheel, so pip runs its build code on install"))
    return issues


def earliest_pypi_upload(document: dict) -> str | None:
    uploads = [f.get("upload_time_iso_8601") for files in document.get("releases", {}).values() for f in files]
    uploads = [u for u in uploads if u]
    return min(uploads) if uploads else None
