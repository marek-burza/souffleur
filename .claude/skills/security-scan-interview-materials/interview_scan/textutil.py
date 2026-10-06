import bisect
import fnmatch
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import PurePosixPath

SNIPPET_LIMIT = 240
URL_IN_TEXT = re.compile(r"(?i)\b(hxxps?|https?|wss?|ftp)://([^\s/'\"`<>]+)")
TRAILING_COMMA = re.compile(r",(\s*[}\]])")


class LineIndex:
    def __init__(self, text: str) -> None:
        self._text = text
        self._starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_number(self, offset: int) -> int:
        return bisect.bisect_right(self._starts, offset)

    def line_bounds(self, line_number: int) -> tuple[int, int]:
        start = self._starts[line_number - 1]
        end = self._text.find("\n", start)
        return start, len(self._text) if end < 0 else end


def make_snippet(text: str, index: LineIndex, start: int, end: int) -> str:
    line_start, line_end = index.line_bounds(index.line_number(start))
    window_start = max(line_start, start - 100)
    window_end = min(line_end, max(end, start) + 100)
    prefix = "..." if window_start > line_start else ""
    suffix = "..." if window_end < line_end else ""
    return safe_for_display(prefix + text[window_start:window_end] + suffix)


def safe_for_display(value: str, limit: int = SNIPPET_LIMIT) -> str:
    visible = escape_invisible_characters(value)
    defanged = defang_urls(visible)
    collapsed = re.sub(r"\s{2,}", lambda m: " " if len(m.group()) < 8 else f" <{len(m.group())} whitespace> ", defanged)
    if len(collapsed) > limit:
        return collapsed[:limit] + "...(truncated)"
    return collapsed


def escape_invisible_characters(value: str) -> str:
    return "".join(_escape_char(ch) for ch in value)


def _escape_char(ch: str) -> str:
    if ch in "\t ":
        return ch
    if ch == "\n":
        return " | "
    if ch.isprintable() and unicodedata.category(ch) not in ("Cf", "Co", "Cs"):
        return ch
    return f"\\u{{{ord(ch):04x}}}"


def defang_urls(value: str) -> str:
    def defang_one(match: re.Match) -> str:
        scheme = re.sub(r"(?i)^http", "hxxp", match.group(1))
        return f"{scheme}://{match.group(2).replace('.', '[.]')}"

    return URL_IN_TEXT.sub(defang_one, value)


def refang(value: str) -> str:
    plain = value.replace("[.]", ".").replace("(.)", ".").replace("[dot]", ".")
    return re.sub(r"(?i)\bhxxp", "http", plain)


def path_matches(rel: str, pattern: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatchcase(PurePosixPath(rel).name, pattern)
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, "*/" + pattern)


def path_matches_any(rel: str, patterns: list[str]) -> bool:
    return any(path_matches(rel, pattern) for pattern in patterns)


def strip_jsonc_comments(text: str) -> str:
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            step = 2 if ch == "\\" else 1
            out.append(text[i:i + step])
            in_string = ch != '"' or step == 2
            i += step
        elif ch == '"':
            out.append(ch)
            in_string = True
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_jsonc(text: str):
    without_comments = strip_jsonc_comments(text.lstrip("﻿"))
    return json.loads(TRAILING_COMMA.sub(r"\1", without_comments))


def try_parse_jsonc(text: str):
    try:
        return parse_jsonc(text)
    except (ValueError, RecursionError):
        return None


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def first_line_containing(text: str, needle: str) -> int | None:
    offset = text.find(needle)
    if offset < 0:
        return None
    return text.count("\n", 0, offset) + 1


def flatten_json(value, prefix: str = "") -> list[tuple[str, object]]:
    if isinstance(value, dict):
        pairs = []
        for key, child in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            pairs.append((child_key, child))
            pairs.extend(flatten_json(child, child_key))
        return pairs
    if isinstance(value, list):
        return [pair for i, child in enumerate(value) for pair in flatten_json(child, f"{prefix}[{i}]")]
    return []
