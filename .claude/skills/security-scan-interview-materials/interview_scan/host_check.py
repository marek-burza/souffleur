import datetime as dt
import re
import subprocess
from pathlib import Path

from .context import ScanContext
from .findings import Severity
from .textutil import safe_for_display

RECENT_DAYS = 30
EDITOR_EXTENSION_DIRS = ("~/.vscode/extensions", "~/.cursor/extensions", "~/.windsurf/extensions", "~/.vscode-server/extensions", "~/.vscode-oss/extensions")
EDITOR_HOME_DIRS = ("~/.vscode", "~/.cursor", "~/.windsurf")
AUTOSTART_DIRS = ("~/.config/autostart", "~/.config/systemd/user", "~/.local/share/systemd/user", "/etc/systemd/system", "/etc/cron.d", "/etc/xdg/autostart")
SHELL_PROFILES = ("~/.bashrc", "~/.zshrc", "~/.profile", "~/.bash_profile", "~/.bash_login", "~/.zprofile", "~/.zshenv", "~/.xprofile", "~/.config/fish/config.fish")
SUSPICIOUS_COMMAND = re.compile(r"(?i)\b(curl|wget)\b.*\|\s*(sh|bash|zsh|node|python)|\bnode\s+-e\b|base64\s+(-d|--decode)|/\.n2/|\.vscode/[\w-]+\.(sh|js)|nohup\b.*&")
SUSPICIOUS_PROCESS = re.compile(r"(?i)\bnode\b.*\s(-e|--eval)\s|\b(node|python[0-9.]*)\b.*(/\.(vscode|cursor)/(?!extensions/|bin/)|/\.n2/|\.npl\b|\.sysinfo\b|0001\.dat|nvidia\.py)")
EDITOR_HOME_SCRIPT = re.compile(r"\.(sh|js|cjs|mjs|py|command)$|^package\.json$")


def run_host_check(ctx: ScanContext) -> None:
    check_known_artifact_paths(ctx)
    check_scripts_in_editor_home_dirs(ctx)
    check_editor_extensions(ctx)
    check_recent_autostart_entries(ctx)
    check_shell_profiles(ctx)
    check_crontab(ctx)
    check_running_processes(ctx)


def is_recent(path: Path) -> bool:
    modified = dt.datetime.fromtimestamp(path.lstat().st_mtime)
    return dt.datetime.now() - modified < dt.timedelta(days=RECENT_DAYS)


def report_host(ctx: ScanContext, rule: str, severity: Severity, title: str, where: str, snippet: str = "") -> None:
    ctx.report(rule, severity, title, path=where, snippet=safe_for_display(snippet, 300), category="host")


def check_known_artifact_paths(ctx: ScanContext) -> None:
    for artifact in ctx.config.iocs.host_artifacts:
        path = Path(artifact).expanduser()
        if path.exists() or path.is_symlink():
            report_host(ctx, "HOST-IOC-ARTIFACT", Severity.CRITICAL, "Known malware artifact present on this machine", str(path))


def check_scripts_in_editor_home_dirs(ctx: ScanContext) -> None:
    for directory in (Path(d).expanduser() for d in EDITOR_HOME_DIRS):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and EDITOR_HOME_SCRIPT.search(entry.name):
                report_host(ctx, "HOST-EDITOR-SCRIPT", Severity.HIGH, "Script file directly inside an editor config directory", str(entry))


def check_editor_extensions(ctx: ScanContext) -> None:
    vscode = ctx.config.section("vscode")
    trusted = set(vscode["trusted_publishers"])
    brand = ctx.regex("vscode.brand_words")
    for directory in (Path(d).expanduser() for d in EDITOR_EXTENSION_DIRS):
        if not directory.is_dir():
            continue
        for extension in directory.iterdir():
            if extension.is_dir():
                classify_installed_extension(ctx, extension, trusted, brand)


def classify_installed_extension(ctx: ScanContext, extension: Path, trusted: set[str], brand: re.Pattern) -> None:
    publisher = extension.name.split(".", 1)[0].lower()
    if publisher in trusted:
        return
    if brand.search(extension.name):
        report_host(ctx, "HOST-EXTENSION", Severity.HIGH, "Installed extension uses a brand name but is not from that vendor", str(extension))
    elif is_recent(extension):
        report_host(ctx, "HOST-EXTENSION", Severity.MEDIUM, f"Extension from an untrusted publisher installed in the last {RECENT_DAYS} days", str(extension))


def check_recent_autostart_entries(ctx: ScanContext) -> None:
    for directory in (Path(d).expanduser() for d in AUTOSTART_DIRS):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if is_recent(entry):
                report_host(ctx, "HOST-AUTOSTART", Severity.MEDIUM, f"Autostart entry created or changed in the last {RECENT_DAYS} days", str(entry))


def check_shell_profiles(ctx: ScanContext) -> None:
    for profile in (Path(p).expanduser() for p in SHELL_PROFILES):
        if not profile.is_file():
            continue
        for number, line in enumerate(profile.read_text(errors="replace").splitlines(), start=1):
            if SUSPICIOUS_COMMAND.search(line):
                report_host(ctx, "HOST-SHELL-PROFILE", Severity.HIGH, "Shell profile runs a download or hidden command", f"{profile}:{number}", line)


def read_command_output(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_crontab(ctx: ScanContext) -> None:
    for line in read_command_output(["crontab", "-l"]).splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            severity = Severity.HIGH if SUSPICIOUS_COMMAND.search(line) or re.search(r"\b(node|python|curl|wget)\b", line) else Severity.LOW
            report_host(ctx, "HOST-CRONTAB", severity, "Crontab entry", "crontab", line)


def check_running_processes(ctx: ScanContext) -> None:
    for line in read_command_output(["ps", "-eo", "pid,args"]).splitlines()[1:]:
        if SUSPICIOUS_PROCESS.search(line):
            report_host(ctx, "HOST-PROCESS", Severity.HIGH, "Running node/python process matching malware launch patterns", "processes", line.strip())
