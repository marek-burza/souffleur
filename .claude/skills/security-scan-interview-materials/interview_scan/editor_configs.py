import json
import re

from .command_analysis import find_interpreter_running_non_code_file
from .context import FileRecord, ScanContext
from .findings import Severity
from .textutil import first_line_containing, path_matches, safe_for_display, try_parse_jsonc

PLATFORM_KEYS = ("osx", "linux", "windows")
DEVCONTAINER_HOST_COMMAND = "initializeCommand"
DEVCONTAINER_CONTAINER_COMMANDS = ("onCreateCommand", "updateContentCommand", "postCreateCommand", "postStartCommand", "postAttachCommand")
INJECTION_ENV_VARS = re.compile(r"\b(NODE_OPTIONS|PYTHONSTARTUP|PYTHONPATH|LD_PRELOAD|DYLD_INSERT_LIBRARIES|BASH_ENV|ENV|PERL5OPT|RUBYOPT)\b")
PRIVILEGED_CONTAINER = re.compile(r"--privileged|--cap-add|SYS_ADMIN|source=/(,|\")|\$\{localEnv:(HOME|USERPROFILE)\}|/var/run/docker\.sock|--network=host|--pid=host")


def analyze_editor_and_container_configs(ctx: ScanContext) -> None:
    handlers = (
        (".vscode/tasks.json", analyze_tasks_file),
        ("*.code-workspace", analyze_workspace_file),
        (".vscode/settings.json", analyze_settings_file),
        (".vscode/launch.json", analyze_launch_file),
        (".vscode/extensions.json", analyze_extension_recommendations_file),
        (".devcontainer.json", analyze_devcontainer_file),
        ("devcontainer.json", analyze_devcontainer_file),
    )
    for record in ctx.text_files():
        for pattern, handler in handlers:
            if path_matches(record.rel, pattern):
                handler(ctx, record)
                break


def load_jsonc_or_report(ctx: ScanContext, record: FileRecord):
    data = try_parse_jsonc(record.text)
    if data is None:
        ctx.report(
            "CFG-UNPARSEABLE",
            Severity.MEDIUM,
            "Editor config could not be parsed; only text patterns were applied",
            path=record.rel,
            category="coverage",
        )
    return data


def analyze_tasks_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if isinstance(data, dict):
        for task in as_list(data.get("tasks")):
            analyze_single_task(ctx, record, task)


def analyze_workspace_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if not isinstance(data, dict):
        return
    for task in as_list(as_dict(data.get("tasks")).get("tasks")):
        analyze_single_task(ctx, record, task)
    analyze_settings_mapping(ctx, record, as_dict(data.get("settings")))
    for configuration in as_list(as_dict(data.get("launch")).get("configurations")):
        analyze_launch_configuration(ctx, record, configuration)
    analyze_extension_recommendations(ctx, record, as_list(as_dict(data.get("extensions")).get("recommendations")))


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def analyze_single_task(ctx: ScanContext, record: FileRecord, task) -> None:
    if not isinstance(task, dict):
        return
    label = str(task.get("label") or task.get("taskName") or "(unlabeled)")
    line = first_line_containing(record.text, f'"{label}"')
    commands = collect_task_commands(task)
    runs_on_open = as_dict(task.get("runOptions")).get("runOn") == "folderOpen"
    dangerous = {p: c for p, c in commands.items() if ctx.regex("commands.dangerous").search(c)}
    disguised = find_interpreter_running_non_code_file(ctx, commands.values())
    summary = f"task '{label}': " + " || ".join(f"[{p}] {c}" for p, c in commands.items())

    if runs_on_open:
        report_task(ctx, record, line, "AR-1-FOLDER-OPEN", Severity.CRITICAL if dangerous or disguised else Severity.HIGH,
                    "Task runs automatically when the folder is opened", summary)
    if dangerous:
        report_task(ctx, record, line, "AR-2-TASK-COMMAND", Severity.CRITICAL if runs_on_open else Severity.HIGH,
                    "Task command downloads, decodes, or launches hidden code", summary)
    if task_hides_its_output(task):
        report_task(ctx, record, line, "AR-3-HIDDEN-TASK", Severity.HIGH, "Task hides its terminal output", summary)
    if count_platforms_fetching_remote_code(ctx, commands) >= 2:
        report_task(ctx, record, line, "AR-4-PER-OS-FETCH", Severity.CRITICAL,
                    "Task has separate per-OS commands that all fetch remote content", summary)
    if disguised:
        report_task(ctx, record, line, "AR-5-DISGUISED-FILE", Severity.CRITICAL,
                    f"Task runs an interpreter on a non-code file: {disguised}", summary)


