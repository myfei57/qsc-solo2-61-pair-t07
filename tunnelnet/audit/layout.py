"""On disk layout for durable audit storage.

Layout under one audit root::

    live/                          online retention window, append only
      2026-10-05/
        access_granted.0001.jsonl
        access_granted.0002.jsonl   a second generation after a size rollover
        fire_detect.0001.jsonl
    staging/                       segments captured while building a package
    archive/
      2026-10-04.0001.tar           immutable archive package (WORM)
      2026-10-04.0001.manifest.json manifest with checksum and coverage
    manifest.log                   append only registry of sealed packages
    state.json                     retention policy and journal watermarks

Segments are grouped by event type first ("audit records fall into files by
type") and then by UTC day; the generation suffix lets a day keep several
immutable segment files when the size guard rolls the journal over.
"""

from __future__ import annotations

import os

LIVE_DIR = "live"
STAGING_DIR = "staging"
ARCHIVE_DIR = "archive"

SEGMENT_SUFFIX = ".jsonl"
PACKAGE_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".manifest.json"
PARTIAL_SUFFIX = ".part"
LOG_NAME = "manifest.log"
STATE_NAME = "state.json"


def ensure_layout(root: str) -> None:
    for directory in (live_dir(root), staging_dir(root), archive_dir(root)):
        os.makedirs(directory, exist_ok=True)


def live_dir(root: str) -> str:
    return os.path.join(root, LIVE_DIR)


def staging_dir(root: str) -> str:
    return os.path.join(root, STAGING_DIR)


def archive_dir(root: str) -> str:
    return os.path.join(root, ARCHIVE_DIR)


def manifest_log_path(root: str) -> str:
    return os.path.join(root, LOG_NAME)


def state_path(root: str) -> str:
    return os.path.join(root, STATE_NAME)


def day_dir(root: str, day: str) -> str:
    return os.path.join(live_dir(root), day)


def segment_name(event: str, generation: int) -> str:
    return f"{event}.{generation:04d}{SEGMENT_SUFFIX}"


def parse_segment_name(name: str) -> tuple[str, int] | None:
    """Split a segment file name into ``(event, generation)``."""

    if not name.endswith(SEGMENT_SUFFIX):
        return None
    stem = name[: -len(SEGMENT_SUFFIX)]
    event, separator, generation_text = stem.rpartition(".")
    if not separator or not generation_text.isdigit():
        return None
    return event, int(generation_text)


def package_name(day: str, sequence: int) -> str:
    return f"{day}.{sequence:04d}{PACKAGE_SUFFIX}"


def manifest_name(day: str, sequence: int) -> str:
    return f"{day}.{sequence:04d}{MANIFEST_SUFFIX}"


def parse_package_name(name: str) -> tuple[str, int] | None:
    if not name.endswith(PACKAGE_SUFFIX):
        return None
    stem = name[: -len(PACKAGE_SUFFIX)]
    day, separator, sequence_text = stem.rpartition(".")
    if not separator or not sequence_text.isdigit():
        return None
    return day, int(sequence_text)


def package_id_for(day: str, sequence: int) -> str:
    return f"{day}.{sequence:04d}"
