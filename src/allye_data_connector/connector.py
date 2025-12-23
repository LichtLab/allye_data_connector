from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.request import Request, urlopen

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


_CANVAS_API_INFO_PATH = Path("~/.allye_secrets/allye_canvas_api.json").expanduser()


def _resolve_canvas_api_url() -> Optional[str]:
    env = (os.environ.get("ALLYE_CANVAS_API_URL") or "").strip()
    if env:
        return env
    try:
        raw = _CANVAS_API_INFO_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    base_url = str(data.get("base_url") or "").strip()
    return base_url or None


def _notify_canvas_receiver(
    table_name: str,
    *,
    create_if_missing: bool,
    refresh_manifest: bool,
    timeout_sec: float,
) -> dict[str, Any]:
    base_url = _resolve_canvas_api_url()
    if not base_url:
        return {"ok": False, "error": "api_url_missing"}
    payload = {
        "table_name": table_name,
        "create_if_missing": bool(create_if_missing),
        "refresh_manifest": bool(refresh_manifest),
    }
    url = base_url.rstrip("/") + "/v1/data-receiver/load"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            resp_body = resp.read()
    except Exception as exc:  # noqa: BLE001 - surface as status
        return {"ok": False, "error": f"request_failed: {exc}"}
    try:
        data = json.loads(resp_body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - surface as status
        return {"ok": False, "error": "invalid_response"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid_response"}
    data.setdefault("ok", False)
    return data


def _log_canvas_notify_result(table_name: str, result: dict[str, Any]) -> None:
    if result.get("ok"):
        created = bool(result.get("created", False))
        loaded = bool(result.get("loaded", True))
        if created and loaded:
            print(
                f"[allye_data_connector] Allye Data Receiver created and loaded table '{table_name}'."
            )
        elif loaded:
            print(f"[allye_data_connector] Allye Data Receiver loaded table '{table_name}'.")
        else:
            print(
                "[allye_data_connector] Allye Data Receiver was created, but load did not complete."
            )
        return
    error = result.get("error", "unknown_error")
    print(
        "[allye_data_connector] Failed to create/load Allye Data Receiver "
        f"(error={error}). Please load the table manually."
    )


def send_dataframe(
    df: pd.DataFrame,
    table_name: str | None = None,
    *,
    secret_dir: str | Path | None = None,
    transport: Literal["auto", "shm", "file"] = "auto",
    max_shm_bytes: int = 64 * 1024 * 1024,
    chunk_rows: int = 500_000,
    ttl_sec: int | None = None,
    shm_unlink_on_read: bool = True,
    notify_canvas: bool = True,
    notify_create_widget: bool = True,
    notify_refresh_manifest: bool = True,
    notify_timeout_sec: float = 1.0,
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
    merged_metadata = dict(metadata or {})
    merged_metadata["shm_unlink_on_read"] = bool(shm_unlink_on_read)
    resolved_name = _send_dataframe_impl(
        df=df,
        secret_dir=secret_dir,
        options=options,
        metadata=merged_metadata,
    )
    if notify_canvas:
        result = _notify_canvas_receiver(
            resolved_name,
            create_if_missing=notify_create_widget,
            refresh_manifest=notify_refresh_manifest,
            timeout_sec=notify_timeout_sec,
        )
        _log_canvas_notify_result(resolved_name, result)
    return resolved_name


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
