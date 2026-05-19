# SlackChatDumper

Slack チャンネルのメッセージを（スレッド返信も含めて）JSON ファイルとしてエクスポートするツールです。
BOT が参照可能なチャンネルの一覧取得や、全チャンネル一括エクスポートにも対応しています。

## セットアップ

```bash
# 仮想環境を有効化
source .venv/bin/activate

# 依存パッケージをインストール
pip install -r requirements.txt
```

## 必要な権限（Bot Token Scopes）

`.env` の `SLACK_TOKEN` は Bot トークン（`xoxb-...`）を想定しています。用途に応じて以下のスコープを付与してください。

| やりたいこと | 必要なスコープ |
| --- | --- |
| 公開チャンネルの一覧・履歴 | `channels:read`, `channels:history` |
| 公開チャンネルへ BOT を自動参加（`--join-public`） | `channels:join` |
| 非公開チャンネルの一覧・履歴 | `groups:read`, `groups:history` |
| DM / グループDM（`--include-dms`） | `im:read`, `im:history`, `mpim:read`, `mpim:history` |
| ユーザー一覧（`--list-users`） | `users:read`（メール込みなら `users:read.email` も） |

> **メッセージ履歴の取得には BOT がそのチャンネルのメンバーである必要があります**（Slack の仕様）。
> - 既定の `--list` / `--all-channels` は **BOT が既にメンバーのチャンネル**（`users.conversations`）が対象です。
> - `--join-public` を付けると、ワークスペースの **全公開チャンネルに BOT を自動参加** させてからエクスポートします（`channels:join` スコープが必要）。
> - **非公開チャンネルは API で自己参加できません。** 対象にしたい場合は Slack 側で `/invite @<bot名>` してください。
>
> `--join-public` は各公開チャンネルに「○○が参加しました」というシステムメッセージが投稿され、メンバー一覧にも BOT が載ります。実行前に影響範囲を確認してください。
> また 2025 年以降に作成された一部アプリでは `conversations.history` / `conversations.replies` のレート制限が厳しいため、チャンネル数が多いと時間がかかることがあります（後述のとおり `429` は自動で待機・リトライし、送出ペースも自動調整します）。

## 設定

### `.env`

```
SLACK_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx
```

### `config.json`（任意）

単一チャンネルモードのデフォルト値です。無くても `--list` / `--all-channels` は動きます。

```json
{
  "channel_id": "C0123456789",
  "date": "2026-04-06"
}
```

## 使い方

### チャンネル一覧

```bash
# BOT が参照可能なチャンネルを一覧表示し result/channels.json に保存
python export_slack.py --list

# DM / グループDM も含める
python export_slack.py --list --include-dms
```

### ユーザー一覧

```bash
# ワークスペースの全ユーザーを result/users.json に保存（削除済み・BOT も含む）
python export_slack.py --list-users
```

メッセージ JSON の `user` フィールド（`U0123ABC456` 形式の ID）は置換しません。代わりに `users.json` を引いて誰の発言かを特定する想定です。`users:read.email` スコープがあれば `profile.email` も保存されます。

### 全チャンネルを一括エクスポート

`--list` で表示されるチャンネル（= BOT がメンバーのチャンネル）すべてが対象です。
チャンネルは 1 件ずつ直列に取得し、各チャンネル内のスレッド返信（`conversations.replies`）も直列に取得します（後述の理由で、並列にしてもレート枠が律速で速くならないため）。

```bash
# 参照可能な全チャンネルを「全期間・スレッド返信込み」でエクスポート
python export_slack.py --all-channels

# 中断後の再実行（既に出力 JSON があるチャンネルは飛ばす）
python export_slack.py --all-channels --skip-existing

# 増分取得：既存ファイルがあれば「保存済みの最新メッセージ以降」だけ取得して既存ぶんとマージ
python export_slack.py --all-channels --update

# 初期送出レート（req/分・メソッドごと）を明示。429 を踏んだら自動で半減していく
python export_slack.py --all-channels --start-rate 12

# 429 リトライ上限（0 = 無制限。各回 Retry-After ぶん待機）
python export_slack.py --all-channels --max-retries 0

# スレッド返信は取得しない / DM・グループDM も含める
python export_slack.py --all-channels --no-threads
python export_slack.py --all-channels --include-dms
```

#### 増分取得（`--update`）

