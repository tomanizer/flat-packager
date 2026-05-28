"""Rebuild a repository tree from a flat text archive."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

from .archive import ARCHIVE_KIND, ARCHIVE_VERSION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore a repository tree from a flat text archive."
    )
    parser.add_argument("archive", help="Flat text archive created by flat-pack.")
    parser.add_argument("output_dir", help="Directory to create or restore into.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting existing files/symlinks in output_dir.",
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


def safe_output_path(root: Path, rel_path: str) -> Path:
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts or rel_path in ("", "."):
        raise ValueError(f"unsafe archive path: {rel_path}")
    return root / rel


def read_json_lines(archive_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with archive_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield line_number, json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON: {exc}") from exc


def validate_header(line_number: int, payload: dict[str, Any]) -> None:
    if payload.get("type") != ARCHIVE_KIND:
        raise ValueError(f"line {line_number}: not a {ARCHIVE_KIND} archive")
    if payload.get("version") != ARCHIVE_VERSION:
        raise ValueError(
            f"line {line_number}: unsupported archive version {payload.get('version')}"
        )


def ensure_can_write(target: Path, overwrite: bool) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if not overwrite:
        raise FileExistsError(f"refusing to overwrite existing path: {target}")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def restore_dir(root: Path, record: dict[str, Any], overwrite: bool, verify_only: bool) -> None:
    target = safe_output_path(root, str(record["path"]))
    if verify_only:
        return
    if target.exists() and not target.is_dir():
        ensure_can_write(target, overwrite)
    target.mkdir(parents=True, exist_ok=True)
    mode = record.get("mode")
    if isinstance(mode, int):
        os.chmod(target, mode)


def restore_file(root: Path, record: dict[str, Any], overwrite: bool, verify_only: bool) -> None:
    if record.get("encoding") != "base64":
        raise ValueError(f"{record.get('path')}: unsupported encoding {record.get('encoding')}")

    content = base64.b64decode(str(record["content"]).encode("ascii"), validate=True)
    expected_size = record.get("size")
    if isinstance(expected_size, int) and len(content) != expected_size:
        raise ValueError(f"{record.get('path')}: size mismatch")

    digest = hashlib.sha256(content).hexdigest()
    if digest != record.get("sha256"):
        raise ValueError(f"{record.get('path')}: sha256 mismatch")

    if verify_only:
        return

    target = safe_output_path(root, str(record["path"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_can_write(target, overwrite)
    target.write_bytes(content)
    mode = record.get("mode")
    if isinstance(mode, int):
        os.chmod(target, mode)


def restore_symlink(
    root: Path,
    record: dict[str, Any],
    overwrite: bool,
    verify_only: bool,
    no_symlinks: bool,
) -> None:
    target = safe_output_path(root, str(record["path"]))
    link_target = str(record["target"])

    if verify_only:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_can_write(target, overwrite)
    if no_symlinks:
        target.write_text(link_target + "\n", encoding="utf-8")
        return
    os.symlink(link_target, target)


def restore_archive(
    archive_path: Path,
    output_dir: Path,
    overwrite: bool,
    verify_only: bool,
    no_symlinks: bool,
) -> tuple[int, int]:
    records = read_json_lines(archive_path)
    try:
        first_line, header = next(records)
    except StopIteration as exc:
        raise ValueError("archive is empty") from exc

    validate_header(first_line, header)

    if not verify_only:
        if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
            raise FileExistsError(
                f"output directory is not empty; use --overwrite: {output_dir}"
            )
        output_dir.mkdir(parents=True, exist_ok=True)

    entries = 0
    files = 0
    saw_end = False

    for line_number, record in records:
        if saw_end:
            raise ValueError(f"line {line_number}: archive has data after end record")

        record_type = record.get("type")
        try:
            if record_type == "dir":
                restore_dir(output_dir, record, overwrite, verify_only)
                entries += 1
            elif record_type == "file":
                restore_file(output_dir, record, overwrite, verify_only)
                entries += 1
                files += 1
            elif record_type == "symlink":
                restore_symlink(output_dir, record, overwrite, verify_only, no_symlinks)
                entries += 1
            elif record_type == "end":
                expected_entries = record.get("entries")
                expected_files = record.get("files")
                if isinstance(expected_entries, int) and expected_entries != entries:
                    raise ValueError(
                        f"entry count mismatch: expected {expected_entries}, got {entries}"
                    )
                if isinstance(expected_files, int) and expected_files != files:
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

    return entries, files


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

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
