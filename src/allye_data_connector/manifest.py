from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from .lock import FileLock
from .storage import PayloadRef


ManifestStatus = Literal["writing", "ready", "error"]


@dataclass(frozen=True)
class ManifestWriteEvent:
    version: int
    id: str
    table_name: str
    producer: str
    status: ManifestStatus
    created_at: str
    ttl_sec: int | None
    payload: PayloadRef | None
    metadata: dict[str, Any]

    def evolve(
        self,
        *,
        status: ManifestStatus | None = None,
        payload: PayloadRef | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ManifestWriteEvent":
        return replace(
            self,
            status=self.status if status is None else status,
            payload=payload,
            metadata=self.metadata if metadata is None else metadata,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "table_name": self.table_name,
            "producer": self.producer,
            "status": self.status,
            "created_at": self.created_at,
            "ttl_sec": self.ttl_sec,
            "payload": self.payload.to_json() if self.payload else None,
            "metadata": self.metadata,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "ManifestWriteEvent":
        payload = obj.get("payload")
        return ManifestWriteEvent(
            version=int(obj["version"]),
            id=str(obj["id"]),
            table_name=str(obj["table_name"]),
            producer=str(obj.get("producer") or ""),
            status=str(obj["status"]),
            created_at=str(obj["created_at"]),
            ttl_sec=obj.get("ttl_sec"),
            payload=PayloadRef.from_json(payload) if payload else None,
            metadata=dict(obj.get("metadata") or {}),
        )


class ManifestStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.manifest_path = self.base_dir / "manifest.jsonl"
        self.lock_path = self.base_dir / "manifest.lock"

    def append(self, event: ManifestWriteEvent) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_json(), ensure_ascii=False, separators=(",", ":"))
        with FileLock(self.lock_path):
            with open(self.manifest_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def iter_events(self) -> Iterable[ManifestWriteEvent]:
        if not self.manifest_path.exists():
            return
        # No lock for read: JSONL is append-only, and we only read full lines.
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    yield ManifestWriteEvent.from_json(obj)
                except Exception:
                    continue

    def latest_ready(self, *, table_name: str, producer: str | None = None) -> Optional[ManifestWriteEvent]:
        latest: ManifestWriteEvent | None = None
        for event in self.iter_events():
            if event.table_name != table_name:
                continue
            if producer is not None and event.producer != producer:
                continue
            if event.status != "ready":
                continue
            latest = event
        return latest

    def latest_ready_per_table(self, *, producer: str | None = None) -> dict[str, ManifestWriteEvent]:
        latest: dict[str, ManifestWriteEvent] = {}
        for event in self.iter_events():
            if event.status != "ready":
                continue
            if producer is not None and event.producer != producer:
                continue
            latest[event.table_name] = event
        return latest
