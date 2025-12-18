# allye_data_connector

Python から `pandas.DataFrame` を送受信するためのローカルコネクタです。

- Python 側: `send_datafame(df)` / `get_datafame(table_name)`
- 連携方式: `~/.alley_secret/manifest.jsonl`（追記）+ 共有ストレージ（小さければ OS shared memory / 大きければ mmap 可能なファイル）

## インストール（開発中）

```bash
pip install -e .
```

## 使い方

```python
import pandas as pd
import allye_data_connector as adc

df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
name = adc.send_dataframe(df, table_name="demo")

df2 = adc.get_dataframe("demo")
```

## 保存先

- デフォルト: `~/.allye_secrets/`
  - manifest: `manifest.jsonl`
  - payload: `payloads/`
- 環境変数 `ALLYE_SECRET_DIR` で上書き可能

## manifest.jsonl（概要）

1行=1イベントの追記です。`status="ready"` の行だけを使えばOKで、`payload` に実体の参照が入ります。

- `payload.transport`: `file_arrow_ipc_v1` または `shm_arrow_stream_v1`
- `payload.locator`: `*.arrow` ファイルパス or shared memory 名

Allye widget 側が送信する場合は `producer="allye"` を推奨します（Python側 `get_dataframe(..., producer="allye")` のフィルタに使えます）。

## 掃除（任意）

`ttl_sec` を付けて送った payload は `gc(dry_run=False)` で期限切れ削除できます。

## 注意

- 200列×1000万行級は「共有メモリ1発」だと OS 制約に当たりやすいので、本実装は自動でファイル（mmap可能）へ退避します。
- dtype は pandas を前提にし、シリアライズ形式は Apache Arrow IPC です。
