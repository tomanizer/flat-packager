"""Shared archive constants and helpers."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterator, TextIO


ARCHIVE_KIND = "repo-flat-archive"
ARCHIVE_VERSION = 2
SUPPORTED_ARCHIVE_VERSIONS = {1, 2}
DEFAULT_CHUNK_SIZE = 1024 * 1024


def detect_archive_compression(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            if handle.read(2) == b"\x1f\x8b":
                return "gzip"
    except FileNotFoundError:
        pass
    return "none"


def should_compress(path: Path, compression: str) -> bool:
    if compression == "gzip":
        return True
    if compression == "none":
        return False
    if compression == "auto":
        return path.suffix == ".gz"
    raise ValueError(f"unsupported compression mode: {compression}")


def open_archive_for_write(
    path: Path,
    compression: str = "auto",
) -> TextIO:
    if should_compress(path, compression):
        return gzip.open(path, "wt", encoding="utf-8", newline="\n")
    return path.open("w", encoding="utf-8", newline="\n")


def open_archive_for_read(path: Path) -> TextIO:
    if detect_archive_compression(path) == "gzip":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def emit_json_line(handle: TextIO, payload: dict[str, object]) -> None:
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    handle.write("\n")


def read_json_lines(archive_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with open_archive_for_read(archive_path) as handle:
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
