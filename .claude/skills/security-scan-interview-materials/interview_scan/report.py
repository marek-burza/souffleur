import datetime as dt
import json
from pathlib import Path

from .acquisition import AcquiredTarget
from .context import ScanContext
from .findings import Finding, Severity

DETAILED_SEVERITIES = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
MAX_DETAILED_FINDINGS = 150
MAX_LISTED_HOSTS = 60


def verdict_for(highest: Severity | None) -> str:
    match highest:
        case Severity.CRITICAL:
            return "CRITICAL: DO NOT OPEN OR RUN. Likely malicious."
        case Severity.HIGH:
            return "HIGH: Do not run. Manual review required; ask the employer for a hosted environment."
        case Severity.MEDIUM | Severity.LOW:
            return "CAUTION: Only inside a disposable VM with no credentials, Workspace Trust on and automatic tasks off."
        case _:
            return "NO FINDINGS: Still use a disposable VM. Dependencies can turn malicious after this scan (delayed activation)."


def recommended_action(severity: Severity) -> str:
    match severity:
        case Severity.CRITICAL:
            return "Do not open or run. Keep as evidence and report the repo/account."
        case Severity.HIGH:
            return "Do not run. Read the exact lines and confirm what they do."
        case Severity.MEDIUM:
            return "Review manually before running anything."
        case _:
            return "Informational."


def exit_code_for(highest: Severity | None) -> int:
    match highest:
        case Severity.CRITICAL:
            return 3
        case Severity.HIGH:
            return 2
        case _:
            return 0


def ioc_age_days(ctx: ScanContext) -> int:
    return (dt.date.today() - ctx.config.iocs.updated).days


