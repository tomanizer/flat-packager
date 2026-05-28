"""Validate a flat archive without restoring it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .archive import detect_archive_compression
from .unpack import load_and_validate_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a flat-pack archive.")
    parser.add_argument("archive", help="Archive to validate.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive).expanduser().resolve()

    try:
        manifest = load_and_validate_archive(archive_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    compression = detect_archive_compression(archive_path)
    print(
        "verified "
        f"{manifest.entries} entries "
        f"({manifest.files} files, {manifest.dirs} dirs, "
        f"{manifest.symlinks} symlinks, {manifest.chunks} chunks, "
        f"{manifest.bytes} bytes, version {manifest.version}, {compression})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
