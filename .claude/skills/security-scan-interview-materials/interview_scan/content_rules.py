from dataclasses import dataclass

from .config import ComboRule, PatternRule
from .context import ScanContext
from .textutil import LineIndex, make_snippet, path_matches_any


@dataclass
class TextOrigin:
    rel: str
    decode_chain: list[str]
    line_override: int | None = None

    @property
    def is_decoded(self) -> bool:
        return bool(self.decode_chain)


def scan_all_files_with_content_rules(ctx: ScanContext) -> None:
    for record in ctx.text_files():
        origin = TextOrigin(record.rel, [])
        apply_pattern_rules(ctx, record.text, origin)
        apply_combo_rules(ctx, record.text, origin)
        apply_ioc_string_rules(ctx, record.text, origin)


def pattern_applies(ctx: ScanContext, rule_files: list[str], rule_exclude: list[str], origin: TextOrigin) -> bool:
    if origin.is_decoded:
        return not rule_files
    if rule_files and not path_matches_any(origin.rel, rule_files):
        return False
    if rule_exclude and path_matches_any(origin.rel, rule_exclude):
        return False
    return not ctx.is_lockfile(origin.rel)


def apply_pattern_rules(ctx: ScanContext, text: str, origin: TextOrigin) -> None:
    index = LineIndex(text)
    for rule in ctx.config.patterns:
        if pattern_applies(ctx, rule.files, rule.exclude, origin):
            report_pattern_matches(ctx, rule, text, index, origin)


def report_pattern_matches(ctx: ScanContext, rule: PatternRule, text: str, index: LineIndex, origin: TextOrigin) -> None:
    matches = collect_matches(rule, text, ctx.config.limit("max_matches_per_rule_per_file"))
    if len(matches) < rule.min_count:
        return
    for match in matches if rule.min_count == 1 else matches[:1]:
        report_match(ctx, rule.id, rule.severity, rule.title, rule.category, rule.explain, text, index, match, origin)


def collect_matches(rule: PatternRule, text: str, cap: int) -> list:
    matches = []
    for match in rule.regex.finditer(text):
        matches.append(match)
        if len(matches) >= cap:
            break
    return matches


def report_match(ctx, rule_id, severity, title, category, explain, text, index, match, origin: TextOrigin) -> None:
    ctx.report(
        rule_id,
        severity,
        title if not origin.is_decoded else f"{title} (inside decoded data)",
        path=origin.rel,
        line=origin.line_override or index.line_number(match.start()),
        snippet=make_snippet(text, index, match.start(), match.end()),
        explain=explain,
        category=category,
        decode_chain=origin.decode_chain,
    )


def apply_combo_rules(ctx: ScanContext, text: str, origin: TextOrigin) -> None:
    index = None
    for rule in ctx.config.combos:
        if not pattern_applies(ctx, rule.files, [], origin):
            continue
        first_match = first_match_if_all_required_present(rule, text)
        if first_match is not None:
            index = index or LineIndex(text)
            report_match(ctx, rule.id, rule.severity, rule.title, rule.category, "", text, index, first_match, origin)


def first_match_if_all_required_present(rule: ComboRule, text: str):
    matches = [regex.search(text) for regex in rule.required]
    if all(matches):
        return matches[0]
    return None


def apply_ioc_string_rules(ctx: ScanContext, text: str, origin: TextOrigin) -> None:
    if ctx.is_lockfile(origin.rel):
        return
    index = None
    for ioc in ctx.config.iocs.strings:
        match = ioc.regex.search(text)
        if match:
            index = index or LineIndex(text)
            report_match(ctx, "IOC-STRING", ioc.severity, f"Known malware string: {ioc.label}", "ioc", "", text, index, match, origin)