`--update` を付けると、**既に出力 JSON があるチャンネルは「保存済みメッセージの中で最も新しい `ts` 以降」だけ**を `conversations.history` で取得し、既存ぶんとマージします（全期間モード時のみ有効）。出力 JSON が無いチャンネルは通常どおり全期間を取得します。単一チャンネルモード（`--all` / 日付 `all`）でも使えます。

- 同じ `ts` のメッセージは新しく取得したほうで上書きするので、境界付近の編集も拾えます。
- スレッド返信は**マージ後の全スレッド親について `conversations.replies` を取り直す**ので、既存スレッドへの新着返信も反映されます（＝大きな履歴ページネーションは省略しつつ、返信は最新化）。
- 出力はこれまでどおりアトミックに書き出すので、増分中に殺しても壊れた JSON は残りません。

> **限界**（これらが必要なときは `--update` なしでたまにフル再取得してください）
> - 前回取得より**古いメッセージの編集・削除**は反映されません（その範囲は再取得しないため）。
> - **古いメッセージに後からぶら下がった「新規スレッド」**（それまで返信 0 件だったメッセージへの初返信）は拾えません。
> - `--update` と `--skip-existing` を同時に指定した場合は `--update` が優先されます（既存ファイルはスキップせず増分更新）。

#### レート制限との付き合い方（重要）

Slack のレート制限は **「メソッド単位 × ワークスペース（トークン）単位」** で課されます。コネクション（＝並列数）を増やしても、同一メソッド（例: `conversations.history`）の合計スループットは増えません。並列数を上げ過ぎると、超過したぶんは `429` を生むだけでムダ撃ち・取りこぼしが増えます。

そこで本ツールは **「直列実行 ＋ 送出レートの適応制御」** という方針を取ります:

- **チャンネルもスレッド返信も 1 件ずつ直列に処理** します。並列にしてもレート枠が律速で全体スループットは変わらず、むしろ
  - 複数チャンネルの大型メッセージ群が同時に RAM に乗ってピークメモリが膨らむ
  - 同一メソッドの同時送出が増えて 429 を踏みやすくなる
  - 429 の `Retry-After` 待機ロスがそのぶん積み上がる
  - というデメリットだけが残ります（実際の運用ログでも、並列 3 のときに `conversations.replies` が 100 回以上 429 を踏み、待機が総時間の ~19% を占めるケースを観測）。
- **メソッドごとにクライアント側のトークンバケット**で送出ペースを律速します（ページネーションの 2 ページ目以降も対象）。
- `429` を踏むと、そのメソッドの**送出レートを 0.7 倍に減速**し、`Retry-After` ぶん待機します。**成功が続けば少しずつレートを上げます**（AIMD。実効レートが自動でレート枠付近に収束する＝「レート枠が許す最速」に近づく）。`--start-rate` は初期値（既定 30 req/分・メソッド毎、上限 60）。
- `429` は `Retry-After` に従って **（実質）無制限に再試行**します（`--max-retries`、既定 50、`0` で無制限）。
- それでも個別チャンネルが落ちることはあり得るので、その場合は **`--skip-existing` を付けて再実行**すれば未取得ぶんだけ拾えます（出力 JSON はアトミックに書き出すので、途中で殺しても壊れた JSON は残りません）。

> 実行中は `⏳ conversations.replies: レート制限(429)。送出レートを 60→42 req/分に下げ、~10s 待機します` のようなログが出ます。これは想定どおりの自動調整です。とくに 2025 年以降に作成された一部アプリは `conversations.history` / `conversations.replies` が「1 リクエスト/分」級に厳しく、そのぶん時間はかかりますが、`--skip-existing` で再開しながら最終的には全部取れます。

### 公開チャンネルへ自動参加してから一括エクスポート

ワークスペースの全公開チャンネルに BOT を参加させ、そのままエクスポートします（`channels:join` スコープが必要）。

```bash
# まず参加だけ実行して結果を確認（エクスポートはしない）
python export_slack.py --join-public

# 参加 ＋ 全公開チャンネルを「全期間・スレッド返信込み」でエクスポート
python export_slack.py --all-channels --join-public
```

### 単一チャンネル

```bash
# config.json のデフォルト値で実行
python export_slack.py

# チャンネルIDと日付を引数で指定
python export_slack.py <チャンネルID> <日付(YYYY-MM-DD)>

# 全期間のメッセージを取得（--all オプションまたは日付に all を指定）
python export_slack.py --all
python export_slack.py <チャンネルID> all
```

`config.json` の `date` を `"all"` に設定することでも全期間を取得できます。

