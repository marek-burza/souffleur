import datetime as dt
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .acquisition import NESTED_SUFFIX, SAFE_GIT_ENV
from .context import ScanContext
from .findings import Severity
from .textutil import safe_for_display

RECORD_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
LOG_FORMAT = RECORD_SEPARATOR + FIELD_SEPARATOR.join(["%H", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%s"])
CONFIG_SECTION = re.compile(r"^\s*\[\s*([^\s\]\"]+)")
CONFIG_VERSION_LINES = re.compile(r"(?im)^\s*(repositoryformatversion|objectformat|refstorage)\s*=.*$")
PACKAGE_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
REMOTE_URL = re.compile(r"(?im)^\s*url\s*=\s*(\S+)")


@dataclass
class Commit:
    sha: str
    author: str
    author_email: str
    authored: dt.datetime
    committer: str
    committer_email: str
    committed: dt.datetime
    subject: str
    files: list[str] = field(default_factory=list)


def analyze_git_provenance(ctx: ScanContext) -> None:
    for git_dir in find_git_directories(ctx.root):
        rel = git_dir.relative_to(ctx.root).as_posix()
        config_text = read_text_if_exists(git_dir / "config")
        check_git_config_for_command_execution(ctx, rel, config_text)
        commits = read_commit_history_safely(ctx, git_dir, config_text)
        if commits is not None:
            evaluate_commit_history(ctx, rel, commits)
    check_project_names_against_iocs(ctx)


def find_git_directories(root: Path) -> list[Path]:
    found = []
    for base, dirnames, _ in os.walk(root, followlinks=False):
        if ".git" in dirnames:
            found.append(Path(base) / ".git")
        dirnames[:] = [d for d in dirnames if d != ".git" and not d.endswith(NESTED_SUFFIX)]
    return found


def read_text_if_exists(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() and not path.is_symlink() else ""


def check_git_config_for_command_execution(ctx: ScanContext, rel: str, config_text: str) -> None:
    dangerous_key = ctx.regex("git.dangerous_config")
    dangerous_section = ctx.regex("git.dangerous_sections")
    section = ""
    for number, line in enumerate(config_text.splitlines(), start=1):
        header = CONFIG_SECTION.match(line)
        if header:
            section = line
            continue
        in_alias = section.strip().lower().startswith("[alias")
        if dangerous_section.match(section) and (dangerous_key.match(line) or in_alias):
            report_git_config_command(ctx, rel, number, section, line)


def report_git_config_command(ctx: ScanContext, rel: str, number: int, section: str, line: str) -> None:
    ctx.report(
        "GIT-CONFIG-EXEC",
        Severity.CRITICAL,
        "Shipped .git/config makes git run a command (e.g. fsmonitor runs on every git status)",
        path=f"{rel}/config",
        line=number,
        snippet=safe_for_display(f"{section.strip()} {line.strip()}"),
        explain="A repo delivered as an archive can include .git/config. Git, and editors that call git, would execute this.",
        category="autorun",
    )


def read_commit_history_safely(ctx: ScanContext, git_dir: Path, config_text: str) -> list[Commit] | None:
    sanitized = make_sanitized_git_copy(ctx, git_dir, config_text)
    command = [
        "git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
        f"--git-dir={sanitized}", "log", "--all", "--no-color", f"--format={LOG_FORMAT}", "--name-only",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=120,
                                env={**os.environ, **SAFE_GIT_ENV}, cwd=ctx.options.work_dir)
    except (OSError, subprocess.TimeoutExpired) as error:
        ctx.report("GIT-UNREADABLE", Severity.LOW, f"Git history could not be read: {error}", path=git_dir.name, category="provenance")
        return None
    if result.returncode != 0:
        ctx.report("GIT-UNREADABLE", Severity.LOW, "Git history could not be read", snippet=safe_for_display(result.stderr), category="provenance")
        return None
    return parse_git_log(result.stdout)


def make_sanitized_git_copy(ctx: ScanContext, git_dir: Path, config_text: str) -> Path:
    copy_name = git_dir.parent.relative_to(ctx.root).as_posix().replace("/", "__") or "root"
    destination = ctx.options.work_dir / "git-sanitized" / copy_name
    shutil.copytree(git_dir, destination, symlinks=True, ignore=shutil.ignore_patterns("hooks", "config"), dirs_exist_ok=True)
    (destination / "config").write_text(build_minimal_git_config(config_text))
    return destination


def is_format_version_line(line: str) -> bool:
    return line.lower().startswith("repositoryformatversion")


def build_minimal_git_config(original_config: str) -> str:
    kept = [match.group(0).strip() for match in CONFIG_VERSION_LINES.finditer(original_config)]
    format_version = next((line for line in kept if is_format_version_line(line)), "repositoryformatversion = 0")
    extension_lines = [line for line in kept if not is_format_version_line(line)]
    config = f"[core]\n\t{format_version}\n\tbare = true\n"
    if extension_lines:
        config += "[extensions]\n" + "".join(f"\t{line}\n" for line in extension_lines)
    return config


def parse_git_log(output: str) -> list[Commit]:
    commits = []
    for record in output.split(RECORD_SEPARATOR)[1:]:
        header, _, file_block = record.partition("\n")
        fields = header.split(FIELD_SEPARATOR)
        if len(fields) < 8:
            continue
        files = [line for line in file_block.splitlines() if line.strip()]
        commits.append(Commit(
            sha=fields[0], author=fields[1], author_email=fields[2], authored=dt.datetime.fromisoformat(fields[3]),
            committer=fields[4], committer_email=fields[5], committed=dt.datetime.fromisoformat(fields[6]),
            subject=fields[7], files=files,
        ))
    return commits


def evaluate_commit_history(ctx: ScanContext, rel: str, commits: list[Commit]) -> None:
    ctx.summary.setdefault("git", []).append(summarize_history(rel, commits))
    if not commits:
        return
    check_commit_count(ctx, rel, commits)
    check_commit_dates(ctx, rel, commits)
    check_free_mail_authors(ctx, rel, commits)
    check_latest_commit_only_touches_config(ctx, rel, commits[0])


def summarize_history(rel: str, commits: list[Commit]) -> dict:
    summary = {"git_dir": rel, "commits": len(commits), "authors": sorted({f"{c.author} <{c.author_email}>" for c in commits})[:20]}
    if commits:
        summary["first_commit"] = min(c.authored for c in commits).isoformat()
        summary["last_commit"] = max(c.committed for c in commits).isoformat()
    return summary


def check_commit_count(ctx: ScanContext, rel: str, commits: list[Commit]) -> None:
    if len(commits) <= 3:
        file_total = sum(len(c.files) for c in commits)
        ctx.report("GIT-FEW-COMMITS", Severity.MEDIUM, f"Only {len(commits)} commit(s) ({file_total} file changes)", path=rel,
                   explain="Throwaway assignment repos are often a single bulk commit copied from elsewhere.", category="provenance")


def check_commit_dates(ctx: ScanContext, rel: str, commits: list[Commit]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    mismatched = [c for c in commits if abs(c.committed - c.authored) > dt.timedelta(days=1)]
    future = [c for c in commits if c.authored > now + dt.timedelta(days=1) or c.committed > now + dt.timedelta(days=1)]
    if mismatched:
        example = mismatched[0]
        ctx.report("GIT-DATE-MISMATCH", Severity.MEDIUM, f"{len(mismatched)} commit(s) with author and commit dates more than a day apart",
                   path=rel, snippet=f"{example.sha[:10]} authored {example.authored.isoformat()} committed {example.committed.isoformat()}",
                   explain="Backdated or rewritten history is a sign of fabricated credibility.", category="provenance")
    if future:
        ctx.report("GIT-FUTURE-DATE", Severity.MEDIUM, f"{len(future)} commit(s) dated in the future", path=rel, category="provenance")


def check_free_mail_authors(ctx: ScanContext, rel: str, commits: list[Commit]) -> None:
    freemail = set(ctx.config.section("git")["freemail_domains"])
    emails = {e.lower() for c in commits for e in (c.author_email, c.committer_email)}
    personal = sorted(e for e in emails if e.rsplit("@", 1)[-1] in freemail)
    if personal:
        ctx.report("GIT-FREEMAIL", Severity.LOW, "Commits authored from personal or free-mail addresses", path=rel,
                   snippet=safe_for_display(", ".join(personal[:10])), category="provenance")


def check_latest_commit_only_touches_config(ctx: ScanContext, rel: str, latest: Commit) -> None:
    config_only = ctx.regex("git.config_only_paths")
    if not latest.files or not all(config_only.match(path) for path in latest.files):
        return
    touches_editor = any(path.startswith(".vscode/") for path in latest.files)
    ctx.report(
        "GIT-LATE-CONFIG",
        Severity.HIGH if touches_editor else Severity.MEDIUM,
        "Most recent commit only changes editor/config files",
        path=rel,
        snippet=safe_for_display(f"{latest.sha[:10]} '{latest.subject}': " + ", ".join(latest.files[:10])),
        explain="Commit-tampering tools in the Void Dokkaebi campaign add a malicious .vscode/ or config change as the last commit.",
        category="provenance",
    )


def check_project_names_against_iocs(ctx: ScanContext) -> None:
    names = collect_project_names(ctx)
    iocs = ctx.config.iocs
    theme = ctx.regex("git.theme_keywords")
    for name in sorted(names):
        if name.lower() in iocs.repo_names:
            ctx.report("IOC-REPO-NAME", Severity.HIGH, f"Project name '{name}' matches a known malicious assignment repo", category="ioc")
        elif theme.search(name):
            ctx.report("CTX-THEME", Severity.LOW, f"Project name '{name}' matches common lure themes", category="provenance")
    check_fake_company_mentions(ctx)


def collect_project_names(ctx: ScanContext) -> set[str]:
    target_name = PurePosixPath(ctx.options.target.rstrip("/")).name
    names = {re.sub(r"(\.git|\.zip|\.tar\.gz|\.tgz)$", "", target_name)}
    names.update(PurePosixPath(r.rel).parts[0] for r in ctx.files if "/" in r.rel)
    for record in ctx.text_files():
        if record.name == "package.json" and not record.is_inside("node_modules"):
            names.update(PACKAGE_NAME.findall(record.text)[:1])
        if record.rel.endswith(".git/config"):
            names.update(PurePosixPath(url).name.removesuffix(".git") for url in REMOTE_URL.findall(record.text))
    return {n for n in names if n and not n.startswith(".")}


def check_fake_company_mentions(ctx: ScanContext) -> None:
    for company in ctx.config.iocs.fake_companies:
        for record in ctx.text_files():
            if company.lower() in record.text.lower():
                ctx.report("IOC-COMPANY", Severity.HIGH, f"Mentions '{company}', a known fake company front", path=record.rel, category="ioc")
                break
