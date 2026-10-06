import base64
import binascii
import bz2
import gzip
import lzma
import re
import zlib
from dataclasses import dataclass

from .content_rules import TextOrigin, apply_combo_rules, apply_ioc_string_rules, apply_pattern_rules
from .context import FileRecord, ScanContext
from .findings import Severity
from .network import URL_PATTERN, detect_wallet_extension_ids, scan_text_for_network_indicators
from .textutil import LineIndex, path_matches_any, safe_for_display

HEX_CANDIDATE = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-fA-F]{2}){20,}(?![0-9A-Za-z])")
HEX_ESCAPE_CANDIDATE = re.compile(r"(?:\\x[0-9a-fA-F]{2}){10,}")
CHARCODE_CANDIDATE = re.compile(r"(?<![\d.])(?:\d{2,3}\s*,\s*){15,}\d{2,3}(?![\d.])")
ENV_ASSIGNMENT = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*["']?([^"'\s#]+)""", re.MULTILINE)
SECRET_LIKE_KEY = re.compile(r"(?i)(KEY|TOKEN|SECRET|API|URL|URI|ENDPOINT|HOST|AUTH|PASS|CONFIG|SERVER)")
COMPRESSION_SIGNATURES = (
    ("zlib", (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda"), zlib.decompress),
    ("gzip", (b"\x1f\x8b",), gzip.decompress),
    ("bz2", (b"BZh",), bz2.decompress),
    ("lzma", (b"\xfd7zXZ\x00", b"\x5d\x00\x00"), lzma.decompress),
)


@dataclass
class DecodedBlob:
    offset: int
    chain: list[str]
    text: str


def analyze_encoded_content(ctx: ScanContext) -> None:
    for record in ctx.text_files():
        if ctx.is_lockfile(record.rel):
            continue
        index = LineIndex(record.text)
        for blob in find_decoded_blobs(ctx, record.text, chain=[], depth=1):
            analyze_decoded_blob(ctx, record, blob, index.line_number(blob.offset))
        if path_matches_any(record.rel, ctx.config.section("files")["env_files"]):
            check_env_values_that_decode_to_urls(ctx, record)


def find_decoded_blobs(ctx: ScanContext, text: str, chain: list[str], depth: int) -> list[DecodedBlob]:
    blobs: list[DecodedBlob] = []
    for offset, kind, literal in iterate_encoded_candidates(ctx, text):
        decoded = decode_candidate_to_text(ctx, kind, literal)
        if decoded is None:
            continue
        steps, decoded_text = decoded
        blob = DecodedBlob(offset, chain + steps, decoded_text)
        blobs.append(blob)
        if depth < ctx.config.limit("max_decode_depth"):
            blobs.extend(rebase_inner_blobs(find_decoded_blobs(ctx, decoded_text, blob.chain, depth + 1), offset))
    return blobs


def rebase_inner_blobs(inner_blobs: list[DecodedBlob], outer_offset: int) -> list[DecodedBlob]:
    return [DecodedBlob(outer_offset, inner.chain, inner.text) for inner in inner_blobs]


def iterate_encoded_candidates(ctx: ScanContext, text: str):
    base64_candidate = re.compile(r"[A-Za-z0-9+/_-]{%d,}={0,2}" % ctx.config.limit("base64_min_len"))
    finders = (
        ("base64", base64_candidate),
        ("hex", HEX_CANDIDATE),
        ("hex-escapes", HEX_ESCAPE_CANDIDATE),
        ("char-codes", CHARCODE_CANDIDATE),
    )
    budget = ctx.config.limit("max_decode_candidates_per_file")
    for kind, regex in finders:
        for match in regex.finditer(text):
            if budget <= 0:
                return
            budget -= 1
            yield match.start(), kind, match.group(0)


def decode_candidate_to_text(ctx: ScanContext, kind: str, literal: str) -> tuple[list[str], str] | None:
    attempts = [([kind], literal)]
    if kind == "base64":
        attempts.append((["reverse", kind], literal[::-1]))
    for steps, candidate in attempts:
        raw = decode_literal(kind, candidate)
        if raw is None or len(raw) > ctx.config.limit("max_decoded_bytes"):
            continue
        raw, compression_steps = unwrap_compression(raw, ctx.config.limit("max_decoded_bytes"))
        text = printable_text_or_none(raw)
        if text is not None:
            return steps + compression_steps, text
    return None


def decode_literal(kind: str, literal: str) -> bytes | None:
    match kind:
        case "base64":
            return decode_base64(literal)
        case "hex":
            return bytes.fromhex(literal)
        case "hex-escapes":
            return bytes.fromhex(literal.replace("\\x", ""))
        case "char-codes":
            return decode_char_codes(literal)
    return None


def decode_base64(literal: str) -> bytes | None:
    if looks_like_plain_identifier(literal):
        return None
    stripped = literal.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        if "-" in padded or "_" in padded:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def looks_like_plain_identifier(literal: str) -> bool:
    letters_only = literal.replace("_", "").replace("-", "")
    return letters_only.isalpha() and (letters_only.islower() or letters_only.isupper())


def decode_char_codes(literal: str) -> bytes | None:
    numbers = [int(n) for n in re.findall(r"\d+", literal)]
    if any(n > 255 for n in numbers):
        return None
    return bytes(numbers)


def unwrap_compression(raw: bytes, size_limit: int) -> tuple[bytes, list[str]]:
    steps: list[str] = []
    for _ in range(3):
        decompressed = try_decompress(raw, size_limit)
        if decompressed is None:
            break
        name, raw = decompressed
        steps.append(name)
    return raw, steps


def try_decompress(raw: bytes, size_limit: int) -> tuple[str, bytes] | None:
    for name, signatures, decompress in COMPRESSION_SIGNATURES:
        if raw.startswith(signatures):
            try:
                result = decompress(raw)
            except (zlib.error, OSError, EOFError, ValueError, lzma.LZMAError):
                continue
            if len(result) <= size_limit:
                return name, result
    return None


def printable_text_or_none(raw: bytes) -> str | None:
    if len(raw) < 8:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return text if printable / len(text) >= 0.95 else None


def analyze_decoded_blob(ctx: ScanContext, record: FileRecord, blob: DecodedBlob, line: int) -> None:
    origin = TextOrigin(record.rel, blob.chain, line)
    apply_pattern_rules(ctx, blob.text, origin)
    apply_combo_rules(ctx, blob.text, origin)
    apply_ioc_string_rules(ctx, blob.text, origin)
    detect_wallet_extension_ids(ctx, blob.text, origin)
    network_severity = scan_text_for_network_indicators(ctx, blob.text, origin)
    report_decoded_payload(ctx, record, blob, line, network_severity)


def describe_decoded_content(ctx: ScanContext, text: str) -> str | None:
    if URL_PATTERN.search(text):
        return "a URL"
    if ctx.regex("commands.dangerous").search(text):
        return "a shell command"
    if len(ctx.regex("files.code_indicator").findall(text)) >= 2:
        return "source code"
    return None


def report_decoded_payload(ctx: ScanContext, record: FileRecord, blob: DecodedBlob, line: int, network_severity: Severity | None) -> None:
    content_kind = describe_decoded_content(ctx, blob.text)
    layers = len(blob.chain)
    if layers >= 3:
        report_deep_encoding(ctx, record, blob, line)
    if content_kind is None:
        return
    escalated = network_severity is not None and network_severity >= Severity.HIGH
    ctx.report(
        "OB-ENCODED-PAYLOAD",
        Severity.CRITICAL if escalated else Severity.HIGH,
        f"Encoded literal decodes to {content_kind}",
        path=record.rel,
        line=line,
        snippet="decoded: " + safe_for_display(blob.text, 200),
        explain="Encoding URLs or code inside string literals hides them from review. Seen in Contagious Interview config files and loaders.",
        category="obfuscation",
        decode_chain=blob.chain,
    )


def report_deep_encoding(ctx: ScanContext, record: FileRecord, blob: DecodedBlob, line: int) -> None:
    ctx.report(
        "OB-NESTED-ENCODING",
        Severity.CRITICAL,
        f"Data encoded in {len(blob.chain)} nested layers",
        path=record.rel,
        line=line,
        snippet="innermost: " + safe_for_display(blob.text, 160),
        explain="Legitimate code almost never nests encodings three deep. InvisibleFerret used dozens of layers.",
        category="obfuscation",
        decode_chain=blob.chain,
    )


def check_env_values_that_decode_to_urls(ctx: ScanContext, record: FileRecord) -> None:
    index = LineIndex(record.text)
    for match in ENV_ASSIGNMENT.finditer(record.text):
        key, value = match.group(1), match.group(2)
        if len(value) < ctx.config.limit("env_value_min_len") or not SECRET_LIKE_KEY.search(key):
            continue
        decoded = decode_candidate_to_text(ctx, "base64", value)
        if decoded and URL_PATTERN.search(decoded[1]):
            report_env_value_hiding_url(ctx, record, index.line_number(match.start()), key, decoded)


def report_env_value_hiding_url(ctx: ScanContext, record: FileRecord, line: int, key: str, decoded: tuple[list[str], str]) -> None:
    chain, text = decoded
    origin = TextOrigin(record.rel, chain, line)
    network_severity = scan_text_for_network_indicators(ctx, text, origin)
    escalated = network_severity is not None and network_severity >= Severity.HIGH
    ctx.report(
        "OB-ENV-URL",
        Severity.CRITICAL if escalated else Severity.HIGH,
        f"Config value {key} looks like a key but decodes to a URL",
        path=record.rel,
        line=line,
        snippet=f"{key} -> " + safe_for_display(text, 200),
        explain="NVISO documented .config.env values named like API keys that decode to JSON-storage URLs hosting the loader.",
        category="obfuscation",
        decode_chain=chain,
    )
