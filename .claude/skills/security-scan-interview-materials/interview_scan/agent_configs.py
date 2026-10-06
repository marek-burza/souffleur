import json

from .context import FileRecord, ScanContext
from .findings import Severity
from .textutil import LineIndex, make_snippet, path_matches_any, safe_for_display, try_parse_jsonc

CLAUDE_COMMAND_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper", "statusLine")


def analyze_agent_configs(ctx: ScanContext) -> None:
    for record in ctx.files_matching(ctx.config.agent_files):
        report_agent_file_present(ctx, record)
        if not record.is_text:
            continue
        report_commands_in_agent_file(ctx, record)
        analyze_agent_json_config(ctx, record)
    ctx.summary["agent_files"] = [r.rel for r in ctx.files_matching(ctx.config.agent_files)]


def report_agent_file_present(ctx: ScanContext, record: FileRecord) -> None:
    ctx.report(
        "AGENT-FILE",
        Severity.MEDIUM,
        "AI-agent instruction or config file (treated as untrusted data, renamed in the extracted copy)",
        path=record.rel,
        explain="A malicious repo can use these files to steer an AI assistant into running its code or hiding findings.",
        category="prompt-injection",
    )


def report_commands_in_agent_file(ctx: ScanContext, record: FileRecord) -> None:
    match = ctx.regex("commands.dangerous").search(record.text)
    if not match:
        return
    index = LineIndex(record.text)
    ctx.report(
        "AR-8-AGENT-COMMANDS",
        Severity.HIGH,
        "AI-agent file contains download or execution commands",
        path=record.rel,
        line=index.line_number(match.start()),
        snippet=make_snippet(record.text, index, match.start(), match.end()),
        category="prompt-injection",
    )


def analyze_agent_json_config(ctx: ScanContext, record: FileRecord) -> None:
    if not record.name.endswith(".json"):
        return
    data = try_parse_jsonc(record.text)
    if not isinstance(data, dict):
        return
    if path_matches_any(record.rel, [".claude/settings.json", ".claude/settings.local.json"]):
        analyze_claude_settings(ctx, record, data)
    servers = data.get("mcpServers") or data.get("servers")
    if isinstance(servers, dict):
        analyze_mcp_servers(ctx, record, servers)


def analyze_claude_settings(ctx: ScanContext, record: FileRecord, settings: dict) -> None:
    if settings.get("hooks"):
        report_agent_setting(ctx, record, "Claude Code hooks run shell commands automatically", settings["hooks"])
    allowed = settings.get("permissions", {}).get("allow", []) if isinstance(settings.get("permissions"), dict) else []
    if allowed:
        report_agent_setting(ctx, record, "Pre-approves tool permissions for the agent", allowed)
    if settings.get("enableAllProjectMcpServers"):
        report_agent_setting(ctx, record, "Auto-enables every MCP server in the project", True)
    for key in CLAUDE_COMMAND_KEYS:
        if settings.get(key):
            report_agent_setting(ctx, record, f"Setting '{key}' runs a command", settings[key])


def analyze_mcp_servers(ctx: ScanContext, record: FileRecord, servers: dict) -> None:
    for name, server in servers.items():
        if isinstance(server, dict) and server.get("command"):
            report_agent_setting(ctx, record, f"MCP server '{name}' launches a local command", server)
        elif isinstance(server, dict) and server.get("url"):
            report_agent_setting(ctx, record, f"MCP server '{name}' connects to a remote URL", server, Severity.MEDIUM)


def report_agent_setting(ctx: ScanContext, record: FileRecord, title: str, value, severity: Severity = Severity.HIGH) -> None:
    ctx.report(
        "AR-8-AGENT-CONFIG",
        severity,
        title,
        path=record.rel,
        snippet=safe_for_display(json.dumps(value), 300),
        category="prompt-injection",
    )
