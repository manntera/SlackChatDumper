# SlackChatDumper

Slack チャンネルのメッセージを（スレッド返信も含めて）JSON ファイルとしてエクスポートするツールです。
公開チャンネルの一覧取得や、全チャンネル一括エクスポートにも対応しています。

## セットアップ

```bash
# 仮想環境を有効化
source .venv/bin/activate

# 依存パッケージをインストール
pip install -r requirements.txt
```

## 必要な権限（OAuth Scopes）

`.env` の `SLACK_TOKEN` には **User トークン（`xoxp-...`）** を設定します（未参加・アーカイブ済みの公開チャンネル履歴も読むため）。以下のスコープを **User Token Scopes** 側に付与してください。

| やりたいこと | 必要なスコープ |
| --- | --- |
| 公開チャンネルの一覧・履歴 | `channels:read`, `channels:history` |
| ユーザー一覧（`--list-users`） | `users:read`（メール込みなら `users:read.email` も） |

> **公開チャンネルのみが取得対象です。** 非公開（プライベート）チャンネル・DM・グループDM は、自分が参加しているかどうかに関わらず取得しません。`--list` / `--all-channels` の列挙からも除外され、チャンネルIDを直接指定しても取得を拒否します。

> User トークンなら、公開チャンネルは **参加していなくても履歴を読めます**（アーカイブ済みを含む）。

### アーカイブ済みチャンネル（既定で含む）

`--list` / `--all-channels` は **既定でアーカイブ済みチャンネルも対象に含めます**。除外したいときだけ `--exclude-archived` を付けてください。

- 一覧は `conversations.list` でワークスペースの公開チャンネルを参加有無に関わらず列挙します。**アーカイブ済みかつ未参加の公開チャンネル** の履歴も、User トークンならそのまま読めます。
- **非公開（プライベート）チャンネルは対象外です**（上記のとおり、参加有無に関わらず取得しません）。

> 2025 年以降に作成された一部アプリでは `conversations.history` / `conversations.replies` のレート制限が厳しいため、チャンネル数が多いと時間がかかることがあります（後述のとおり `429` は自動で待機・リトライし、送出ペースも自動調整します）。

## 設定

### `.env`

```
SLACK_TOKEN=xoxp-xxxxxxxxxxxx-xxxxxxxxxxxx
```

### `config.json`（任意）

単一チャンネルモードのデフォルト値と、一括エクスポート時の除外チャンネルを設定します。無くても `--list` / `--all-channels` は動きます。

```json
{
  "channel_id": "C0123456789",
  "date": "2026-04-06",
  "exclude_channels": ["log-ci", "#alerts", "C0XXXXXXX"]
}
```

- `exclude_channels` … `--list` / `--all-channels` の対象から外すチャンネル。**チャンネル名**（先頭 `#` の有無・大文字小文字は無視）か **チャンネルID** で指定します。ログ出力専用チャンネルなどを除外する用途です。
  - `--list` 実行時は、除外されたチャンネルを `🚫 除外 N 件 …` として表示するので、指定が効いているか確認できます。
  - 単一チャンネルモード（チャンネルIDを直接指定）には適用されません（明示指定が優先）。

## 使い方

### チャンネル一覧

```bash
# 参照可能な公開チャンネルを一覧表示し result/cache/channels.json に保存（アーカイブ済みも含む）
python script/export_slack.py --list

# アーカイブ済みを除外して一覧表示
python script/export_slack.py --list --exclude-archived
```

### ユーザー一覧

```bash
# ワークスペースの全ユーザーを result/cache/users.json に保存（削除済み・BOT も含む）
python script/export_slack.py --list-users
```

メッセージ JSON の `user` フィールド（`U0123ABC456` 形式の ID）は置換しません。代わりに `users.json` を引いて誰の発言かを特定する想定です。`users:read.email` スコープがあれば `profile.email` も保存されます。

### 全チャンネルを一括エクスポート

`--list` で表示されるチャンネル（= ワークスペースの公開チャンネル）すべてが対象です。
**アーカイブ済みチャンネルも既定で含まれます。** 除外したいときは `--exclude-archived` を付けます（前述の「アーカイブ済みチャンネル」節を参照）。
チャンネルは 1 件ずつ直列に取得し、各チャンネル内のスレッド返信（`conversations.replies`）も直列に取得します（後述の理由で、並列にしてもレート枠が律速で速くならないため）。

```bash
# 参照可能な全チャンネルを「全期間・スレッド返信込み」でエクスポート
python script/export_slack.py --all-channels

# 中断後の再実行（既に出力 JSON があるチャンネルは飛ばす）
python script/export_slack.py --all-channels --skip-existing

# 増分取得：既存ファイルがあれば「保存済みの最新メッセージ以降」だけ取得して既存ぶんとマージ
python script/export_slack.py --all-channels --update

# 初期送出レート（req/分・メソッドごと）を明示。429 を踏んだら自動で半減していく
python script/export_slack.py --all-channels --start-rate 12

# 429 リトライ上限（0 = 無制限。各回 Retry-After ぶん待機）
python script/export_slack.py --all-channels --max-retries 0

# スレッド返信は取得しない / アーカイブ済みを除外する
python script/export_slack.py --all-channels --no-threads
python script/export_slack.py --all-channels --exclude-archived
```

