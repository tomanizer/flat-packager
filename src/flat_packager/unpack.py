"""Rebuild a repository tree from a flat text archive."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .archive import ARCHIVE_KIND, ARCHIVE_VERSION


ArchiveRecord = tuple[int, dict[str, Any]]


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


def read_json_lines(archive_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with archive_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"line {line_number}: archive record must be an object")
            yield line_number, payload


def validate_header(line_number: int, payload: dict[str, Any]) -> None:
    if payload.get("type") != ARCHIVE_KIND:
        raise ValueError(f"line {line_number}: not a {ARCHIVE_KIND} archive")
    if payload.get("version") != ARCHIVE_VERSION:
        raise ValueError(
            f"line {line_number}: unsupported archive version {payload.get('version')}"
        )


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


def validate_file_record(record: dict[str, Any]) -> None:
    path = record_path(record)
    validate_mode(record)
    if record.get("encoding") != "base64":
        raise ValueError(f"{path}: unsupported encoding {record.get('encoding')}")
    content_field = record.get("content")
    if not isinstance(content_field, str):
        raise ValueError(f"{path}: content must be a base64 string")
    try:
        content = base64.b64decode(content_field.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(f"{path}: invalid base64 content") from exc

    expected_size = record.get("size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError(f"{path}: invalid size {expected_size!r}")
    if len(content) != expected_size:
        raise ValueError(f"{path}: size mismatch")

    digest = hashlib.sha256(content).hexdigest()
    if digest != record.get("sha256"):
        raise ValueError(f"{path}: sha256 mismatch")


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


def validate_no_symlink_ancestors(
    records: list[ArchiveRecord],
    symlink_paths: set[tuple[str, ...]],
) -> None:
    for _line_number, record in records:
        path = record_path(record)
        parts = safe_relative_parts(path)
        for index in range(1, len(parts)):
            ancestor = parts[:index]
            if ancestor in symlink_paths:
                ancestor_path = "/".join(ancestor)
                raise ValueError(f"{path}: path is nested under archived symlink {ancestor_path}")


def load_and_validate_archive(archive_path: Path) -> tuple[list[ArchiveRecord], int, int]:
    records_iter = read_json_lines(archive_path)
    try:
        first_line, header = next(records_iter)
    except StopIteration as exc:
        raise ValueError("archive is empty") from exc

    validate_header(first_line, header)

    records: list[ArchiveRecord] = []
    seen_paths: set[str] = set()
    symlink_paths: set[tuple[str, ...]] = set()
    entries = 0
    files = 0
    saw_end = False

    for line_number, record in records_iter:
        if saw_end:
            raise ValueError(f"line {line_number}: archive has data after end record")

        record_type = record.get("type")
        try:
            if record_type in {"dir", "file", "symlink"}:
                path = record_path(record)
                if path in seen_paths:
                    raise ValueError(f"{path}: duplicate archive path")
                seen_paths.add(path)

                if record_type == "dir":
                    validate_dir_record(record)
                elif record_type == "file":
                    validate_file_record(record)
                    files += 1
                else:
                    validate_symlink_record(record)
                    symlink_paths.add(safe_relative_parts(path))

                records.append((line_number, record))
                entries += 1
            elif record_type == "end":
                expected_entries = record.get("entries")
                expected_files = record.get("files")
                if not isinstance(expected_entries, int):
                    raise ValueError("end record has invalid entries count")
                if not isinstance(expected_files, int):
                    raise ValueError("end record has invalid files count")
                if expected_entries != entries:
                    raise ValueError(
                        f"entry count mismatch: expected {expected_entries}, got {entries}"
                    )
                if expected_files != files:
                    raise ValueError(
                        f"file count mismatch: expected {expected_files}, got {files}"
                    )
                saw_end = True
            else:
                raise ValueError(f"unknown record type {record_type!r}")
        except KeyError as exc:
            raise ValueError(f"line {line_number}: missing required key {exc}") from exc
        except Exception as exc:
            raise type(exc)(f"line {line_number}: {exc}") from exc

    if not saw_end:
        raise ValueError("archive is missing end record")

    validate_no_symlink_ancestors(records, symlink_paths)
    return records, entries, files


def mode_from_record(record: dict[str, Any]) -> int | None:
    mode = record.get("mode")
    return mode if isinstance(mode, int) else None


def write_dir(root: Path, record: dict[str, Any], dir_modes: list[tuple[Path, int]]) -> None:
    target = safe_output_path(root, record_path(record))
    target.mkdir(parents=True, exist_ok=True)
    mode = mode_from_record(record)
    if mode is not None:
        dir_modes.append((target, mode))


def write_file(root: Path, record: dict[str, Any]) -> None:
    target = safe_output_path(root, record_path(record))
    target.parent.mkdir(parents=True, exist_ok=True)
    content = base64.b64decode(str(record["content"]).encode("ascii"), validate=True)
    target.write_bytes(content)
    mode = mode_from_record(record)
    if mode is not None:
        os.chmod(target, mode)


def write_symlink(root: Path, record: dict[str, Any], no_symlinks: bool) -> None:
    target = safe_output_path(root, record_path(record))
    target.parent.mkdir(parents=True, exist_ok=True)
    if no_symlinks:
        target.write_text(str(record["target"]) + "\n", encoding="utf-8")
        return
    os.symlink(str(record["target"]), target)


def write_records(root: Path, records: list[ArchiveRecord], no_symlinks: bool) -> None:
    dir_modes: list[tuple[Path, int]] = []
    for _line_number, record in records:
        record_type = record["type"]
        if record_type == "dir":
            write_dir(root, record, dir_modes)
        elif record_type == "file":
            write_file(root, record)
        elif record_type == "symlink":
            write_symlink(root, record, no_symlinks)
        else:
            raise ValueError(f"unknown record type {record_type!r}")

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
    records, entries, files = load_and_validate_archive(archive_path)
    if verify_only:
        return entries, files

    output_dir = absolute_path(output_dir)
    if output_dir.parent == output_dir:
        raise ValueError(f"refusing to restore directly to filesystem root: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_replaceable(output_dir, overwrite)

    staging_dir: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.restore-", dir=output_dir.parent)
    )
    try:
        write_records(staging_dir, records, no_symlinks)
        replace_output_with_staging(staging_dir, output_dir, overwrite)
        staging_dir = None
    finally:
        if staging_dir is not None and path_exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)

    return entries, files


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
