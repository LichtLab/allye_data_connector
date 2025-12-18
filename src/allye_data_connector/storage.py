from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd


Transport = Literal["shm_arrow_stream_v1", "file_arrow_ipc_v1"]


@dataclass(frozen=True)
class PayloadRef:
    transport: Transport
    locator: str
    data_size: int
    shape: tuple[int, int]
    schema_sha256: str
    created_at: str
    expires_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "locator": self.locator,
            "data_size": self.data_size,
            "shape": list(self.shape),
            "schema_sha256": self.schema_sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "PayloadRef":
        shape = obj.get("shape") or [0, 0]
        return PayloadRef(
            transport=obj["transport"],
            locator=obj["locator"],
            data_size=int(obj["data_size"]),
            shape=(int(shape[0]), int(shape[1])),
            schema_sha256=str(obj.get("schema_sha256") or ""),
            created_at=str(obj.get("created_at") or ""),
            expires_at=obj.get("expires_at"),
        )


def write_dataframe_payload(
    df: pd.DataFrame,
    *,
    base_dir: Path,
    transfer_id: str,
    transport: Literal["auto", "shm", "file"],
    max_shm_bytes: int,
    chunk_rows: int,
    ttl_sec: int | None,
) -> PayloadRef:
    import pyarrow as pa
    import pyarrow.ipc as ipc

    payload_dir = base_dir / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)

    schema_hash = _schema_sha256(df)
    created_at = _now_iso()
    expires_at = _expires_at_iso(ttl_sec)

    approx_bytes = int(df.memory_usage(index=False, deep=True).sum())
    use_shm = (transport == "shm") or (transport == "auto" and approx_bytes <= max_shm_bytes)
    if transport == "file":
        use_shm = False

    if use_shm:
        # Stream format into shared memory (small payloads only).
        sink = pa.BufferOutputStream()
        sample_rows = min(len(df), min(chunk_rows, 50_000))
        sample_table = pa.Table.from_pandas(df.iloc[:sample_rows], preserve_index=False)
        writer = ipc.new_stream(sink, sample_table.schema)
        try:
            for chunk in _iter_df_chunks(df, chunk_rows):
                table = pa.Table.from_pandas(chunk, preserve_index=False, schema=sample_table.schema, safe=False)
                writer.write_table(table)
        finally:
            writer.close()

        buf = sink.getvalue()
        data = buf.to_pybytes()

        if len(data) > max_shm_bytes and transport != "shm":
            # Auto fallback to file.
            use_shm = False
        else:
            from multiprocessing.shared_memory import SharedMemory

            shm = SharedMemory(create=True, size=len(data))
            shm.buf[: len(data)] = data
            shm.close()
            return PayloadRef(
                transport="shm_arrow_stream_v1",
                locator=shm.name,
                data_size=len(data),
                shape=(int(df.shape[0]), int(df.shape[1])),
                schema_sha256=schema_hash,
                created_at=created_at,
                expires_at=expires_at,
            )

    # File-based Arrow IPC (recommended for large payloads, mmap-friendly).
    path = payload_dir / f"{transfer_id}.arrow"
    compression = os.environ.get("ALLYE_ARROW_COMPRESSION", "zstd") or None
    options = ipc.IpcWriteOptions(compression=compression) if compression else ipc.IpcWriteOptions()

    sample_rows = min(len(df), min(chunk_rows, 50_000))
    sample_table = pa.Table.from_pandas(df.iloc[:sample_rows], preserve_index=False)
    with pa.OSFile(str(path), "wb") as sink:
        with ipc.new_file(sink, sample_table.schema, options=options) as writer:
            for chunk in _iter_df_chunks(df, chunk_rows):
                table = pa.Table.from_pandas(chunk, preserve_index=False, schema=sample_table.schema, safe=False)
                writer.write_table(table)

    data_size = path.stat().st_size
    return PayloadRef(
        transport="file_arrow_ipc_v1",
        locator=str(path),
        data_size=int(data_size),
        shape=(int(df.shape[0]), int(df.shape[1])),
        schema_sha256=schema_hash,
        created_at=created_at,
        expires_at=expires_at,
    )


def read_payload_to_dataframe(payload: PayloadRef) -> pd.DataFrame:
    import pyarrow as pa
    import pyarrow.ipc as ipc

    if payload.transport == "shm_arrow_stream_v1":
        from multiprocessing.shared_memory import SharedMemory

        shm = SharedMemory(name=payload.locator, create=False)
        try:
            data = bytes(shm.buf[: payload.data_size])
        finally:
            shm.close()
        reader = ipc.open_stream(pa.BufferReader(data))
        return reader.read_all().to_pandas()

    if payload.transport == "file_arrow_ipc_v1":
        source = pa.memory_map(payload.locator, "r")
        reader = ipc.open_file(source)
        return reader.read_all().to_pandas()

    raise ValueError(f"unknown transport: {payload.transport}")


def cleanup_payload(payload: PayloadRef) -> None:
    if payload.transport == "file_arrow_ipc_v1":
        try:
            Path(payload.locator).unlink(missing_ok=True)
        except Exception:
            return
        return

    if payload.transport == "shm_arrow_stream_v1":
        try:
            from multiprocessing.shared_memory import SharedMemory

            shm = SharedMemory(name=payload.locator, create=False)
            try:
                shm.unlink()
            finally:
                shm.close()
        except Exception:
            return
        return

    raise ValueError(f"unknown transport: {payload.transport}")


def _iter_df_chunks(df: pd.DataFrame, chunk_rows: int):
    if chunk_rows <= 0 or len(df) <= chunk_rows:
        yield df
        return
    for start in range(0, len(df), chunk_rows):
        yield df.iloc[start : start + chunk_rows]


def _schema_sha256(df: pd.DataFrame) -> str:
    d = {
        "columns": list(map(str, df.columns.tolist())),
        "dtypes": {str(k): str(v) for k, v in df.dtypes.to_dict().items()},
    }
    raw = (str(d["columns"]) + "|" + str(d["dtypes"])).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _expires_at_iso(ttl_sec: int | None) -> str | None:
    if ttl_sec is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + ttl_sec))
