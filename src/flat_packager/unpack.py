"""Rebuild a repository tree from a flat text archive."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .archive import ARCHIVE_KIND, SUPPORTED_ARCHIVE_VERSIONS, read_json_lines


ArchiveRecord = tuple[int, dict[str, Any]]


@dataclass
class ChunkState:
    path: str
    expected_chunks: int
    expected_size: int
    expected_sha256: str
    seen_chunks: int = 0
    seen_size: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)


@dataclass
class ArchiveManifest:
    header: dict[str, Any]
    version: int
    records: list[ArchiveRecord]
    entries: int
    files: int
    dirs: int
    symlinks: int
    chunks: int
    bytes: int


@dataclass
class ActiveWrite:
    path: str
    expected_chunks: int
    seen_chunks: int
    handle: Any
    mode: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore a repository tree from a flat text archive."
    )
    parser.add_argument("archive", help="Flat text archive created by flat-pack.")
    parser.add_argument("output_dir", help="Directory to create or restore into.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing output directory.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Validate archive structure and hashes without writing files.",
    )
    parser.add_argument(
        "--no-symlinks",
        action="store_true",
        help="Restore symlinks as small text files containing their target path.",
    )
    return parser.parse_args()


def absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def safe_relative_parts(rel_path: str) -> tuple[str, ...]:
    if not isinstance(rel_path, str):
        raise ValueError(f"archive path must be a string: {rel_path!r}")
    if "\x00" in rel_path:
        raise ValueError(f"archive path contains NUL byte: {rel_path!r}")
    if "\\" in rel_path:
        raise ValueError(f"archive path contains unsupported backslash: {rel_path!r}")
    if rel_path in ("", ".") or rel_path.startswith("/"):
        raise ValueError(f"unsafe archive path: {rel_path}")

    parts = tuple(rel_path.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe archive path: {rel_path}")
    return parts


def safe_output_path(root: Path, rel_path: str) -> Path:
    return root.joinpath(*safe_relative_parts(rel_path))


def validate_header(line_number: int, payload: dict[str, Any]) -> int:
    if payload.get("type") != ARCHIVE_KIND:
        raise ValueError(f"line {line_number}: not a {ARCHIVE_KIND} archive")
    version = payload.get("version")
    if version not in SUPPORTED_ARCHIVE_VERSIONS:
        raise ValueError(f"line {line_number}: unsupported archive version {version}")
    return int(version)


def record_path(record: dict[str, Any]) -> str:
    path = record.get("path")
    if not isinstance(path, str):
        raise ValueError("record path must be a string")
    safe_relative_parts(path)
    return path


def validate_mode(record: dict[str, Any]) -> None:
    mode = record.get("mode")
    if mode is None:
        return
    if not isinstance(mode, int) or mode < 0 or mode > 0o7777:
        raise ValueError(f"{record.get('path')}: invalid mode {mode!r}")


def mode_from_record(record: dict[str, Any]) -> int | None:
    mode = record.get("mode")
    return mode if isinstance(mode, int) else None


def decode_base64(path: str, content: Any) -> bytes:
    if not isinstance(content, str):
        raise ValueError(f"{path}: content must be a base64 string")
    try:
        return base64.b64decode(content.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(f"{path}: invalid base64 content") from exc


def validate_file_record_v1(record: dict[str, Any]) -> int:
    path = record_path(record)
    validate_mode(record)
    if record.get("encoding") != "base64":
        raise ValueError(f"{path}: unsupported encoding {record.get('encoding')}")

    content = decode_base64(path, record.get("content"))
    expected_size = record.get("size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError(f"{path}: invalid size {expected_size!r}")
    if len(content) != expected_size:
        raise ValueError(f"{path}: size mismatch")

    digest = hashlib.sha256(content).hexdigest()
    if digest != record.get("sha256"):
        raise ValueError(f"{path}: sha256 mismatch")
    return expected_size


def validate_file_record_v2(record: dict[str, Any]) -> ChunkState | None:
    path = record_path(record)
    validate_mode(record)
    if record.get("encoding") != "base64-chunks":
        raise ValueError(f"{path}: unsupported encoding {record.get('encoding')}")

    expected_size = record.get("size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError(f"{path}: invalid size {expected_size!r}")
    expected_chunks = record.get("chunks")
    if not isinstance(expected_chunks, int) or expected_chunks < 0:
        raise ValueError(f"{path}: invalid chunks count {expected_chunks!r}")
    expected_sha256 = record.get("sha256")
    if not isinstance(expected_sha256, str) or expected_sha256 == "":
        raise ValueError(f"{path}: sha256 must be a non-empty string")

    if expected_chunks == 0:
        if expected_size != 0:
            raise ValueError(f"{path}: zero chunks with non-zero size")
        if hashlib.sha256(b"").hexdigest() != expected_sha256:
            raise ValueError(f"{path}: sha256 mismatch")
        return None

    return ChunkState(
        path=path,
        expected_chunks=expected_chunks,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )


def validate_chunk_record(record: dict[str, Any], state: ChunkState | None) -> ChunkState | None:
    if state is None:
        raise ValueError("chunk record is not attached to a file")

    path = record_path(record)
    if path != state.path:
        raise ValueError(f"{path}: chunk belongs to {path}, expected {state.path}")
    index = record.get("index")
    if index != state.seen_chunks:
        raise ValueError(f"{path}: chunk index mismatch, expected {state.seen_chunks}")

    chunk = decode_base64(path, record.get("content"))
    expected_size = record.get("size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError(f"{path}: invalid chunk size {expected_size!r}")
    if len(chunk) != expected_size:
        raise ValueError(f"{path}: chunk size mismatch")

    chunk_digest = record.get("sha256")
    if chunk_digest is not None and hashlib.sha256(chunk).hexdigest() != chunk_digest:
        raise ValueError(f"{path}: chunk sha256 mismatch")

    state.hasher.update(chunk)
    state.seen_chunks += 1
    state.seen_size += len(chunk)
    if state.seen_chunks < state.expected_chunks:
        return state

    if state.seen_size != state.expected_size:
        raise ValueError(f"{path}: size mismatch")
    if state.hasher.hexdigest() != state.expected_sha256:
        raise ValueError(f"{path}: sha256 mismatch")
    return None


def validate_dir_record(record: dict[str, Any]) -> None:
    record_path(record)
    validate_mode(record)


def validate_symlink_record(record: dict[str, Any]) -> None:
    path = record_path(record)
    validate_mode(record)
    target = record.get("target")
    if not isinstance(target, str) or target == "":
        raise ValueError(f"{path}: symlink target must be a non-empty string")
    if "\x00" in target:
        raise ValueError(f"{path}: symlink target contains NUL byte")


def logical_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "content"}


def validate_path_ancestry(path_types: dict[str, str]) -> None:
    for path in path_types:
        parts = safe_relative_parts(path)
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            ancestor_type = path_types.get(ancestor)
            if ancestor_type is None or ancestor_type == "dir":
                continue
            if ancestor_type == "symlink":
                raise ValueError(f"{path}: path is nested under archived symlink {ancestor}")
            raise ValueError(f"{path}: path is nested under archived file {ancestor}")


def validate_end_record(
    record: dict[str, Any],
    entries: int,
    files: int,
    dirs: int,
    symlinks: int,
    chunks: int,
    bytes_count: int,
) -> None:
    expected_entries = record.get("entries")
    expected_files = record.get("files")
    if not isinstance(expected_entries, int):
        raise ValueError("end record has invalid entries count")
    if not isinstance(expected_files, int):
        raise ValueError("end record has invalid files count")
    if expected_entries != entries:
        raise ValueError(f"entry count mismatch: expected {expected_entries}, got {entries}")
    if expected_files != files:
        raise ValueError(f"file count mismatch: expected {expected_files}, got {files}")

    optional_counts = {
        "dirs": dirs,
        "symlinks": symlinks,
        "chunks": chunks,
        "bytes": bytes_count,
    }
    for key, actual in optional_counts.items():
        expected = record.get(key)
        if expected is not None and expected != actual:
            raise ValueError(f"{key} count mismatch: expected {expected}, got {actual}")


def load_and_validate_archive(archive_path: Path) -> ArchiveManifest:
    records_iter = read_json_lines(archive_path)
    try:
        first_line, header = next(records_iter)
    except StopIteration as exc:
        raise ValueError("archive is empty") from exc

    version = validate_header(first_line, header)

    records: list[ArchiveRecord] = []
    seen_paths: set[str] = set()
    path_types: dict[str, str] = {}
    entries = 0
    files = 0
    dirs = 0
    symlinks = 0
    chunks = 0
    bytes_count = 0
    saw_end = False
    active_chunk_file: ChunkState | None = None

    for line_number, record in records_iter:
        if saw_end:
            raise ValueError(f"line {line_number}: archive has data after end record")

        record_type = record.get("type")
        try:
            if record_type in {"dir", "file", "symlink"}:
                if active_chunk_file is not None:
                    raise ValueError(f"{active_chunk_file.path}: incomplete chunk stream")
                path = record_path(record)
                if path in seen_paths:
                    raise ValueError(f"{path}: duplicate archive path")
                seen_paths.add(path)
                path_types[path] = str(record_type)

                if record_type == "dir":
                    validate_dir_record(record)
                    dirs += 1
                elif record_type == "file":
                    if version == 1:
                        bytes_count += validate_file_record_v1(record)
                    else:
                        active_chunk_file = validate_file_record_v2(record)
                        bytes_count += int(record["size"])
                    files += 1
                else:
                    validate_symlink_record(record)
                    symlinks += 1

                records.append((line_number, logical_record(record)))
                entries += 1
            elif record_type == "chunk":
                if version != 2:
                    raise ValueError("chunk records require archive version 2")
                active_chunk_file = validate_chunk_record(record, active_chunk_file)
                chunks += 1
            elif record_type == "end":
                if active_chunk_file is not None:
                    raise ValueError(f"{active_chunk_file.path}: incomplete chunk stream")
                validate_end_record(record, entries, files, dirs, symlinks, chunks, bytes_count)
                saw_end = True
            else:
                raise ValueError(f"unknown record type {record_type!r}")
        except KeyError as exc:
            raise ValueError(f"line {line_number}: missing required key {exc}") from exc
        except Exception as exc:
            raise type(exc)(f"line {line_number}: {exc}") from exc

    if not saw_end:
        raise ValueError("archive is missing end record")

    validate_path_ancestry(path_types)
    return ArchiveManifest(
        header=header,
        version=version,
        records=records,
        entries=entries,
        files=files,
        dirs=dirs,
        symlinks=symlinks,
        chunks=chunks,
        bytes=bytes_count,
    )


def write_dir(root: Path, record: dict[str, Any], dir_modes: list[tuple[Path, int]]) -> None:
    target = safe_output_path(root, record_path(record))
    target.mkdir(parents=True, exist_ok=True)
    mode = mode_from_record(record)
    if mode is not None:
        dir_modes.append((target, mode))


def write_file_v1(root: Path, record: dict[str, Any]) -> None:
    target = safe_output_path(root, record_path(record))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(decode_base64(record_path(record), record["content"]))
    mode = mode_from_record(record)
    if mode is not None:
        os.chmod(target, mode)


def start_file_v2(root: Path, record: dict[str, Any]) -> ActiveWrite | None:
    path = record_path(record)
    target = safe_output_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    chunks = int(record["chunks"])
    mode = mode_from_record(record)
    if chunks == 0:
        target.write_bytes(b"")
        if mode is not None:
            os.chmod(target, mode)
        return None
    return ActiveWrite(
        path=path,
        expected_chunks=chunks,
        seen_chunks=0,
        handle=target.open("wb"),
        mode=mode,
    )


def write_chunk(record: dict[str, Any], active: ActiveWrite | None) -> ActiveWrite | None:
    if active is None:
        raise ValueError("chunk record is not attached to a file")
    path = record_path(record)
    if path != active.path:
        raise ValueError(f"{path}: chunk belongs to {path}, expected {active.path}")
    if record.get("index") != active.seen_chunks:
        raise ValueError(f"{path}: chunk index mismatch, expected {active.seen_chunks}")
    active.handle.write(decode_base64(path, record["content"]))
    active.seen_chunks += 1
    if active.seen_chunks < active.expected_chunks:
        return active

    active.handle.close()
    if active.mode is not None:
        os.chmod(active.handle.name, active.mode)
    return None


def write_symlink(root: Path, record: dict[str, Any], no_symlinks: bool) -> None:
    target = safe_output_path(root, record_path(record))
    target.parent.mkdir(parents=True, exist_ok=True)
    if no_symlinks:
        target.write_text(str(record["target"]) + "\n", encoding="utf-8")
        return
    os.symlink(str(record["target"]), target)


def write_records_from_archive(
    archive_path: Path,
    root: Path,
    version: int,
    no_symlinks: bool,
) -> None:
    dir_modes: list[tuple[Path, int]] = []
    active_file: ActiveWrite | None = None

    try:
        records_iter = read_json_lines(archive_path)
        next(records_iter)
        for _line_number, record in records_iter:
            record_type = record.get("type")
            if record_type == "dir":
                write_dir(root, record, dir_modes)
            elif record_type == "symlink":
                write_symlink(root, record, no_symlinks)
            elif record_type == "file":
                if version == 1:
                    write_file_v1(root, record)
                else:
                    active_file = start_file_v2(root, record)
            elif record_type == "chunk":
                active_file = write_chunk(record, active_file)
            elif record_type == "end":
                break

        if active_file is not None:
            raise ValueError(f"{active_file.path}: incomplete chunk stream")
    finally:
        if active_file is not None:
            active_file.handle.close()

    for path, mode in sorted(dir_modes, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(path, mode)


def ensure_output_replaceable(output_dir: Path, overwrite: bool) -> None:
    if not path_exists(output_dir):
        return
    if output_dir.is_dir() and not output_dir.is_symlink():
        is_non_empty = any(output_dir.iterdir())
        if is_non_empty and not overwrite:
            raise FileExistsError(
                f"output directory is not empty; use --overwrite: {output_dir}"
            )
        return
    if not overwrite:
        raise FileExistsError(f"output path exists; use --overwrite: {output_dir}")


def unique_temp_path(parent: Path, prefix: str) -> Path:
    temp_path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    temp_path.rmdir()
    return temp_path


def replace_output_with_staging(staging_dir: Path, output_dir: Path, overwrite: bool) -> None:
    ensure_output_replaceable(output_dir, overwrite)

    backup_dir: Path | None = None
    if path_exists(output_dir):
        backup_dir = unique_temp_path(output_dir.parent, f".{output_dir.name}.backup-")
        output_dir.rename(backup_dir)

    try:
        staging_dir.rename(output_dir)
    except Exception:
        if backup_dir is not None and path_exists(backup_dir) and not path_exists(output_dir):
            backup_dir.rename(output_dir)
        raise
    else:
        if backup_dir is not None and path_exists(backup_dir):
            if backup_dir.is_dir() and not backup_dir.is_symlink():
                shutil.rmtree(backup_dir)
            else:
                backup_dir.unlink()


def restore_archive(
    archive_path: Path,
    output_dir: Path,
    overwrite: bool,
    verify_only: bool,
    no_symlinks: bool,
) -> tuple[int, int]:
    manifest = load_and_validate_archive(archive_path)
    if verify_only:
        return manifest.entries, manifest.files

    output_dir = absolute_path(output_dir)
    if output_dir.parent == output_dir:
        raise ValueError(f"refusing to restore directly to filesystem root: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_replaceable(output_dir, overwrite)

    staging_dir: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.restore-", dir=output_dir.parent)
    )
    try:
        write_records_from_archive(
            archive_path=archive_path,
            root=staging_dir,
            version=manifest.version,
            no_symlinks=no_symlinks,
        )
        replace_output_with_staging(staging_dir, output_dir, overwrite)
        staging_dir = None
    finally:
        if staging_dir is not None and path_exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)

    return manifest.entries, manifest.files


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive).expanduser().resolve()
    output_dir = absolute_path(Path(args.output_dir))

    try:
        entries, files = restore_archive(
            archive_path=archive_path,
            output_dir=output_dir,
            overwrite=args.overwrite,
            verify_only=args.verify_only,
            no_symlinks=args.no_symlinks,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    action = "verified" if args.verify_only else "restored"
    print(f"{action} {entries} entries ({files} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
