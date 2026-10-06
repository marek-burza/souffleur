import hashlib
import os
from pathlib import Path, PurePosixPath

from .context import FileRecord, ScanContext
from .findings import Severity

HEAD_BYTES = 1024
SKIPPED_GIT_INTERNALS = ("objects", "lfs", "logs")


def build_inventory(ctx: ScanContext) -> None:
    for disk_path in walk_without_following_links(ctx.root):
        rel = original_relative_path(disk_path, ctx)
        if disk_path.is_symlink():
            report_symlink(ctx, disk_path, rel)
            continue
        if is_skipped_git_internal(rel):
            continue
        ctx.files.append(read_file_record(disk_path, rel, ctx))
    ctx.summary["file_count"] = len(ctx.files)
    ctx.summary["total_bytes"] = sum(record.size for record in ctx.files)


def walk_without_following_links(root: Path):
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirnames:
            if (base / name).is_symlink():
                yield base / name
        for name in filenames:
            yield base / name


def original_relative_path(disk_path: Path, ctx: ScanContext) -> str:
    suffix = ctx.config.section("quarantine")["suffix"]
    parts = disk_path.relative_to(ctx.root).parts
    return "/".join(part.removesuffix(suffix) for part in parts)


def is_skipped_git_internal(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if ".git" not in parts:
        return False
    after_git = parts[parts.index(".git") + 1:]
    return bool(after_git) and after_git[0] in SKIPPED_GIT_INTERNALS


def report_symlink(ctx: ScanContext, disk_path: Path, rel: str) -> None:
    target = os.readlink(disk_path)
    resolved = (disk_path.parent / target).resolve()
    escapes_root = not resolved.is_relative_to(ctx.root.resolve())
    ctx.report(
        "FS-SYMLINK",
        Severity.HIGH if escapes_root else Severity.MEDIUM,
        "Symlink pointing outside the project" if escapes_root else "Symlink inside the project",
        path=rel,
        snippet=f"-> {target}",
        explain="Symlinks can make an editor or build tool read or write files outside the folder. Not followed by the scanner.",
        category="filesystem",
    )


def read_file_record(disk_path: Path, rel: str, ctx: ScanContext) -> FileRecord:
    max_bytes = ctx.config.limit("max_file_bytes")
    size = disk_path.stat().st_size
    sha256, head, content = hash_file_and_read_prefix(disk_path, max_bytes)
    if size > max_bytes:
        report_oversized_file(ctx, rel, size, max_bytes)
    return FileRecord(
        rel=rel,
        disk=disk_path,
        size=size,
        sha256=sha256,
        head=head,
        text=decode_as_text(content),
        truncated=size > max_bytes,
        magic=detect_magic(head, ctx),
    )


def hash_file_and_read_prefix(disk_path: Path, max_bytes: int) -> tuple[str, bytes, bytes]:
    digest = hashlib.sha256()
    kept = bytearray()
    with disk_path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            if len(kept) < max_bytes:
                kept.extend(chunk[: max_bytes - len(kept)])
    return digest.hexdigest(), bytes(kept[:HEAD_BYTES]), bytes(kept)


def report_oversized_file(ctx: ScanContext, rel: str, size: int, max_bytes: int) -> None:
    ctx.report(
        "FS-OVERSIZED",
        Severity.MEDIUM,
        "File larger than the scan limit; only the beginning was scanned",
        path=rel,
        snippet=f"{size} bytes, scanned first {max_bytes}",
        category="coverage",
    )


def decode_as_text(content: bytes) -> str | None:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16", errors="replace")
    if b"\x00" in content[:8192]:
        return None
    return content.decode("utf-8", errors="replace")


def detect_magic(head: bytes, ctx: ScanContext) -> str | None:
    signatures = ctx.config.section("magic")
    for kind, prefixes in signatures.items():
        if kind == "extensions":
            continue
        for prefix in [prefixes] if isinstance(prefixes, str) else prefixes:
            if head.startswith(bytes.fromhex(prefix)):
                return kind
    if head[257:262] == b"ustar":
        return "tar"
    return None
