from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flat_packager.archive import ARCHIVE_KIND, emit_json_line, read_json_lines
from flat_packager.inspect import inspect_data
from flat_packager.pack import build_archive, prepare_source
from flat_packager.unpack import load_and_validate_archive, restore_archive


def file_record(path: str, content: bytes) -> dict[str, object]:
    return {
        "type": "file",
        "path": path,
        "mode": 0o644,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def symlink_record(path: str, target: str) -> dict[str, object]:
    return {
        "type": "symlink",
        "path": path,
        "mode": 0o777,
        "target": target,
    }


def write_archive(path: Path, records: list[dict[str, object]], version: int = 1) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        emit_json_line(handle, {"type": ARCHIVE_KIND, "version": version})
        for record in records:
            emit_json_line(handle, record)
        emit_json_line(
            handle,
            {
                "type": "end",
                "entries": len(records),
                "files": sum(1 for record in records if record["type"] == "file"),
            },
        )


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_dir = Path(tempfile.mkdtemp(prefix="flat-packager-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_archive_round_trips_files_directories_binary_and_symlink(self) -> None:
        source = self.work_dir / "source"
        archive = self.work_dir / "repo.flat.txt"
        restored = self.work_dir / "restored"

        (source / "nested" / "deeper").mkdir(parents=True)
        (source / "empty-dir").mkdir()
        (source / "README.md").write_text("# Demo\nhello\n", encoding="utf-8")
        (source / "nested" / "note.txt").write_text("nested text\n", encoding="utf-8")
        (source / "nested" / "deeper" / "data.bin").write_bytes(bytes(range(256)))

        symlink_created = False
        try:
            os.symlink("../README.md", source / "nested" / "readme-link")
            symlink_created = True
        except OSError:
            pass

        entries, files = build_archive(
            root=source,
            output_path=archive,
            source_label=str(source),
            tracked_only=False,
            include_git=False,
            exclude_patterns=[],
            max_file_bytes=None,
        )

        restored_entries, restored_files = restore_archive(
            archive_path=archive,
            output_dir=restored,
            overwrite=False,
            verify_only=False,
            no_symlinks=False,
        )

        self.assertEqual(restored_entries, entries)
        self.assertEqual(restored_files, files)
        self.assertEqual((restored / "README.md").read_text(encoding="utf-8"), "# Demo\nhello\n")
        self.assertEqual(
            (restored / "nested" / "deeper" / "data.bin").read_bytes(),
            bytes(range(256)),
        )
        self.assertTrue((restored / "empty-dir").is_dir())
        if symlink_created:
            self.assertEqual(os.readlink(restored / "nested" / "readme-link"), "../README.md")

    def test_chunked_v2_archive_uses_chunk_records_and_round_trips(self) -> None:
        source = self.work_dir / "source"
        archive = self.work_dir / "repo.flat.txt"
        restored = self.work_dir / "restored"
        source.mkdir()
        (source / "large-ish.txt").write_bytes(b"abcdefghij")

        entries, files = build_archive(
            root=source,
            output_path=archive,
            source_label=str(source),
            tracked_only=False,
            include_git=False,
            exclude_patterns=[],
            max_file_bytes=None,
            chunk_size=4,
        )

        records = [record for _line, record in read_json_lines(archive)]
        self.assertEqual(records[0]["version"], 2)
        self.assertEqual(records[1]["encoding"], "base64-chunks")
        self.assertEqual(records[1]["chunks"], 3)
        self.assertEqual(sum(1 for record in records if record["type"] == "chunk"), 3)

        restored_entries, restored_files = restore_archive(
            archive_path=archive,
            output_dir=restored,
            overwrite=False,
            verify_only=False,
            no_symlinks=False,
        )

        self.assertEqual(restored_entries, entries)
        self.assertEqual(restored_files, files)
        self.assertEqual((restored / "large-ish.txt").read_bytes(), b"abcdefghij")

    def test_gzip_archive_inspects_and_restores(self) -> None:
        source = self.work_dir / "source"
        archive = self.work_dir / "repo.flat.txt.gz"
        restored = self.work_dir / "restored"
        source.mkdir()
        (source / "file.txt").write_text("compressed\n", encoding="utf-8")

        build_archive(
            root=source,
            output_path=archive,
            source_label=str(source),
            tracked_only=False,
            include_git=False,
            exclude_patterns=[],
            max_file_bytes=None,
            chunk_size=3,
            compression="gzip",
        )

        manifest = load_and_validate_archive(archive)
        data = inspect_data(archive, manifest)
        self.assertEqual(data["compression"], "gzip")
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["files"], 1)
        self.assertEqual(data["chunks"], 4)

        restore_archive(
            archive_path=archive,
            output_dir=restored,
            overwrite=False,
            verify_only=False,
            no_symlinks=False,
        )
        self.assertEqual((restored / "file.txt").read_text(encoding="utf-8"), "compressed\n")

    def test_verify_only_checks_archive_without_writing_output(self) -> None:
        source = self.work_dir / "source"
        archive = self.work_dir / "repo.flat.txt"
        output = self.work_dir / "verify-target"

        source.mkdir()
        (source / "file.txt").write_text("content\n", encoding="utf-8")

        build_archive(
            root=source,
            output_path=archive,
            source_label=str(source),
            tracked_only=False,
            include_git=False,
            exclude_patterns=[],
            max_file_bytes=None,
        )

        entries, files = restore_archive(
            archive_path=archive,
            output_dir=output,
            overwrite=False,
            verify_only=True,
            no_symlinks=False,
        )

        self.assertEqual(entries, 1)
        self.assertEqual(files, 1)
        self.assertFalse(output.exists())

    def test_invalid_archive_does_not_partially_restore(self) -> None:
        archive = self.work_dir / "bad.flat.txt"
        output = self.work_dir / "output"
        bad_record = file_record("bad.txt", b"bad\n")
        bad_record["sha256"] = "wrong"
        write_archive(archive, [file_record("ok.txt", b"ok\n"), bad_record])

        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            restore_archive(
                archive_path=archive,
                output_dir=output,
                overwrite=False,
                verify_only=False,
                no_symlinks=False,
            )

        self.assertFalse(output.exists())

    def test_invalid_archive_does_not_replace_existing_output(self) -> None:
        archive = self.work_dir / "bad.flat.txt"
        output = self.work_dir / "output"
        output.mkdir()
        (output / "keep.txt").write_text("keep\n", encoding="utf-8")
        bad_record = file_record("bad.txt", b"bad\n")
        bad_record["sha256"] = "wrong"
        write_archive(archive, [bad_record])

        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            restore_archive(
                archive_path=archive,
                output_dir=output,
                overwrite=True,
                verify_only=False,
                no_symlinks=False,
            )

        self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_existing_symlink_parent_cannot_redirect_restore(self) -> None:
        archive = self.work_dir / "archive.flat.txt"
        output = self.work_dir / "output"
        outside = self.work_dir / "outside"
        output.mkdir()
        outside.mkdir()
        try:
            os.symlink(outside, output / "link")
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        write_archive(archive, [file_record("link/owned.txt", b"owned\n")])

        restore_archive(
            archive_path=archive,
            output_dir=output,
            overwrite=True,
            verify_only=False,
            no_symlinks=False,
        )

        self.assertFalse((outside / "owned.txt").exists())
        self.assertTrue((output / "link").is_dir())
        self.assertEqual((output / "link" / "owned.txt").read_text(encoding="utf-8"), "owned\n")

    def test_archive_symlink_ancestor_is_rejected(self) -> None:
        archive = self.work_dir / "archive.flat.txt"
        output = self.work_dir / "output"
        write_archive(
            archive,
            [
                symlink_record("link", "/tmp"),
                file_record("link/owned.txt", b"owned\n"),
            ],
        )

        with self.assertRaisesRegex(ValueError, "nested under archived symlink"):
            restore_archive(
                archive_path=archive,
                output_dir=output,
                overwrite=False,
                verify_only=False,
                no_symlinks=False,
            )

        self.assertFalse(output.exists())

    def test_remote_clone_options_are_passed_to_git(self) -> None:
        with patch("flat_packager.pack.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            _clone_path, temp_dir = prepare_source(
                "owner/repo",
                branch="release",
                recurse_submodules=True,
                shallow=False,
            )

        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        self.assertIsNotNone(temp_dir)
        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "clone",
                "--branch",
                "release",
                "--recurse-submodules",
                "https://github.com/owner/repo.git",
                str(Path(temp_dir) / "repo"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