def collect_task_commands(task: dict) -> dict[str, str]:
    commands = {}
    default = command_line_of(task)
    if default:
        commands["default"] = default
    for platform in PLATFORM_KEYS:
        platform_command = command_line_of(as_dict(task.get(platform)))
        if platform_command:
            commands[platform] = platform_command
    return commands


def command_line_of(block: dict) -> str:
    command = block.get("command")
    parts = [" ".join(map(str, command)) if isinstance(command, list) else str(command)] if command else []
    for argument in as_list(block.get("args")):
        parts.append(str(argument.get("value", "")) if isinstance(argument, dict) else str(argument))
    return " ".join(parts).strip()


def task_hides_its_output(task: dict) -> bool:
    presentation = as_dict(task.get("presentation"))
    if presentation.get("reveal") == "never":
        return True
    return presentation.get("echo") is False and presentation.get("close") is True


def count_platforms_fetching_remote_code(ctx: ScanContext, commands: dict[str, str]) -> int:
    fetch = ctx.regex("commands.network_fetch")
    return sum(1 for platform in PLATFORM_KEYS if fetch.search(commands.get(platform, "")))


def report_task(ctx: ScanContext, record: FileRecord, line, rule: str, severity: Severity, title: str, summary: str) -> None:
    ctx.report(
        rule,
        severity,
        title,
        path=record.rel,
        line=line,
        snippet=safe_for_display(summary, 400),
        explain="Contagious Interview and UNK_DeadDrop deliver malware through .vscode/tasks.json tasks that run on folder open.",
        category="autorun",
    )


def analyze_settings_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if isinstance(data, dict):
        analyze_settings_mapping(ctx, record, data)


def analyze_settings_mapping(ctx: ScanContext, record: FileRecord, settings: dict) -> None:
    for key, value in settings.items():
        rule = first_matching_setting_rule(ctx, key, value)
        if rule:
            ctx.report(
                "CFG-VSCODE-SETTING",
                rule.severity,
                rule.title,
                path=record.rel,
                line=first_line_containing(record.text, f'"{key}"'),
                snippet=safe_for_display(f"{key}: {json.dumps(value)}"),
                category="editor-config",
            )


def first_matching_setting_rule(ctx: ScanContext, key: str, value):
    for rule in ctx.config.vscode_settings:
        if not rule.key.search(key):
            continue
        if rule.value is None or (isinstance(value, str) and rule.value.search(value)):
            return rule
    return None


def analyze_launch_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if isinstance(data, dict):
        for configuration in as_list(data.get("configurations")):
            analyze_launch_configuration(ctx, record, configuration)


def analyze_launch_configuration(ctx: ScanContext, record: FileRecord, configuration) -> None:
    if not isinstance(configuration, dict):
        return
    name = str(configuration.get("name", "(unnamed)"))
    line = first_line_containing(record.text, f'"{name}"')
    serialized = json.dumps(configuration)
    findings = launch_configuration_problems(ctx, configuration, serialized)
    for severity, title in findings:
        ctx.report("CFG-LAUNCH", severity, title, path=record.rel, line=line,
                   snippet=safe_for_display(f"{name}: {serialized}", 400), category="editor-config")


