from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flat_packager.pack import build_archive
from flat_packager.unpack import restore_archive


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


if __name__ == "__main__":
    unittest.main()
