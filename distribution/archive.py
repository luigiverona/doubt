"""Build and inspect the deterministic self-contained release archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from . import runtime

ROOT = runtime.ROOT
ARCHIVE_ROOT = runtime.BUNDLE_NAME
ARCHIVE_NAME = f"{ARCHIVE_ROOT}.tar.gz"
MEMBERS_SOURCE = ROOT / "release/members.txt"
DIGEST_SOURCE = ROOT / "release/SHA256"
MAX_MEMBERS = 256
MAX_FILE_SIZE = 32 * 1024 * 1024
MAX_TOTAL_SIZE = 64 * 1024 * 1024


class ArchiveError(ValueError):
    """Unsafe, incomplete, or nondeterministic archive input."""


def manifest(bundle: Path) -> tuple[str, ...]:
    runtime.validate(bundle)
    entries = [f"d {ARCHIVE_ROOT}"]
    for path in sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix()):
        kind = "d" if path.is_dir() else "f"
        entries.append(f"{kind} {ARCHIVE_ROOT}/{path.relative_to(bundle).as_posix()}")
    return tuple(entries)


def expected_manifest() -> tuple[str, ...]:
    if not MEMBERS_SOURCE.is_file():
        raise ArchiveError("release/members.txt is missing")
    values = tuple(line for line in MEMBERS_SOURCE.read_text(encoding="utf-8").splitlines() if line)
    if not values or len(values) != len(set(values)):
        raise ArchiveError("release member manifest is empty or duplicated")
    return values


def build(bundle: Path, output: Path) -> None:
    actual = manifest(bundle)
    if actual != expected_manifest():
        raise ArchiveError("runtime tree differs from release/members.txt")
    output = output.resolve()
    if not output.parent.is_dir():
        raise ArchiveError("archive output parent does not exist")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0) as compressed:
                _write_tar(bundle, compressed, actual)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary_name, output)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _write_tar(bundle: Path, stream: gzip.GzipFile, entries: tuple[str, ...]) -> None:
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            kind, name = entry.split(" ", 1)
            relative = PurePosixPath(name).relative_to(ARCHIVE_ROOT)
            source = bundle.joinpath(*relative.parts) if relative.parts else bundle
            if kind == "d":
                archive.addfile(_metadata(name, directory=True))
            else:
                content = source.read_bytes()
                archive.addfile(_metadata(name, directory=False, size=len(content)), io.BytesIO(content))


def _metadata(name: str, *, directory: bool, size: int = 0) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.mode = 0o755 if directory or name == f"{ARCHIVE_ROOT}/doubt" else 0o644
    item.uid = 0
    item.gid = 0
    item.uname = "root"
    item.gname = "root"
    item.mtime = 0
    item.size = size
    return item


def inspect(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ArchiveError("archive is missing or unsafe")
    seen: list[str] = []
    total = 0
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ArchiveError("archive contains too many members")
        for member in members:
            name = _normalized(member.name)
            kind = "d" if member.isdir() else "f" if member.isfile() else ""
            if not kind or member.issym() or member.islnk():
                raise ArchiveError(f"unsupported archive member type: {name}")
            if member.type == tarfile.GNUTYPE_SPARSE or member.mode & 0o6000:
                raise ArchiveError(f"unsafe archive metadata: {name}")
            expected_mode = 0o755 if kind == "d" or name == f"{ARCHIVE_ROOT}/doubt" else 0o644
            if member.mode != expected_mode or member.uid != 0 or member.gid != 0 or member.mtime != 0:
                raise ArchiveError(f"nondeterministic archive metadata: {name}")
            if member.isfile():
                if member.size > MAX_FILE_SIZE:
                    raise ArchiveError(f"archive member is too large: {name}")
                total += member.size
                if total > MAX_TOTAL_SIZE:
                    raise ArchiveError("archive expands beyond the size limit")
            seen.append(f"{kind} {name}")
    if tuple(seen) != expected_manifest():
        raise ArchiveError("archive members differ from release/members.txt")


def _normalized(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise ArchiveError(f"unsafe archive member: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveError(f"unsafe archive member: {name!r}")
    if PurePosixPath(name).as_posix() != name:
        raise ArchiveError(f"non-normalized archive member: {name!r}")
    return name


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def expected_digest() -> str:
    content = DIGEST_SOURCE.read_text(encoding="ascii") if DIGEST_SOURCE.is_file() else ""
    value = content.split(maxsplit=1)[0] if content.split() else ""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ArchiveError("release/SHA256 is missing or invalid")
    return value


def deterministic(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    roots: list[Path] = []
    archives: list[Path] = []
    try:
        for name in ("one", "two"):
            bundle = directory / name / ARCHIVE_ROOT
            work = directory / name / "work"
            runtime.build(bundle, work)
            roots.append(bundle)
            archive = directory / f"{name}-{ARCHIVE_NAME}"
            build(bundle, archive)
            inspect(archive)
            archives.append(archive)
        if archives[0].read_bytes() != archives[1].read_bytes():
            raise ArchiveError("independent clean runtime builds are not byte-identical")
        final = directory / ARCHIVE_NAME
        os.replace(archives[0], final)
        checksum = digest(final)
        return final, checksum
    finally:
        for root in (directory / "one", directory / "two"):
            if root.exists():
                shutil.rmtree(root)
        for archive in archives:
            archive.unlink(missing_ok=True)


def reproducible(directory: Path) -> tuple[Path, str]:
    archive, checksum = deterministic(directory)
    if checksum != expected_digest():
        archive.unlink(missing_ok=True)
        raise ArchiveError(f"release archive SHA-256 differs: {checksum}")
    return archive, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "inspect", "digest", "manifest", "reproducible"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--work", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if args.work is None:
                raise ArchiveError("build requires --work")
            runtime.build(args.work / ARCHIVE_ROOT, args.work / "pyinstaller")
            build(args.work / ARCHIVE_ROOT, args.path)
        elif args.command == "inspect":
            inspect(args.path)
        elif args.command == "digest":
            print(digest(args.path))
        elif args.command == "manifest":
            print("\n".join(manifest(args.path)))
        else:
            archive, checksum = reproducible(args.path)
            print(f"archive: {archive}")
            print(f"sha256: {checksum}")
    except (ArchiveError, runtime.RuntimeError) as error:
        parser.exit(1, f"archive error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
