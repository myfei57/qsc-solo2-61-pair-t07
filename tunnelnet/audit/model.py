"""Audit records written for every linkage event."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    """One linkage audit entry.

    ``detail`` carries optional event specific payload. It is defaulted so
    older callers and existing three field documents keep loading unchanged.
    ``origin`` marks whether a record was read from the live window or from a
    sealed archive package; it is never persisted as part of the record.
    """

    id: str
    event: str
    at: str
    detail: dict[str, object] | None = None
    origin: str = "live"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"id": self.id, "event": self.event, "at": self.at}
        if self.detail:
            payload["detail"] = self.detail
        if self.origin != "live":
            payload["origin"] = self.origin
        return payload

    def to_document(self) -> dict[str, object]:
        """Stable JSON document persisted on disk."""

        document: dict[str, object] = {"id": self.id, "event": self.event, "at": self.at}
        if self.detail:
            document["detail"] = self.detail
        return document

    @classmethod
    def from_document(cls, document: dict[str, object], origin: str = "live") -> "Record":
        detail = document.get("detail")
        return cls(
            id=str(document["id"]),
            event=str(document["event"]),
            at=str(document["at"]),
            detail=detail if isinstance(detail, dict) else None,
            origin=origin,
        )
