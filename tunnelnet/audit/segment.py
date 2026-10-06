"""JSONL segment files behind the live audit journal."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import layout
from .model import Record


@dataclass(frozen=True)
class Segment:
    """One immutable or appendable JSONL file under ``live/<day>/``."""

    path: str
    day: str
    event: str
    generation: int

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def relative(self) -> str:
        """Path inside an archive tar, keyed by day and event."""

        return os.path.join(self.day, self.name)

    @property
    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except FileNotFoundError:
            return 0

    def records(self) -> list[Record]:
        """Decode every well formed line; corrupt tails are skipped.

        A torn final line can only come from a crash mid append, so it is
        ignored while sealed history earlier in the file stays readable.
        """

        items: list[Record] = []
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        document = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(document, dict) and {"id", "event", "at"} <= set(document):
                        items.append(Record.from_document(document))
        except FileNotFoundError:
            return []
        return items


def list_day_segments(root: str, day: str) -> list[Segment]:
    directory = layout.day_dir(root, day)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    segments: list[Segment] = []
    for name in names:
        parsed = layout.parse_segment_name(name)
        if parsed is None:
            continue
        event, generation = parsed
        segments.append(
            Segment(path=os.path.join(directory, name), day=day, event=event, generation=generation)
        )
    return sorted(segments, key=lambda item: (item.event, item.generation))


def list_live_days(root: str) -> list[str]:
    directory = layout.live_dir(root)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    days = [name for name in names if _looks_like_day(name) and os.path.isdir(os.path.join(directory, name))]
    return sorted(days)


def _looks_like_day(name: str) -> bool:
    if len(name) != 10 or name[4] != "-" or name[7] != "-":
        return False
    return name[:4].isdigit() and name[5:7].isdigit() and name[8:].isdigit()


def next_generation(root: str, day: str, event: str) -> int:
    generations = [
        segment.generation
        for segment in list_day_segments(root, day)
        if segment.event == event
    ]
    return (max(generations) + 1) if generations else 1


def append_line(path: str, line: str) -> None:
    """Append one JSON line with an ``O_APPEND`` open and flushed bytes."""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def encode_record(record: Record) -> str:
    return json.dumps(record.to_document(), ensure_ascii=False, sort_keys=True)
