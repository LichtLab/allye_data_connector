# Allye Widgets 機能要件（allye_data_receiver / allye_data_transmitter）

本資料は、Python パッケージ `allye_data_connector` と相互運用するために、Allye 側 widget が満たすべき機能要件をまとめたものです。

## 1. 前提・スコープ

- 対象OS: macOS / Windows
- 実行環境: 同一ホスト・同一ユーザ権限のプロセス間連携
- データ型: `pandas.DataFrame` ↔ Orange Table（Allye canvas のテーブル）
- 連携方式: `manifest.jsonl`（追記）+ payload（ファイル or 共有メモリ）
- 大容量（例: 200列×1000万行）を想定するが、UI/メモリ制約により **常に全件ロードできることは保証しない**（要：警告/サンプリング/キャンセル等）

## 2. 共有プロトコル（Widget が準拠すべきこと）

### 2.1 保存先ディレクトリ解決

- 既定: `~/.allye_secrets/`
- 上書き: 環境変数 `ALLYE_SECRET_DIR`

Widget は Python 側と同一の解決規則に従って、以下のパスを利用します。

- manifest: `<secret_dir>/manifest.jsonl`
- lock: `<secret_dir>/manifest.lock`
- payload dir: `<secret_dir>/payloads/`

### 2.2 manifest.jsonl フォーマット（最低限扱うフィールド）

manifest は 1行=1 JSON オブジェクトの append-only ログです。Widget は **`status="ready"` の最新行**を採用します。

- `version`（int）: 現状 `1`
- `id`（str）: 転送単位UUID
- `table_name`（str）: 論理名（UI表示・指定キー）
- `producer`（str）: 例 `python` / `allye`
- `status`（str）: `writing` / `ready` / `error`
- `created_at`（str）: ISO風（例 `2025-12-18T12:34:56+0900`）
- `ttl_sec`（int|null）: 任意
- `payload`（object|null）: `ready` 時に必須
  - `transport`（str）: `file_arrow_ipc_v1` または `shm_arrow_stream_v1`
  - `locator`（str）:
    - `file_arrow_ipc_v1`: `.arrow` ファイルの絶対パス
    - `shm_arrow_stream_v1`: OS shared memory 名
  - `data_size`（int）: bytes
  - `shape`（[rows, cols]）
  - `schema_sha256`（str）: 簡易スキーマハッシュ
  - `created_at`（str）
  - `expires_at`（str|null）
- `metadata`（object）: 拡張用（UI表示用ヒント等）

### 2.3 ロック（同時書き込み/読み取りの前提）

- Writer（Transmitter）: `manifest.lock` を排他で取り、`manifest.jsonl` に追記する
- Reader（Receiver）: append-only を前提に **ロック無しで読んで良い**（ただし「行単位」で読むこと）

### 2.4 payload 形式（必須対応）

- `file_arrow_ipc_v1`
  - Arrow IPC “file” 形式（mmap可能）
  - 大容量は基本これを推奨
- `shm_arrow_stream_v1`
  - Arrow IPC “stream” 形式の bytes を shared memory に格納
  - 小容量向け（OS制約が強いので UI から優先度を下げるのを推奨）

## 3. allye_data_receiver（Python → Allye）の機能要件

### 3.1 基本機能（必須）

- manifest 監視・再読み込み
  - `manifest.jsonl` を読み、`status="ready"` のイベントからテーブル一覧を生成
  - 同名 `table_name` は「最後に現れた ready」を採用（最新版）
- テーブル選択UI
  - `table_name` 一覧表示
  - 付随情報表示: `producer` / `created_at` / `shape` / `bytes` / `transport`
- 受信（ロード）
  - 選択した `payload` を読み込み、Orange Table（または Allye canvas のテーブル形式）に変換して出力
  - `file_arrow_ipc_v1`: Arrow file をメモリマップで読み込めること
  - `shm_arrow_stream_v1`: shared memory を開いて bytes を読み、Arrow stream として復元できること
- エラー処理
  - `status="error"` の最新がある場合: UI にエラーとして表示（`metadata.error` があれば表示）
  - payload が見つからない/読めない場合: リトライボタン + 失敗理由の表示

### 3.2 大容量データ対応（強く推奨）

- ロード前見積もり
  - `bytes` / `shape` が閾値を超える場合、即時全件ロードせず警告を出す
- 部分ロード/サンプリング
  - 例: 先頭N行のみロード、またはランダムサンプル（UIで選択）
- キャンセル
  - ロード中にユーザが中断できる

### 3.3 互換性（推奨）

- `producer` フィルタ
  - 既定では全 producer を表示しつつ、`producer="python"` のみ等で絞り込める

## 4. allye_data_transmitter（Allye → Python）の機能要件

### 4.1 基本機能（必須）

- 入力: Allye canvas の Orange Table を受け取る
- 送信UI
  - `table_name` を指定（省略時は自動生成可）
  - `transport` を選択（`auto` / `file` / `shm`）
  - `Send` ボタン（明示的実行）
- 送信（書き込み）
  - `producer="allye"` を設定（推奨）
  - manifest へイベント追記（順序）
    1) `status="writing"` を追記
    2) payload を生成（Arrow IPC）
    3) `status="ready"` を追記（payload 情報付き）
  - payload の transport 選択
    - 既定: `file_arrow_ipc_v1`（大容量でも安全）
    - `shm_arrow_stream_v1` は「小さい時だけ」等で制限可能

### 4.3 再送・上書きポリシー（推奨）

- 同じ `table_name` で再送した場合は manifest 上は別イベントとして追記し、Receiver/Python は「最新 ready」を採用
- UI で「同名を上書き（最新版として送る）」の意味を明確化（＝追記は必ず行われる）

## 5. 受信側Python（allye_data_connector.get_dataframe）との整合

Python 側は以下の前提で動作します。

- `manifest.jsonl` の末尾から見て最新の `ready` を採用
- `table_name` 指定で取得し、`producer` 指定があれば一致するもののみ採用
- `payload.transport` に応じて Arrow IPC を復元して DataFrame 化

したがって Transmitter 側は、上記の `payload` 形式・値を必ず満たす必要があります。

## 6. 受け入れ基準（Acceptance Criteria）

### Receiver

- `manifest.jsonl` に `file_arrow_ipc_v1` の `ready` が追加されると、選択して Orange Table として取り込める
- `status="error"` を UI に明示できる
- payload 未発見/破損時に「失敗理由 + リトライ」ができる

### Transmitter

- Orange Table を送信すると、`manifest.jsonl` に `writing`→`ready` の2行が追加される
- Python 側 `get_dataframe(table_name, producer="allye")` で DataFrame として取得できる

## 7. 実装メモ（技術選定のヒント）

- Widget が Python 実装可能なら:
  - `pyarrow` で `ipc.open_file(memory_map(...))` / `ipc.open_stream(BufferReader(...))`
  - shared memory は Python 3.8+ の `multiprocessing.shared_memory` を利用
- Widget が JS/TS 実装なら:
  - `file_arrow_ipc_v1` を優先し、Arrow JS で IPC file を読む（shared memory は実装難度が上がる）
