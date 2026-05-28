"""Inspect flat-pack archive contents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .archive import detect_archive_compression
from .unpack import ArchiveManifest, load_and_validate_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a flat-pack archive.")
    parser.add_argument("archive", help="Archive to inspect.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of largest files and symlinks to display.",
    )
    return parser.parse_args()


def human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def file_records(manifest: ArchiveManifest) -> list[dict[str, Any]]:
    return [
        record
        for _line_number, record in manifest.records
        if record.get("type") == "file"
    ]


def symlink_records(manifest: ArchiveManifest) -> list[dict[str, Any]]:
    return [
        record
        for _line_number, record in manifest.records
        if record.get("type") == "symlink"
    ]


def inspect_data(archive_path: Path, manifest: ArchiveManifest) -> dict[str, Any]:
    files = file_records(manifest)
    symlinks = symlink_records(manifest)
    largest_files = sorted(files, key=lambda record: int(record.get("size", 0)), reverse=True)
    return {
        "archive": str(archive_path),
        "compression": detect_archive_compression(archive_path),
        "version": manifest.version,
        "source": manifest.header.get("source"),
        "created_at": manifest.header.get("created_at"),
        "root_name": manifest.header.get("root_name"),
        "entries": manifest.entries,
        "files": manifest.files,
        "dirs": manifest.dirs,
        "symlinks": manifest.symlinks,
        "chunks": manifest.chunks,
        "bytes": manifest.bytes,
        "largest_files": [
            {
                "path": record["path"],
                "size": record.get("size", 0),
                "sha256": record.get("sha256"),
                "chunks": record.get("chunks", 0),
            }
            for record in largest_files
        ],
        "symlink_records": [
            {
                "path": record["path"],
                "target": record.get("target"),
            }
            for record in symlinks
        ],
    }


def print_text(data: dict[str, Any], limit: int) -> None:
    print(f"Archive: {data['archive']}")
    print(f"Version: {data['version']}")
    print(f"Compression: {data['compression']}")
    if data.get("source"):
        print(f"Source: {data['source']}")
    if data.get("created_at"):
        print(f"Created: {data['created_at']}")
    if data.get("root_name"):
        print(f"Root: {data['root_name']}")
    print(
        "Entries: "
        f"{data['entries']} "
        f"({data['files']} files, {data['dirs']} dirs, {data['symlinks']} symlinks)"
    )
    print(f"File bytes: {human_size(int(data['bytes']))}")
    print(f"Chunks: {data['chunks']}")

    largest_files = data["largest_files"][: max(limit, 0)]
    if largest_files:
        print("Largest files:")
        for record in largest_files:
            chunk_text = f", {record['chunks']} chunks" if record.get("chunks") else ""
            print(f"  {human_size(int(record['size']))}  {record['path']}{chunk_text}")

    symlinks = data["symlink_records"][: max(limit, 0)]
    if symlinks:
        print("Symlinks:")
        for record in symlinks:
            print(f"  {record['path']} -> {record['target']}")


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive).expanduser().resolve()

    try:
        manifest = load_and_validate_archive(archive_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    data = inspect_data(archive_path, manifest)
    limit = max(args.limit, 0)
    data["largest_files"] = data["largest_files"][:limit]
    data["symlink_records"] = data["symlink_records"][:limit]
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print_text(data, limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