## 出力

- `result/channels.json` … 対象チャンネル一覧（`--list` / `--all-channels` / `--join-public` 実行時）
- `result/slack_{チャンネルID}_{チャンネル名}_{日付 or all}.json` … 各チャンネルの会話

各チャンネルの JSON はおおよそ次の形です。スレッド親メッセージには `replies`（返信メッセージの配列）が付きます。

```json
{
  "channel": "C0123456789",
  "channel_name": "general",
  "is_private": false,
  "period": "all",
  "message_count": 123,
  "thread_count": 7,
  "reply_count": 25,
  "exported_at": "2026-05-12T12:00:00",
  "messages": [
    { "ts": "...", "text": "...", "thread_ts": "...", "reply_count": 3, "replies": [ { "ts": "...", "text": "..." } ] }
  ]
}
```

## 運用取得をワンコマンドで（`update.sh`）

`users.json` 更新 → 全チャンネル増分取得 → SQLite 反映、をまとめて実行します。ログは `logs/update-YYYYMMDD-HHMMSS.log` に保存されます。

```bash
# 通常運用（増分取得 = --update）
./update.sh

# 初回 or 定期フル取得（全期間を取り直す）
./update.sh fresh
```

中で実行しているのは以下と同等です。

```bash
python export_slack.py --list-users
python export_slack.py --all-channels --update     # fresh のときは --update なし
python import_to_sqlite.py
```

## SQLite に取り込んで集計する

`result/` 配下の JSON を SQLite に流し込み、SQL で横断検索・集計できるようにします。
**既存の JSON は書き換えません**。後段の取り込みスクリプト（`import_to_sqlite.py`）として独立しています。

```bash
# result/ 配下の slack_*.json / channels.json / users.json を全部取り込み、result/slack.db を作る
python import_to_sqlite.py

# 既存テーブルを DROP して作り直し（スキーマを変えたとき）
python import_to_sqlite.py --reset

# 特定のファイルだけ取り込み（--update で更新したぶんを反映、など）
python import_to_sqlite.py result/slack_C0123456789_general_all.json

# 出力先 DB を変える
python import_to_sqlite.py --db /tmp/slack.db
```

再実行は UPSERT で安全です（`messages` は `(channel_id, ts)` を主キーに更新、`reactions`/`files` は対象メッセージぶんを入れ替え）。

### スキーマ

| テーブル | 主キー | 主な列 |
| --- | --- | --- |
| `channels` | `id` | `name`, `is_private`, `is_im`, `is_mpim`, `period`, `exported_at`, `message_count`, `thread_count`, `reply_count`, `raw` |
| `users` | `id` | `name`, `real_name`, `display_name`, `email`, `is_bot`, `deleted`, `raw` |
| `messages` | `(channel_id, ts)` | `thread_ts`, `parent_ts`, `is_reply`, `user`, `type`, `subtype`, `text`, `reply_count`, `edited_ts`, `client_msg_id`, `raw` |
| `reactions` | `(channel_id, ts, name, user)` | （絵文字名と押したユーザーを 1 行ずつ展開） |
| `files` | `(channel_id, ts, file_id)` | `name`, `title`, `mimetype`, `filetype`, `size`, `user`, `url_private`, `permalink`, `raw` |

- スレッド返信は `messages` に同居し、`is_reply = 1` ・ `parent_ts = 親メッセージの ts` で区別します。スレッド単位の取り出しは `WHERE channel_id = ? AND (ts = :root OR parent_ts = :root)`。
- `raw` 列には元 JSON をそのまま保存しているので、スキーマに無い項目も `json_extract(raw, '$.path')` で取り出せます。

### クエリ例

```sql
-- ユーザー別の発言数 TOP10（BOT 除外）
SELECT u.real_name, u.name, COUNT(*) AS n
FROM messages m
JOIN users u ON u.id = m.user
WHERE u.is_bot = 0 AND u.deleted = 0
GROUP BY m.user
ORDER BY n DESC
LIMIT 10;

-- あるチャンネルのスレッドを 1 本ぶん時系列で取り出す
SELECT ts, user, text
FROM messages
WHERE channel_id = 'C0123456789'
  AND (ts = '1700000000.000100' OR parent_ts = '1700000000.000100')
ORDER BY ts;

-- 絵文字リアクションの利用ランキング
SELECT name, COUNT(*) AS n
FROM reactions
GROUP BY name
ORDER BY n DESC
LIMIT 20;
```
