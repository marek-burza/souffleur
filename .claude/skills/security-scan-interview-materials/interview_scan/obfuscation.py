import re
from collections import defaultdict
from pathlib import PurePosixPath

from .content_rules import TextOrigin, apply_pattern_rules
from .context import FileRecord, ScanContext
from .findings import Severity
from .textutil import LineIndex, make_snippet, path_matches_any, safe_for_display, shannon_entropy

INVISIBLE_CHARACTERS = re.compile("[​‌‎‏‪-‮⁠-⁤⁦-⁩﻿­᠎\U000e0000-\U000e007f]")
UNICODE_TAG_CHARACTERS = re.compile("[\U000e0000-\U000e007f]+")
STRING_LITERAL = re.compile(r"""(['"`])([^'"`\n]{%d,})\1""")
EXECUTION_TOKENS = re.compile(r"\b(eval|exec|execSync|require|import|Function|spawn|atob|__import__)\s*\(|\bchild_process\b|\bBuffer\.from\b|\bsubprocess\.")


def run_obfuscation_checks(ctx: ScanContext) -> None:
    for record in ctx.files:
        check_unicode_in_filename(ctx, record)
        if record.is_text:
            check_long_lines(ctx, record)
            check_high_entropy_literals(ctx, record)
            check_invisible_unicode(ctx, record)


def check_unicode_in_filename(ctx: ScanContext, record: FileRecord) -> None:
    if INVISIBLE_CHARACTERS.search(record.rel):
        ctx.report(
            "OB-FILENAME-UNICODE",
            Severity.HIGH,
            "File name contains invisible or bidirectional Unicode (disguises the real extension)",
            path=safe_for_display(record.rel),
            category="hiding",
        )


def check_long_lines(ctx: ScanContext, record: FileRecord) -> None:
    files = ctx.config.section("files")
    skip = ctx.is_lockfile(record.rel) or ctx.is_minified(record.rel) or path_matches_any(record.rel, files["long_line_skip"])
    if skip:
        return
    threshold = ctx.config.limit("long_line_chars")
    for number, line in enumerate(record.text.splitlines(), start=1):
        if len(line) > threshold:
            report_long_line(ctx, record, number, line)


def report_long_line(ctx: ScanContext, record: FileRecord, number: int, line: str) -> None:
    executes = EXECUTION_TOKENS.search(line)
    excerpt_start = max(0, executes.start() - 80) if executes else 0
    ctx.report(
        "OB-LONG-LINE",
        Severity.HIGH if executes else Severity.MEDIUM,
        f"Very long line ({len(line)} chars) in a non-minified file" + (" containing execution keywords" if executes else ""),
        path=record.rel,
        line=number,
        snippet=safe_for_display(line[excerpt_start:], 200),
        explain="Payloads are often appended to a normal file as one huge line so they scroll off-screen.",
        category="hiding",
    )


def check_high_entropy_literals(ctx: ScanContext, record: FileRecord) -> None:
    if ctx.is_lockfile(record.rel) or ctx.is_minified(record.rel):
        return
    min_len = ctx.config.limit("entropy_min_len")
    threshold = ctx.config.raw["limits"]["entropy_threshold"]
    index = None
    for match in re.finditer(STRING_LITERAL.pattern % min_len, record.text):
        entropy = shannon_entropy(match.group(2))
        if entropy > threshold:
            index = index or LineIndex(record.text)
            report_high_entropy_literal(ctx, record, index, match, entropy)


def report_high_entropy_literal(ctx: ScanContext, record: FileRecord, index: LineIndex, match: re.Match, entropy: float) -> None:
    ctx.report(
        "OB-HIGH-ENTROPY",
        Severity.MEDIUM,
        f"High-entropy string literal ({len(match.group(2))} chars, entropy {entropy:.2f})",
        path=record.rel,
        line=index.line_number(match.start()),
        snippet=make_snippet(record.text, index, match.start(), match.end()),
        explain="Encrypted or packed payloads look like random strings.",
        category="obfuscation",
    )


def check_invisible_unicode(ctx: ScanContext, record: FileRecord) -> None:
    text = record.text[1:] if record.text.startswith("﻿") else record.text
    matches = list(INVISIBLE_CHARACTERS.finditer(text))
    if not matches:
        return
    index = LineIndex(text)
    first = matches[0]
    ctx.report(
        "OB-INVISIBLE-UNICODE",
        Severity.HIGH,
        f"Invisible or bidirectional Unicode characters ({len(matches)} found)",
        path=record.rel,
        line=index.line_number(first.start()),
        snippet=make_snippet(text, index, first.start(), first.end()),
        explain="Invisible characters can hide code from human reviewers (Trojan Source) or smuggle instructions to AI agents.",
        category="hiding",
    )
    reveal_hidden_tag_text(ctx, record, text, index)


def reveal_hidden_tag_text(ctx: ScanContext, record: FileRecord, text: str, index: LineIndex) -> None:
    for match in UNICODE_TAG_CHARACTERS.finditer(text):
        hidden = "".join(chr(ord(ch) - 0xE0000) for ch in match.group(0))
        line = index.line_number(match.start())
        ctx.report(
            "OB-HIDDEN-TAG-TEXT",
            Severity.HIGH,
            "Text hidden with Unicode tag characters (invisible to humans, readable by AI models)",
            path=record.rel,
            line=line,
            snippet="hidden text: " + safe_for_display(hidden, 300),
            category="prompt-injection",
        )
        apply_pattern_rules(ctx, hidden, TextOrigin(record.rel, ["unicode-tags"], line))


