from .config import SurfaceRule
from .context import FileRecord, ScanContext
from .textutil import path_matches


def report_execution_surfaces(ctx: ScanContext) -> None:
    for rule in ctx.config.surfaces:
        matches = [record for record in ctx.files if surface_rule_matches(rule, record)]
        for record in matches[:1] if rule.once else matches:
            ctx.report(
                "SURFACE",
                rule.severity,
                rule.title,
                path=record.rel,
                snippet=f"{len(matches)} files" if rule.once and len(matches) > 1 else "",
                explain="This file can run code automatically or when a common command is used. Review it before running anything.",
                category="execution-surface",
            )


def surface_rule_matches(rule: SurfaceRule, record: FileRecord) -> bool:
    if not path_matches(record.rel, rule.path):
        return False
    return not (rule.exclude and path_matches(record.rel, rule.exclude))
