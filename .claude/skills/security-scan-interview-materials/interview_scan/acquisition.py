import gzip
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .context import ScanContext
from .findings import Severity

NESTED_SUFFIX = ".__extracted__"
REMOTE_GIT_URL = re.compile(r"^(https?://|ssh://|git://|git@)")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
SAFE_GIT_ENV = {
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


class ArchiveLimitExceeded(Exception):
    pass


@dataclass
class AcquiredTarget:
    source: str
    kind: str
    root: Path
    sha256: str | None


def acquire_target(ctx: ScanContext) -> AcquiredTarget:
    target = ctx.options.target
    destination = ctx.options.work_dir / "extracted"
    destination.mkdir(parents=True, mode=0o700)
    acquired = acquire_into(ctx, target, destination)
    extract_nested_archives(ctx, destination, depth=1)
    remove_execute_permissions(destination)
    quarantine_agent_config_files(ctx, destination)
    ctx.root = destination
    return acquired


def acquire_into(ctx: ScanContext, target: str, destination: Path) -> AcquiredTarget:
    if REMOTE_GIT_URL.match(target):
        clone_repository_safely(target, destination)
        return AcquiredTarget(target, "git", destination, None)
    source = Path(target).expanduser().resolve()
    if source.is_dir():
        copy_directory_without_following_links(source, destination)
        return AcquiredTarget(str(source), "directory", destination, None)
    if not source.is_file():
        raise FileNotFoundError(f"Target not found: {target}")
    digest = sha256_of_file(source)
    kind = extract_archive_or_copy_file(ctx, source, destination, rel_prefix="")
    return AcquiredTarget(str(source), kind, destination, digest)


def clone_repository_safely(url: str, destination: Path) -> None:
    command = [
        "git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never",
        "clone", "--no-recurse-submodules", "--", url, str(destination),
    ]
    subprocess.run(command, check=True, env={**os.environ, **SAFE_GIT_ENV}, cwd=destination.parent)


def copy_directory_without_following_links(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def extract_archive_or_copy_file(ctx: ScanContext, source: Path, destination: Path, rel_prefix: str) -> str:
    if zipfile.is_zipfile(source):
        extract_zip_safely(ctx, source, destination, rel_prefix)
        return "zip"
    if is_tar_archive(source):
        extract_tar_safely(ctx, source, destination, rel_prefix)
        return "tar"
    if source.read_bytes()[:2] == b"\x1f\x8b":
        decompress_single_gzip(ctx, source, destination)
        return "gzip"
    shutil.copy2(source, destination / source.name)
    return "file"


def is_tar_archive(path: Path) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except OSError:
        return False


def safe_member_path(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if member.is_absolute() or WINDOWS_DRIVE.match(normalized) or ".." in member.parts:
        return None
    if not member.parts:
        return None
    return member


def report_unsafe_member(ctx: ScanContext, rel_prefix: str, name: str, reason: str, severity: Severity) -> None:
    ctx.report(
        "ARCHIVE-ENTRY",
        severity,
        f"Archive entry {reason}; not extracted",
        path=rel_prefix + name,
        snippet=name,
        explain="Path traversal, absolute paths and links let an archive write or point outside the folder it is extracted into.",
        category="archive",
    )


def check_archive_totals(ctx: ScanContext, entry_count: int, total_size: int, rel_prefix: str) -> None:
    if entry_count > ctx.config.limit("max_entries") or total_size > ctx.config.limit("max_total_bytes"):
        ctx.report(
            "ARCHIVE-BOMB",
            Severity.HIGH,
            "Archive exceeds size or entry limits (possible archive bomb); extraction stopped",
            path=rel_prefix or "(archive)",
            snippet=f"{entry_count} entries, {total_size} bytes uncompressed",
            category="archive",
        )
        raise ArchiveLimitExceeded


def is_suspicious_compression_ratio(ctx: ScanContext, uncompressed: int, compressed: int) -> bool:
    return compressed > 0 and uncompressed > 10_000_000 and uncompressed / compressed > ctx.config.limit("max_compression_ratio")


def extract_zip_safely(ctx: ScanContext, source: Path, destination: Path, rel_prefix: str) -> None:
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        try:
            check_archive_totals(ctx, len(members), sum(m.file_size for m in members), rel_prefix)
        except ArchiveLimitExceeded:
            return
        for member in members:
            extract_one_zip_member(ctx, archive, member, destination, rel_prefix)


def extract_one_zip_member(ctx, archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path, rel_prefix: str) -> None:
    member_path = safe_member_path(member.filename)
    if member_path is None:
        report_unsafe_member(ctx, rel_prefix, member.filename, "has an absolute or parent-directory path", Severity.HIGH)
        return
    if stat.S_ISLNK(member.external_attr >> 16):
        link_target = archive.read(member).decode("utf-8", errors="replace")
        report_unsafe_member(ctx, rel_prefix, f"{member.filename} -> {link_target}", "is a symlink", Severity.HIGH)
        return
    if member.is_dir():
        make_private_directory(destination / member_path)
        return
    if is_suspicious_compression_ratio(ctx, member.file_size, member.compress_size):
        report_unsafe_member(ctx, rel_prefix, member.filename, "has an extreme compression ratio", Severity.HIGH)
        return
    if member.flag_bits & 0x1 and not ctx.options.archive_password:
        report_encrypted_member(ctx, rel_prefix + member.filename)
        return
    password = ctx.options.archive_password.encode() if ctx.options.archive_password else None
    try:
        with archive.open(member, pwd=password) as stream:
            write_file_without_execute_bits(stream, destination / member_path)
    except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
        report_unsafe_member(ctx, rel_prefix, member.filename, f"could not be read ({error})", Severity.MEDIUM)


def report_encrypted_member(ctx: ScanContext, rel: str) -> None:
    ctx.report(
        "ARCHIVE-ENCRYPTED",
        Severity.HIGH,
        "Password-protected archive entry could not be inspected (rerun with --password)",
        path=rel,
        explain="Encrypted archives hide content from scanners. Dream Job used encrypted ZIPs.",
        category="archive",
    )


def extract_tar_safely(ctx: ScanContext, source: Path, destination: Path, rel_prefix: str) -> None:
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        try:
            check_archive_totals(ctx, len(members), sum(m.size for m in members), rel_prefix)
        except ArchiveLimitExceeded:
            return
        for member in members:
            extract_one_tar_member(ctx, archive, member, destination, rel_prefix)


def extract_one_tar_member(ctx, archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path, rel_prefix: str) -> None:
    member_path = safe_member_path(member.name)
    if member_path is None:
        report_unsafe_member(ctx, rel_prefix, member.name, "has an absolute or parent-directory path", Severity.HIGH)
        return
    if member.issym() or member.islnk():
        report_unsafe_member(ctx, rel_prefix, f"{member.name} -> {member.linkname}", "is a link", Severity.HIGH)
        return
    if member.isdir():
        make_private_directory(destination / member_path)
        return
    if not member.isfile():
        report_unsafe_member(ctx, rel_prefix, member.name, "is a device or special file", Severity.HIGH)
        return
    stream = archive.extractfile(member)
    if stream is not None:
        write_file_without_execute_bits(stream, destination / member_path)


def decompress_single_gzip(ctx: ScanContext, source: Path, destination: Path) -> None:
    output_name = source.name.removesuffix(".gz") or "decompressed"
    limit = ctx.config.limit("max_total_bytes")
    with gzip.open(source) as stream:
        write_file_without_execute_bits(stream, destination / output_name, limit)


def make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_file_without_execute_bits(stream, target: Path, byte_limit: int | None = None) -> None:
    make_private_directory(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        return
    written = 0
    with os.fdopen(descriptor, "wb") as output:
        while chunk := stream.read(1 << 20):
            written += len(chunk)
            if byte_limit is not None and written > byte_limit:
                break
            output.write(chunk)


def extract_nested_archives(ctx: ScanContext, directory: Path, depth: int) -> None:
    if depth > ctx.config.limit("nested_archive_depth"):
        return
    for archive_path in find_nested_archives(directory):
        extract_one_nested_archive(ctx, directory, archive_path, depth)


def find_nested_archives(directory: Path) -> list[Path]:
    found = []
    for base, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git" and not d.endswith(NESTED_SUFFIX)]
        for name in filenames:
            path = Path(base) / name
            if not path.is_symlink() and looks_like_archive(path):
                found.append(path)
    return found


def looks_like_archive(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(4)
    return head.startswith((b"PK\x03\x04", b"\x1f\x8b")) or is_tar_archive(path)


def extract_one_nested_archive(ctx: ScanContext, root: Path, archive_path: Path, depth: int) -> None:
    rel = archive_path.relative_to(root).as_posix()
    nested_destination = archive_path.with_name(archive_path.name + NESTED_SUFFIX)
    make_private_directory(nested_destination)
    ctx.report(
        "ARCHIVE-NESTED",
        Severity.LOW,
        "Nested archive extracted and scanned",
        path=rel,
        snippet=f"contents under {rel}{NESTED_SUFFIX}/",
        category="archive",
    )
    try:
        extract_archive_or_copy_file(ctx, archive_path, nested_destination, rel_prefix=rel + "!/")
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as error:
        ctx.report("ARCHIVE-UNREADABLE", Severity.MEDIUM, f"Nested archive could not be read: {error}", path=rel, category="archive")
        return
    extract_nested_archives(ctx, nested_destination, depth + 1)


def remove_execute_permissions(directory: Path) -> None:
    for base, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in dirnames:
            path = Path(base) / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in filenames:
            path = Path(base) / name
            if not path.is_symlink():
                path.chmod(0o600)


def quarantine_agent_config_files(ctx: ScanContext, directory: Path) -> None:
    quarantine = ctx.config.section("quarantine")
    names = set(quarantine["names"])
    for base, dirnames, filenames in os.walk(directory, topdown=False, followlinks=False):
        for name in [*filenames, *dirnames]:
            if name in names:
                path = Path(base) / name
                path.rename(path.with_name(name + quarantine["suffix"]))
