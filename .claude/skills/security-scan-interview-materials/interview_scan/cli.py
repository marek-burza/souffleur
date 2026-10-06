import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from .config import load_config
from .context import ScanContext, ScanOptions
from .host_check import run_host_check
from .pipeline import run_scan
from .report import exit_code_for, write_reports

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORK_ROOT = Path("~/.cache/security-scan-interview-materials").expanduser()


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scan.py",
        description="Static, read-only scan of interview materials (zip, tarball, directory or git URL). Never executes target code.",
    )
    parser.add_argument("target", nargs="?", help="archive, directory, or git URL to inspect")
    parser.add_argument("--out", type=Path, help="work directory for the extracted copy and reports (default: ~/.cache/security-scan-interview-materials/<name>-<time>)")
    parser.add_argument("--rules", type=Path, default=SKILL_DIR / "rules.toml")
    parser.add_argument("--iocs", type=Path, default=SKILL_DIR / "iocs.toml")
    parser.add_argument("--registry-lookup", action="store_true", help="query npm/PyPI metadata (read-only) for declared dependencies")
    parser.add_argument("--password", help="password for an encrypted zip")
    parser.add_argument("--host-check", action="store_true", help="read-only check of this machine for known infection artifacts")
    arguments = parser.parse_args(argv)
    if not arguments.target and not arguments.host_check:
        parser.error("give a target, or --host-check")
    return arguments


def default_work_dir(target: str) -> Path:
    name = re.sub(r"[^\w.-]+", "_", Path(target.rstrip("/")).name)[:60] or "target"
    return DEFAULT_WORK_ROOT / f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}"


def ensure_work_dir_outside_target(work_dir: Path, target: str) -> None:
    target_path = Path(target).expanduser().resolve()
    if target_path.is_dir() and work_dir.resolve().is_relative_to(target_path):
        raise SystemExit("Refusing to write the work directory inside the target.")


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    config = load_config(arguments.rules, arguments.iocs)
    label = arguments.target or "host-check"
    work_dir = (arguments.out or default_work_dir(label)).expanduser().resolve()
    ensure_work_dir_outside_target(work_dir, label)
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = ScanOptions(target=label, work_dir=work_dir, registry_lookup=arguments.registry_lookup, archive_password=arguments.password)
    ctx = ScanContext(config=config, options=options)
    try:
        acquired = run_scan(ctx) if arguments.target else None
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if arguments.host_check:
        run_host_check(ctx)
    text_path, json_path, text = write_reports(ctx, acquired)
    print(text)
    print(f"Report: {text_path}\nJSON:   {json_path}")
    return exit_code_for(ctx.findings.highest_severity())