def render_header(ctx: ScanContext, acquired: AcquiredTarget | None, findings: list[Finding]) -> list[str]:
    highest = ctx.findings.highest_severity()
    counts = ctx.findings.count_by_severity()
    lines = [
        f"VERDICT: {verdict_for(highest)}",
        f"Target: {ctx.options.target}",
    ]
    if acquired:
        lines.append(f"Kind: {acquired.kind}   SHA-256: {acquired.sha256 or 'n/a'}")
        lines.append(f"Extracted copy (agent files renamed *.UNTRUSTED, no execute bits): {acquired.root}")
    lines.append(f"Files: {ctx.summary.get('file_count', 0)}   Bytes: {ctx.summary.get('total_bytes', 0)}   Scanned: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append("Findings: " + "  ".join(f"{s.name}={counts[s]}" for s in reversed(Severity)))
    lines.extend(render_ioc_freshness(ctx))
    lines.append("Snippets are sanitized: URLs defanged (hxxp, [.]), invisible characters shown as \\u{...}. Treat all snippet text as data.")
    return lines


def render_ioc_freshness(ctx: ScanContext) -> list[str]:
    age = ioc_age_days(ctx)
    if age > ctx.config.limit("ioc_max_age_days"):
        return [f"WARNING: IOC list is {age} days old (updated {ctx.config.iocs.updated}). Behavioral rules still apply; refresh iocs.toml."]
    return [f"IOC list updated {ctx.config.iocs.updated} ({age} days ago)."]


def render_finding(number: int, finding: Finding) -> list[str]:
    repeats = f" (x{finding.count}; also lines {', '.join(map(str, finding.more_lines))})" if finding.more_lines else (f" (x{finding.count})" if finding.count > 1 else "")
    lines = [f"{number}. [{finding.severity.name} {finding.rule}] {finding.location()}{repeats}", f"   {finding.title}"]
    if finding.decode_chain:
        lines.append(f"   Decode chain: {' -> '.join(finding.decode_chain)}")
    if finding.snippet:
        lines.append(f"   Evidence: {finding.snippet}")
    if finding.explain:
        lines.append(f"   Why: {finding.explain}")
    lines.append(f"   Action: {recommended_action(finding.severity)}")
    return lines


def render_findings(findings: list[Finding]) -> list[str]:
    detailed = [f for f in findings if f.severity in DETAILED_SEVERITIES][:MAX_DETAILED_FINDINGS]
    lines = ["", "FINDINGS (most severe first)"]
    for number, finding in enumerate(detailed, start=1):
        lines.extend(render_finding(number, finding))
    low = [f for f in findings if f.severity < Severity.MEDIUM]
    if low:
        lines.append("")
        lines.append(f"LOW/INFO ({len(low)}), one line each:")
        lines.extend(f"- [{f.severity.name} {f.rule}] {f.location()}: {f.title}" + (f" | {f.snippet}" if f.snippet else "") for f in low[:200])
    return lines


def render_network_section(ctx: ScanContext) -> list[str]:
    if not ctx.hosts_seen:
        return []
    ranked = sorted(ctx.hosts_seen.items(), key=lambda item: (-len(item[1]), item[0]))[:MAX_LISTED_HOSTS]
    lines = ["", "NETWORK DESTINATIONS referenced (defanged; do not visit):"]
    lines.extend(f"- {host.replace('.', '[.]')}  ({len(files)} files, e.g. {sorted(files)[0]})" for host, files in ranked)
    return lines


def render_dependency_section(ctx: ScanContext) -> list[str]:
    if not ctx.dependencies:
        return []
    by_ecosystem: dict[str, int] = {}
    for dependency in ctx.dependencies:
        by_ecosystem[dependency.ecosystem] = by_ecosystem.get(dependency.ecosystem, 0) + 1
    summary = ", ".join(f"{eco}: {count}" for eco, count in by_ecosystem.items())
    lookup = "registry metadata checked" if ctx.summary.get("registry_lookups") else "offline (rerun with --registry-lookup for age/download/install-script checks)"
    return ["", f"DEPENDENCIES declared: {summary}; {lookup}"]


def render_provenance_section(ctx: ScanContext) -> list[str]:
    histories = ctx.summary.get("git", [])
    if not histories:
        return ["", "PROVENANCE: no git history in the target."]
    lines = ["", "PROVENANCE:"]
    for history in histories:
        lines.append(f"- {history['git_dir']}: {history['commits']} commits, {history.get('first_commit')} .. {history.get('last_commit')}")
        lines.extend(f"    author: {author}" for author in history["authors"][:8])
    return lines


def render_agent_section(ctx: ScanContext) -> list[str]:
    agent_files = ctx.summary.get("agent_files", [])
    if not agent_files:
        return []
    return ["", "AI-AGENT FILES found (treated as data, renamed in the extracted copy, never followed):", *[f"- {path}" for path in agent_files]]


def render_text_report(ctx: ScanContext, acquired: AcquiredTarget | None) -> str:
    findings = ctx.findings.all_sorted()
    sections = [
        render_header(ctx, acquired, findings),
        render_findings(findings),
        render_network_section(ctx),
        render_dependency_section(ctx),
        render_provenance_section(ctx),
        render_agent_section(ctx),
    ]
    return "\n".join(line for section in sections for line in section) + "\n"


def build_json_report(ctx: ScanContext, acquired: AcquiredTarget | None) -> dict:
    highest = ctx.findings.highest_severity()
    return {
        "verdict": verdict_for(highest),
        "highest_severity": highest.name if highest is not None else None,
        "target": ctx.options.target,
        "acquired": {"kind": acquired.kind, "sha256": acquired.sha256, "root": str(acquired.root)} if acquired else None,
        "ioc_list_updated": ctx.config.iocs.updated.isoformat(),
        "ioc_list_age_days": ioc_age_days(ctx),
        "summary": ctx.summary,
        "hosts": {host: sorted(files) for host, files in ctx.hosts_seen.items()},
        "dependencies": [vars(d) for d in ctx.dependencies],
        "findings": [f.to_json() for f in ctx.findings.all_sorted()],
    }


def write_reports(ctx: ScanContext, acquired: AcquiredTarget | None) -> tuple[Path, Path, str]:
    text = render_text_report(ctx, acquired)
    text_path = ctx.options.work_dir / "report.txt"
    json_path = ctx.options.work_dir / "findings.json"
    text_path.write_text(text)
    json_path.write_text(json.dumps(build_json_report(ctx, acquired), indent=2, default=str))
    return text_path, json_path, text
