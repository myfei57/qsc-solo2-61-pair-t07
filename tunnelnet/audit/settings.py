"""Persistent audit service configuration.

The retention policy survives restarts in ``state.json``. The file is small
and rewritten atomically; it never holds audit records themselves.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import layout
from .retention import RetentionPolicy


@dataclass(frozen=True)
class AuditSettings:
    online_days: int = 7
    max_segment_bytes: int = 8 * 1024 * 1024
    max_day_bytes: int = 32 * 1024 * 1024

    def policy(self) -> RetentionPolicy:
        return RetentionPolicy(online_days=max(1, self.online_days))

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def load_settings(root: str, defaults: AuditSettings | None = None) -> AuditSettings:
    settings = defaults or AuditSettings()
    path = layout.state_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, ValueError):
        return settings
    if not isinstance(payload, dict):
        return settings
    values = settings.as_dict()
    for key in values:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            values[key] = value
    return AuditSettings(**values)


def save_settings(root: str, settings: AuditSettings) -> None:
    layout.ensure_layout(root)
    path = layout.state_path(root)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(settings.as_dict(), indent=2, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