def run_file_type_checks(ctx: ScanContext) -> None:
    for record in ctx.files:
        check_extension_against_magic(ctx, record)
        check_code_in_data_file(ctx, record)
        check_binary_or_script_file(ctx, record)
    check_dll_side_loading(ctx)


def check_extension_against_magic(ctx: ScanContext, record: FileRecord) -> None:
    expected = ctx.config.section("magic")["extensions"].get(record.ext)
    is_executable = record.magic in ("pe", "elf", "macho")
    if is_executable and record.ext not in executable_extensions(ctx):
        report_disguised_file(ctx, record, f"is a native executable ({record.magic})")
        return
    if not expected or any(record.head.startswith(bytes.fromhex(sig)) for sig in expected):
        return
    if record.is_text:
        report_disguised_file(ctx, record, "contains text instead of the expected binary format")
    else:
        report_disguised_file(ctx, record, "has file contents that don't match the extension", Severity.MEDIUM)


def executable_extensions(ctx: ScanContext) -> set[str]:
    binaries = ctx.config.section("binaries")
    return set(binaries["high"]) | set(binaries["medium"]) | {"", "bin", "o", "a", "class"}


def report_disguised_file(ctx: ScanContext, record: FileRecord, problem: str, base_severity: Severity = Severity.HIGH) -> None:
    reference = find_execution_reference(ctx, record)
    ctx.report(
        "OB-DISGUISED-FILE",
        Severity.CRITICAL if reference else base_severity,
        f".{record.ext} file {problem}" + (" and is executed by another file" if reference else ""),
        path=record.rel,
        snippet=reference or describe_file_start(record),
        explain="Payloads are disguised as fonts, images or dictionaries and launched with node/python from a task or script.",
        category="hiding",
    )


def describe_file_start(record: FileRecord) -> str:
    if record.is_text:
        return "starts with: " + safe_for_display(record.text[:160])
    return "starts with bytes: " + record.head[:16].hex(" ")


def check_code_in_data_file(ctx: ScanContext, record: FileRecord) -> None:
    files = ctx.config.section("files")
    if not record.is_text or record.ext not in files["data_extensions"] or ctx.is_lockfile(record.rel):
        return
    if record.name.startswith("requirements") or record.name in ("CMakeLists.txt", "robots.txt"):
        return
    hits = ctx.regex("files.code_indicator").findall(record.text)
    if len(hits) < files["code_indicator_min_hits"]:
        return
    reference = find_execution_reference(ctx, record)
    ctx.report(
        "OB-CODE-IN-DATA-FILE",
        Severity.CRITICAL if reference else Severity.MEDIUM,
        f"Data file (.{record.ext}) contains program code" + (" and is executed by another file" if reference else ""),
        path=record.rel,
        snippet=reference or describe_file_start(record),
        category="hiding",
    )


def find_execution_reference(ctx: ScanContext, target: FileRecord) -> str | None:
    interpreter = ctx.regex("commands.interpreter")
    loader = re.compile(r"\b(require|import|exec|execSync|spawn|fork|readFileSync|runpy|source|load)\b")
    for record in ctx.text_files():
        if record.rel == target.rel or target.name not in record.text:
            continue
        for number, line in enumerate(record.text.splitlines(), start=1):
            if target.name in line and (interpreter.search(line) or loader.search(line)):
                return f"referenced from {record.rel}:{number}: " + safe_for_display(line.strip(), 160)
    return None


def check_binary_or_script_file(ctx: ScanContext, record: FileRecord) -> None:
    binaries = ctx.config.section("binaries")
    if record.ext in binaries["high"]:
        report_binary(ctx, record, Severity.HIGH)
    elif record.ext in binaries["medium"]:
        report_binary(ctx, record, Severity.MEDIUM)
    elif record.magic in ("pe", "elf", "macho") and record.ext in ("", "bin", "o", "a"):
        report_binary(ctx, record, Severity.HIGH)


def report_binary(ctx: ScanContext, record: FileRecord, severity: Severity) -> None:
    kind = "Executable or installer" if severity >= Severity.HIGH else "Script"
    ctx.report(
        "BIN-FILE",
        severity,
        f"{kind} file shipped in the project (.{record.ext or record.magic})",
        path=record.rel,
        snippet=f"sha256 {record.sha256}, {record.size} bytes",
        explain="A coding exercise rarely needs bundled executables. Check what runs it before trusting it.",
        category="binary",
    )


def check_dll_side_loading(ctx: ScanContext) -> None:
    by_directory: dict[str, set[str]] = defaultdict(set)
    for record in ctx.files:
        by_directory[str(PurePosixPath(record.rel).parent)].add(record.ext)
    for directory, extensions in by_directory.items():
        if "exe" in extensions and "dll" in extensions:
            ctx.report(
                "BIN-SIDELOAD",
                Severity.HIGH,
                "Executable and DLL side by side (DLL side-loading layout)",
                path=directory + "/",
                explain="Operation Dream Job paired a signed viewer with a malicious DLL loaded from the same folder.",
                category="binary",
            )
