#!/usr/bin/env python3
"""Compatibility wrapper for the flat-pack console command."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from flat_packager.pack import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
