"""Pack a repository tree into one flat text archive."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .archive import ARCHIVE_KIND, ARCHIVE_VERSION, emit_json_line


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten a GitHub/local repository into one text archive."
    )
    parser.add_argument(
        "source",
        help=(
            "Repository source. Use a local path, a git URL, or owner/repo "
            "shorthand for public GitHub repos."
        ),
    )
    parser.add_argument("output", help="Path to write the flat text archive.")
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="For local git checkouts, include only files tracked by git.",
    )
    parser.add_argument(
        "--include-git",
        action="store_true",
        help="Include the .git directory when walking a local directory.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude paths matching this glob. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="Fail if any single file is larger than this many bytes.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the temporary clone directory when source is a git URL.",
    )
    return parser.parse_args()


def looks_like_git_source(source: str) -> bool:
    if source.startswith(("http://", "https://", "ssh://", "git@")):
        return True
    if source.endswith(".git"):
        return True
    if "/" in source and not Path(source).exists():
        owner_repo = source.split("/")
        return len(owner_repo) == 2 and all(owner_repo)
    return False


def normalize_source(source: str) -> str:
    if "/" in source and not Path(source).exists() and not source.startswith(
        ("http://", "https://", "ssh://", "git@")
    ):
        owner, repo = source.split("/", 1)
        return f"https://github.com/{owner}/{repo}.git"
    return source


def prepare_source(source: str) -> tuple[Path, str | None]:
    path = Path(source).expanduser()
    if path.exists():
        return path.resolve(), None

    if not looks_like_git_source(source):
        raise SystemExit(f"source does not exist and does not look like a git repo: {source}")

    clone_url = normalize_source(source)
    temp_dir = tempfile.mkdtemp(prefix="repo-flat-clone-")
    clone_path = Path(temp_dir) / "repo"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(clone_path)],
            check=True,
        )
    except FileNotFoundError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit("git is required to clone remote repositories") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit(f"git clone failed with exit code {exc.returncode}") from exc
    return clone_path.resolve(), temp_dir


def is_excluded(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def safe_relative_path(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel == "." or rel.startswith("../") or rel.startswith("/"):
        raise ValueError(f"unsafe path discovered: {path}")
    return rel


def tracked_paths(root: Path, exclude_patterns: list[str]) -> Iterator[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SystemExit("git is required for --tracked-only") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"git ls-files failed: {message}") from exc

    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        if is_excluded(rel, exclude_patterns):
            continue
        yield root / rel


def walked_paths(
    root: Path,
    output_path: Path,
    include_git: bool,
    exclude_patterns: list[str],
) -> Iterator[Path]:
    for current_root, dir_names, file_names in os.walk(root, topdown=True):
        current = Path(current_root)
        rel_current = "" if current == root else safe_relative_path(root, current)

        kept_dirs = []
        symlink_dirs = []
        for name in sorted(dir_names):
            path = current / name
            rel = f"{rel_current}/{name}" if rel_current else name
            if not include_git and rel == ".git":
                continue
            if is_excluded(rel, exclude_patterns):
                continue
            if path.is_symlink():
                symlink_dirs.append(path)
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        if current != root:
            yield current

        yield from symlink_dirs

        for name in sorted(file_names):
            path = current / name
            rel = safe_relative_path(root, path)
            if path.resolve() == output_path:
                continue
            if is_excluded(rel, exclude_patterns):
                continue
            yield path


def parent_dirs_for_tracked_files(root: Path, files: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for file_path in files:
        for parent in reversed(file_path.relative_to(root).parents):
            if parent == Path("."):
                continue
            full_parent = root / parent
            if full_parent not in seen:
                seen.add(full_parent)
                ordered.append(full_parent)
    return ordered


def file_record(root: Path, path: Path, max_file_bytes: int | None) -> dict[str, object]:
    rel = safe_relative_path(root, path)
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)

    if stat.S_ISLNK(metadata.st_mode):
        return {
            "type": "symlink",
            "path": rel,
            "mode": mode,
            "target": os.readlink(path),
        }

    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"unsupported filesystem entry: {rel}")

    size = metadata.st_size
    if max_file_bytes is not None and size > max_file_bytes:
        raise ValueError(f"{rel} is {size} bytes, above --max-file-bytes")

    content = path.read_bytes()
    return {
        "type": "file",
        "path": rel,
        "mode": mode,
        "size": size,
        "sha256": hashlib.sha256(content).hexdigest(),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def dir_record(root: Path, path: Path) -> dict[str, object]:
    metadata = path.lstat()
    return {
        "type": "dir",
        "path": safe_relative_path(root, path),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def build_archive(
    root: Path,
    output_path: Path,
    source_label: str,
    tracked_only: bool,
    include_git: bool,
    exclude_patterns: list[str],
    max_file_bytes: int | None,
) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "type": ARCHIVE_KIND,
        "version": ARCHIVE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source_label,
        "root_name": root.name,
        "tracked_only": tracked_only,
    }

    entry_count = 0
    file_count = 0

    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        emit_json_line(handle, header)

        if tracked_only:
            files = sorted(tracked_paths(root, exclude_patterns), key=lambda p: p.as_posix())
            for directory in parent_dirs_for_tracked_files(root, files):
                emit_json_line(handle, dir_record(root, directory))
                entry_count += 1
            paths: Iterable[Path] = files
        else:
            paths = walked_paths(root, output_path.resolve(), include_git, exclude_patterns)

        for path in paths:
            if path.is_dir() and not path.is_symlink():
                record = dir_record(root, path)
            else:
                record = file_record(root, path, max_file_bytes)
                if record["type"] == "file":
                    file_count += 1
            emit_json_line(handle, record)
            entry_count += 1

        emit_json_line(handle, {"type": "end", "entries": entry_count, "files": file_count})

    return entry_count, file_count


def main() -> int:
    args = parse_args()
    temp_dir: str | None = None
    try:
        root, temp_dir = prepare_source(args.source)
        if not root.is_dir():
            raise SystemExit(f"source is not a directory: {root}")

        output_path = Path(args.output).expanduser().resolve()
        entries, files = build_archive(
            root=root,
            output_path=output_path,
            source_label=args.source,
            tracked_only=args.tracked_only,
            include_git=args.include_git,
            exclude_patterns=args.exclude,
            max_file_bytes=args.max_file_bytes,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            if args.keep_clone:
                print(f"kept clone at {temp_dir}", file=sys.stderr)
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"wrote {output_path} ({entries} entries, {files} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
