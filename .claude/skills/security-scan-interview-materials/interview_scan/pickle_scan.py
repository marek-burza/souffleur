import pickletools

from .context import FileRecord, ScanContext
from .findings import Severity

STRING_OPCODES = {"SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8", "SHORT_BINSTRING", "BINSTRING", "STRING"}
PICKLE_PROTOCOL_MARKER = 0x80


def analyze_serialized_model_files(ctx: ScanContext) -> None:
    extensions = set(ctx.config.section("python")["pickle_extensions"])
    for record in ctx.files:
        if record.is_text:
            continue
        looks_pickled = record.head[:1] == bytes([PICKLE_PROTOCOL_MARKER])
        if record.ext in extensions or looks_pickled:
            analyze_pickle_file(ctx, record, looks_pickled)


def analyze_pickle_file(ctx: ScanContext, record: FileRecord, looks_pickled: bool) -> None:
    if not looks_pickled:
        report_serialized_file(ctx, record, Severity.MEDIUM, "Serialized model/data file (loading it can execute code)", "")
        return
    imports = list_pickle_imports(record.disk.read_bytes()[: ctx.config.limit("max_file_bytes")])
    dangerous = [name for name in imports if is_dangerous_pickle_import(ctx, name)]
    unknown = [name for name in imports if not is_known_safe_pickle_import(ctx, name) and name not in dangerous]
    if dangerous:
        report_serialized_file(ctx, record, Severity.CRITICAL, "Pickle file imports code-execution functions when loaded", ", ".join(dangerous))
    elif unknown:
        report_serialized_file(ctx, record, Severity.MEDIUM, "Pickle file imports unexpected objects when loaded", ", ".join(unknown[:20]))
    else:
        report_serialized_file(ctx, record, Severity.LOW, "Pickle file (only common data-science imports found)", ", ".join(imports[:10]))


def list_pickle_imports(data: bytes) -> list[str]:
    imports: list[str] = []
    recent_strings: list[str] = []
    try:
        for opcode, argument, _ in pickletools.genops(data):
            if opcode.name in STRING_OPCODES:
                recent_strings = (recent_strings + [str(argument)])[-2:]
            elif opcode.name in ("GLOBAL", "INST"):
                imports.append(str(argument).replace(" ", "."))
            elif opcode.name == "STACK_GLOBAL" and len(recent_strings) == 2:
                imports.append(".".join(recent_strings))
    except Exception:
        imports.append("(pickle stream could not be fully parsed)")
    return list(dict.fromkeys(imports))


def is_dangerous_pickle_import(ctx: ScanContext, qualified_name: str) -> bool:
    if is_known_safe_pickle_import(ctx, qualified_name):
        return False
    module = qualified_name.rsplit(".", 1)[0]
    dangerous = ctx.config.section("python")["pickle_dangerous_modules"]
    return any(module == m or module.startswith(m + ".") for m in dangerous)


def is_known_safe_pickle_import(ctx: ScanContext, qualified_name: str) -> bool:
    safe = ctx.config.section("python")["pickle_safe_module_prefixes"]
    return any(qualified_name == p or qualified_name.startswith(p + ".") for p in safe)


def report_serialized_file(ctx: ScanContext, record: FileRecord, severity: Severity, title: str, imports: str) -> None:
    ctx.report(
        "PY-PICKLE",
        severity,
        title,
        path=record.rel,
        snippet=f"imports: {imports}" if imports else f"{record.size} bytes",
        explain="pickle, joblib and torch.load run code embedded in the file. Never load model files from an untrusted repo.",
        category="deserialization",
    )