#### 増分取得（`--update`）

`--update` を付けると、**既に出力 JSON があるチャンネルは「保存済みメッセージの中で最も新しい `ts` 以降」だけ**を `conversations.history` で取得し、既存ぶんとマージします（全期間モード時のみ有効）。出力 JSON が無いチャンネルは通常どおり全期間を取得します。単一チャンネルモード（`--all` / 日付 `all`）でも使えます。

- 同じ `ts` のメッセージは新しく取得したほうで上書きするので、境界付近の編集も拾えます。
- スレッド返信（`conversations.replies`）は、**全スレッド親を取り直すと増分でも全取得と同じ時間がかかる**ため（`replies` の呼び出し回数は `conversations.history` のページ数を桁違いに上回ります）、**(1) 今回新しく取れたスレッド親**と、**(2) 既存スレッド親のうち最終返信（`latest_reply`）が直近 `--reply-refresh-days` 日以内のもの**だけ取り直し、それ以外は保存済みの返信をそのまま流用します。
  - `--reply-refresh-days` のデフォルトは `7`。`0` を指定すると新規スレッドのみ（最速）、十分大きな値を指定すると全スレッド再取得相当になります。
  - 完了行に `replies 再取得 N・流用 M` を出すので、どれだけ省略できたか確認できます。
- 出力はこれまでどおりアトミックに書き出すので、増分中に殺しても壊れた JSON は残りません。

> **限界**（これらが必要なときは `--update` なしでたまにフル再取得してください）
> - 前回取得より**古いメッセージの編集・削除**は反映されません（その範囲は再取得しないため）。
> - **古いメッセージに後からぶら下がった「新規スレッド」**（それまで返信 0 件だったメッセージへの初返信）は拾えません。
> - **`--reply-refresh-days` 日より長く沈黙していたスレッドへの新着返信**は反映されません（窓の外なので `replies` を取り直さないため）。沈黙しがちなスレッドも拾いたいときは値を大きくしてください。
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


### 単一チャンネル

```bash
# config.json のデフォルト値で実行
python script/export_slack.py

# チャンネルIDと日付を引数で指定
python script/export_slack.py <チャンネルID> <日付(YYYY-MM-DD)>

# 全期間のメッセージを取得（--all オプションまたは日付に all を指定）
python script/export_slack.py --all
python script/export_slack.py <チャンネルID> all
```

`config.json` の `date` を `"all"` に設定することでも全期間を取得できます。

## 出力

生の Slack ダンプ（JSON）は成果物と分けて `result/cache/` に置きます。`result/cache/` 配下は Slack から取り直せる landing zone、解析で使う成果物は `result/slack.db`（→ [SQLite に取り込んで集計する](#sqlite-に取り込んで集計する)）です。

- `result/cache/channels.json` … 対象チャンネル一覧（`--list` / `--all-channels` 実行時）
- `result/cache/users.json` … ワークスペースのユーザー一覧（`--list-users` 実行時）
- `result/cache/slack_{チャンネルID}_{チャンネル名}_{日付 or all}.json` … 各チャンネルの会話

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

`users.json` 更新 → 全チャンネル増分取得 → SQLite 反映、をまとめて実行します。ログは `logs/update-YYYYMMDD-HHMMSS.log` に保存されます。アーカイブ済みチャンネルも含めて取得します（既定の挙動）。

```bash
# 通常運用（増分取得 = --update）
./script/update.sh

# 初回 or 定期フル取得（全期間を取り直す）
./script/update.sh fresh
```

中で実行しているのは以下と同等です。

```bash
python script/export_slack.py --list-users
python script/export_slack.py --all-channels --update     # fresh のときは --update なし
python script/import_to_sqlite.py
```

## SQLite に取り込んで集計する

`result/cache/` 配下の JSON を SQLite に流し込み、SQL で横断検索・集計できるようにします。
**既存の JSON は書き換えません**。後段の取り込みスクリプト（`import_to_sqlite.py`）として独立しています。

```bash
# result/cache/ 配下の slack_*.json / channels.json / users.json を全部取り込み、result/slack.db を作る
python script/import_to_sqlite.py

# 既存テーブルを DROP して作り直し（スキーマを変えたとき）
python script/import_to_sqlite.py --reset

# 特定のファイルだけ取り込み（--update で更新したぶんを反映、など）
python script/import_to_sqlite.py result/cache/slack_C0123456789_general_all.json

# 出力先 DB を変える
python script/import_to_sqlite.py --db /tmp/slack.db
```

再実行は UPSERT で安全です（`messages` は `(channel_id, ts)` を主キーに更新、`reactions`/`files` は対象メッセージぶんを入れ替え）。

### スキーマ

| テーブル | 主キー | 主な列 |
| --- | --- | --- |
| `channels` | `id` | `name`, `is_private`, `is_archived`, `period`, `exported_at`, `message_count`, `thread_count`, `reply_count`, `raw` |
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
