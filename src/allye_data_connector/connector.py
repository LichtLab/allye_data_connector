from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd

from .manifest import ManifestStore, ManifestWriteEvent
from .storage import PayloadRef, cleanup_payload, read_payload_to_dataframe, write_dataframe_payload
from .util import now_iso, resolve_secret_dir


@dataclass(frozen=True)
class SendOptions:
    table_name: Optional[str] = None
    transport: Literal["auto", "shm", "file"] = "auto"
    max_shm_bytes: int = 64 * 1024 * 1024
    chunk_rows: int = 500_000
    ttl_sec: Optional[int] = None
    producer: str = "python"


def send_dataframe(
    df: pd.DataFrame,
    table_name: str | None = None,
    *,
    secret_dir: str | Path | None = None,
    transport: Literal["auto", "shm", "file"] = "auto",
    max_shm_bytes: int = 64 * 1024 * 1024,
    chunk_rows: int = 500_000,
    ttl_sec: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    options = SendOptions(
        table_name=table_name,
        transport=transport,
        max_shm_bytes=max_shm_bytes,
        chunk_rows=chunk_rows,
        ttl_sec=ttl_sec,
        producer="python",
    )
    return _send_dataframe_impl(df=df, secret_dir=secret_dir, options=options, metadata=metadata)


def _send_dataframe_impl(
    *,
    df: pd.DataFrame,
    secret_dir: str | Path | None,
    options: SendOptions,
    metadata: dict[str, Any] | None,
) -> str:
    base_dir = resolve_secret_dir(secret_dir)
    store = ManifestStore(base_dir)

    transfer_id = str(uuid.uuid4())
    resolved_table_name = options.table_name or f"table_{int(time.time())}_{transfer_id[:8]}"

    writing_event = ManifestWriteEvent(
        version=1,
        id=transfer_id,
        table_name=resolved_table_name,
        producer=options.producer,
        status="writing",
        created_at=now_iso(),
        ttl_sec=options.ttl_sec,
        payload=None,
        metadata=metadata or {},
    )
    store.append(writing_event)

    try:
        payload_ref = write_dataframe_payload(
            df,
            base_dir=base_dir,
            transfer_id=transfer_id,
            transport=options.transport,
            max_shm_bytes=options.max_shm_bytes,
            chunk_rows=options.chunk_rows,
            ttl_sec=options.ttl_sec,
        )
    except Exception as exc:  # noqa: BLE001 - surface error in manifest
        store.append(
            writing_event.evolve(
                status="error",
                payload=None,
                metadata={**(metadata or {}), "error": repr(exc)},
            )
        )
        raise

    store.append(
        writing_event.evolve(
            status="ready",
            payload=payload_ref,
            metadata=metadata or {},
        )
    )

    return resolved_table_name


def get_dataframe(
    table_name: str,
    *,
    secret_dir: str | Path | None = None,
    producer: str | None = None,
    wait: bool = False,
    timeout_sec: float = 30.0,
    poll_interval_sec: float = 0.5,
) -> pd.DataFrame:
    base_dir = resolve_secret_dir(secret_dir)
    store = ManifestStore(base_dir)

    deadline = time.time() + timeout_sec
    while True:
        event = store.latest_ready(table_name=table_name, producer=producer)
        if event is not None and event.payload is not None:
            return read_payload_to_dataframe(event.payload)

        if not wait or time.time() >= deadline:
            raise KeyError(f"ready entry not found for table_name={table_name!r}")

        time.sleep(poll_interval_sec)


def list_tables(
    *,
    secret_dir: str | Path | None = None,
    producer: str | None = None,
) -> list[dict[str, Any]]:
    base_dir = resolve_secret_dir(secret_dir)
    store = ManifestStore(base_dir)
    latest = store.latest_ready_per_table(producer=producer)
    out: list[dict[str, Any]] = []
    for table_name, event in sorted(latest.items(), key=lambda kv: kv[0]):
        payload: PayloadRef | None = event.payload
        out.append(
            {
                "table_name": table_name,
                "id": event.id,
                "producer": event.producer,
                "created_at": event.created_at,
                "ttl_sec": event.ttl_sec,
                "transport": payload.transport if payload else None,
                "shape": payload.shape if payload else None,
                "bytes": payload.data_size if payload else None,
            }
        )
    return out


def gc(
    *,
    secret_dir: str | Path | None = None,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """
    Expired payload cleanup based on `payload.expires_at`.

    - `dry_run=True`: only reports what would be removed
    - returns a list of actions: {"action": "delete", "transport": ..., "locator": ...}
    """
    from datetime import datetime

    base_dir = resolve_secret_dir(secret_dir)
    store = ManifestStore(base_dir)

    latest = store.latest_ready_per_table(producer=None)
    actions: list[dict[str, Any]] = []
    now = datetime.now().astimezone()

    for event in latest.values():
        payload = event.payload
        if payload is None or payload.expires_at is None:
            continue
        try:
            expires = datetime.strptime(payload.expires_at, "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            continue
        if expires > now:
            continue

        actions.append({"action": "delete", "transport": payload.transport, "locator": payload.locator})
        if not dry_run:
            cleanup_payload(payload)

    return actions