def launch_configuration_problems(ctx: ScanContext, configuration: dict, serialized: str) -> list[tuple[Severity, str]]:
    problems = []
    if configuration.get("preLaunchTask"):
        problems.append((Severity.LOW, f"Debug configuration runs task '{configuration['preLaunchTask']}' first"))
    if configuration.get("runtimeExecutable"):
        problems.append((Severity.MEDIUM, "Debug configuration uses a custom runtime executable"))
    if INJECTION_ENV_VARS.search(json.dumps(configuration.get("env", {}))):
        problems.append((Severity.HIGH, "Debug configuration sets code-injection environment variables"))
    if ctx.regex("commands.dangerous").search(serialized):
        problems.append((Severity.HIGH, "Debug configuration contains download or hidden-execution commands"))
    program = [str(configuration.get("program", "")), str(configuration.get("runtimeExecutable", ""))]
    disguised = find_interpreter_running_non_code_file(ctx, [f"node {p}" for p in program if p])
    if disguised:
        problems.append((Severity.CRITICAL, f"Debug configuration runs a non-code file: {disguised}"))
    return problems


def analyze_extension_recommendations_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if isinstance(data, dict):
        analyze_extension_recommendations(ctx, record, as_list(data.get("recommendations")))


def analyze_extension_recommendations(ctx: ScanContext, record: FileRecord, recommendations: list) -> None:
    vscode = ctx.config.section("vscode")
    trusted = set(vscode["trusted_publishers"])
    brand = ctx.regex("vscode.brand_words")
    for extension_id in map(str, recommendations):
        publisher = extension_id.split(".", 1)[0].lower()
        if publisher in trusted:
            continue
        impersonates = bool(brand.search(extension_id))
        ctx.report(
            "AR-7-EXTENSION",
            Severity.HIGH if impersonates else Severity.MEDIUM,
            "Recommended extension uses a brand name but is not from that vendor" if impersonates
            else "Recommended extension from a publisher that is not on the trusted list",
            path=record.rel,
            line=first_line_containing(record.text, extension_id),
            snippet=safe_for_display(extension_id),
            explain="UNK_DeadDrop installed a malicious extension posing as a Google service. Verify the publisher on the marketplace.",
            category="editor-config",
        )


def analyze_devcontainer_file(ctx: ScanContext, record: FileRecord) -> None:
    data = load_jsonc_or_report(ctx, record)
    if not isinstance(data, dict):
        return
    if DEVCONTAINER_HOST_COMMAND in data:
        report_devcontainer_command(ctx, record, data, DEVCONTAINER_HOST_COMMAND, Severity.HIGH)
    for key in DEVCONTAINER_CONTAINER_COMMANDS:
        if key in data:
            report_devcontainer_command(ctx, record, data, key, Severity.MEDIUM)
    report_privileged_devcontainer(ctx, record, data)


def flatten_command_value(value) -> str:
    if isinstance(value, dict):
        return " && ".join(flatten_command_value(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(map(str, value))
    return str(value)


def report_devcontainer_command(ctx: ScanContext, record: FileRecord, data: dict, key: str, base: Severity) -> None:
    command = flatten_command_value(data[key])
    dangerous = ctx.regex("commands.dangerous").search(command)
    severity = Severity.CRITICAL if dangerous and base >= Severity.HIGH else Severity.HIGH if dangerous else base
    where = "on your host machine" if key == DEVCONTAINER_HOST_COMMAND else "inside the container"
    ctx.report(
        "AR-6-DEVCONTAINER",
        severity,
        f"devcontainer {key} runs a command {where}",
        path=record.rel,
        line=first_line_containing(record.text, f'"{key}"'),
        snippet=safe_for_display(command, 300),
        category="autorun",
    )


def report_privileged_devcontainer(ctx: ScanContext, record: FileRecord, data: dict) -> None:
    serialized = json.dumps({k: data.get(k) for k in ("runArgs", "mounts", "privileged", "capAdd", "workspaceMount")})
    match = PRIVILEGED_CONTAINER.search(serialized)
    if match or data.get("privileged") is True:
        ctx.report(
            "AR-6-DEVCONTAINER",
            Severity.HIGH,
            "devcontainer runs privileged or mounts host directories",
            path=record.rel,
            snippet=safe_for_display(serialized, 300),
            category="autorun",
        )
