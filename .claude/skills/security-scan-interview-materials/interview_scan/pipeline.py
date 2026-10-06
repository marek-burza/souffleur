import traceback

from .acquisition import AcquiredTarget, acquire_target
from .agent_configs import analyze_agent_configs
from .content_rules import scan_all_files_with_content_rules
from .context import ScanContext
from .decoding import analyze_encoded_content
from .editor_configs import analyze_editor_and_container_configs
from .findings import Severity
from .git_provenance import analyze_git_provenance
from .inventory import build_inventory
from .network import scan_all_files_for_network_indicators
from .npm_manifests import analyze_npm_projects
from .obfuscation import run_file_type_checks, run_obfuscation_checks
from .pickle_scan import analyze_serialized_model_files
from .python_manifests import analyze_python_projects
from .registry_lookup import run_registry_lookups
from .surfaces import report_execution_surfaces

ANALYSIS_PHASES = (
    ("execution surfaces", report_execution_surfaces),
    ("editor and devcontainer configs", analyze_editor_and_container_configs),
    ("AI-agent configs", analyze_agent_configs),
    ("content rules", scan_all_files_with_content_rules),
    ("network indicators", scan_all_files_for_network_indicators),
    ("encoded content", analyze_encoded_content),
    ("obfuscation", run_obfuscation_checks),
    ("file types and binaries", run_file_type_checks),
    ("serialized models", analyze_serialized_model_files),
    ("npm projects", analyze_npm_projects),
    ("python projects", analyze_python_projects),
    ("git provenance", analyze_git_provenance),
)


def run_scan(ctx: ScanContext) -> AcquiredTarget:
    acquired = acquire_target(ctx)
    build_inventory(ctx)
    check_known_file_hashes(ctx)
    for phase_name, phase in ANALYSIS_PHASES:
        run_phase_and_record_errors(ctx, phase_name, phase)
    if ctx.options.registry_lookup:
        run_phase_and_record_errors(ctx, "registry lookup", run_registry_lookups)
    return acquired


def run_phase_and_record_errors(ctx: ScanContext, phase_name: str, phase) -> None:
    try:
        phase(ctx)
    except Exception as error:
        ctx.report(
            "SCANNER-ERROR",
            Severity.MEDIUM,
            f"Scanner phase '{phase_name}' failed; coverage is incomplete",
            snippet=f"{type(error).__name__}: {error}",
            explain=traceback.format_exc(limit=3),
            category="coverage",
        )


def check_known_file_hashes(ctx: ScanContext) -> None:
    known = ctx.config.iocs.hashes
    for record in ctx.files:
        label = known.get(record.sha256)
        if label:
            ctx.report("IOC-HASH", Severity.CRITICAL, f"File hash matches known malware: {label}", path=record.rel,
                       snippet=f"sha256 {record.sha256}", category="ioc")
