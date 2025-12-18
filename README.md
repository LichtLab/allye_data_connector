# allye_data_connector

`allye_data_connector` is a local connector that lets you exchange `pandas.DataFrame` objects between Python and Allye widgets.

It works by appending events to a line-delimited manifest file (`manifest.jsonl`) and storing the actual Arrow payload either in:

- OS shared memory (fast, for smaller data), or
- a memory-mappable file on disk (for larger data).

## Installation

From PyPI:

```bash
pip install allye_data_connector
```

From source (development):

```bash
pip install -e .
```

If you haven't installed Allye yet, please download and install it from [here](https://www.ai-allye.com/).

## Quickstart

Send a DataFrame to Allye Canvas:

```python
import pandas as pd
import allye_data_connector as adc

df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
table_name = adc.send_dataframe(df, table_name="demo")
print(table_name)
```

Use Allye's `Allye Data Receiver` widget to receive the dataframe data and perform visualizations.

![Allye Data Receiver](img/Receiver.png)





Get Data from Canvas / Read it back:

Use Allye's `Allye Data Transmitter` widget to send Allye data, receive it on the Jupyter side, and execute subsequent processing.

![Allye Data Transmitter](img/Transmitter.png)

```python
df2 = adc.get_dataframe("demo")
print(df2.head())
```

Wait for a DataFrame to appear (polling):

```python
df3 = adc.get_dataframe("from_widget", producer="allye", wait=True, timeout_sec=30)
```

List available tables:

```python
tables = adc.list_tables()
for t in tables:
    print(t["table_name"], t["transport"], t["shape"])
```


## How it works (high level)

1. `send_dataframe()` appends a `status="writing"` event to `manifest.jsonl`.
2. The DataFrame is serialized to Apache Arrow:
   - `transport="auto"` uses shared memory when the payload is below `max_shm_bytes`
   - otherwise it falls back to writing Arrow IPC files under `payloads/` (chunked by `chunk_rows`)
3. A final `status="ready"` event is appended with a `payload` reference (transport + locator).
4. `get_dataframe()` scans the manifest for the latest `status="ready"` entry matching `table_name` (and optional `producer`),
   then loads the Arrow payload back into a DataFrame.

## Features

- Send/receive DataFrames with `send_dataframe()` and `get_dataframe()`
- Local, file-based coordination via `manifest.jsonl` (append-only)
- Automatic transport selection (`transport="auto"`) based on size
- Optional TTL + garbage collection for payload cleanup (`gc()`)
- Producer filtering (e.g., read only entries produced by Allye widgets)


## Cleanup (optional)

If you send with `ttl_sec=...`, the payload gets an expiration timestamp. You can remove expired payloads with:

```python
adc.gc(dry_run=False)
```

`gc(dry_run=True)` (default) only reports what would be removed.

## Notes

- This is a local IPC-oriented connector; it does not do networking or authentication.
- Very large DataFrames may exceed OS shared memory limits; the implementation automatically falls back to disk-backed Arrow files.
