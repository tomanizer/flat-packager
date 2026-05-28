"""Shared archive constants and helpers."""

from __future__ import annotations

import json
from typing import TextIO


ARCHIVE_KIND = "repo-flat-archive"
ARCHIVE_VERSION = 1


def emit_json_line(handle: TextIO, payload: dict[str, object]) -> None:
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    handle.write("\n")
