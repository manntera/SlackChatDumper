# SlackChatDumper

Slack の公開チャンネルのメッセージを（スレッド返信も含めて）JSON にエクスポートし、SQLite に取り込んで SQL で横断検索・集計できるようにするツールです。

```
slackdump sync              # users → export（増分）→ import をまとめて実行（普段の運用はこれだけ）
slackdump sync --full       # 全期間を取り直す（初回 or 定期フル取得）

slackdump channels          # 参照可能な公開チャンネル一覧 → result/cache/channels.json
slackdump users             # 全ユーザー一覧 → result/cache/users.json
slackdump export            # 全チャンネルを増分エクスポート → result/cache/slack_*.json
slackdump export --full     # 全チャンネルをフルエクスポート
slackdump import            # result/cache/ の JSON → result/slack.db
```

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

`pip install -e .`（editable install）で `slackdump` コマンドが使えるようになります。どのディレクトリから実行しても、出力先（`result/`）や設定（`config.json` / `.env`）は常に **このリポジトリのルート** 基準で解決されます。

## 必要な権限（OAuth Scopes）

`.env` の `SLACK_TOKEN` には **User トークン（`xoxp-...`）** を設定します（未参加・アーカイブ済みの公開チャンネル履歴も読むため）。以下のスコープを **User Token Scopes** 側に付与してください。

| やりたいこと | 必要なスコープ |
| --- | --- |
| 公開チャンネルの一覧・履歴 | `channels:read`, `channels:history` |
| ユーザー一覧（`slackdump users`） | `users:read`（メール込みなら `users:read.email` も） |

> **公開チャンネルのみが取得対象です。** 非公開（プライベート）チャンネル・DM・グループDM は、自分が参加しているかどうかに関わらず取得しません。
>
> User トークンなら、公開チャンネルは **参加していなくても履歴を読めます**（アーカイブ済みを含む）。

### アーカイブ済みチャンネル（既定で含む）

`channels` / `export` は **既定でアーカイブ済みチャンネルも対象に含めます**。除外したいときだけ `--exclude-archived` を付けてください。

## 設定

### `.env`

```
SLACK_TOKEN=xoxp-xxxxxxxxxxxx-xxxxxxxxxxxx
```

### `config.json`（任意）

一括エクスポート時の除外チャンネルを設定します。無くても動きます。

```json
{
  "exclude_channels": ["log-ci", "#alerts", "C0XXXXXXX"]
}
```

- `exclude_channels` … `channels` / `export` の対象から外すチャンネル。**チャンネル名**（先頭 `#` の有無・大文字小文字は無視）か **チャンネルID** で指定します。ログ出力専用チャンネルなどを除外する用途です。除外されたチャンネルは `🚫 除外 N 件 …` として表示されるので、指定が効いているか確認できます。

## 普段の運用（`slackdump sync`）

`users.json` 更新 → 全チャンネル増分取得 → SQLite 反映、をまとめて実行します。出力は画面に表示されると同時に `logs/sync-YYYYMMDD-HHMMSS.log` にも保存されます。

```bash
# 通常運用（増分取得）
slackdump sync

# 初回 or 定期フル取得（全期間を取り直す）
slackdump sync --full
```

中で実行しているのは以下と同等です。

```bash
slackdump users
slackdump export            # --full のときはフル取得
slackdump import
```

## エクスポートの仕組み

### 増分取得（`export` の既定動作）

既に出力した JSON があるチャンネルは、保存済みメッセージの中で最も新しい ts 以降だけを取得して既存ぶんとマージします。既存ファイルが無いチャンネルは全期間を取得します。

スレッド返信（`conversations.replies`）は全スレッドを取り直すと増分でもフル取得と同じ時間がかかるため、

1. 今回新しく取れたスレッド親
2. 既存スレッドのうち最終返信が直近 `--reply-refresh-days` 日以内のもの（デフォルト 7 日）

だけを取り直し、それ以外は保存済みの返信を流用します。

> **仕様上の限界**: 前回取得より古いメッセージの「編集・削除」、古いメッセージに後からぶら下がった「新規スレッド」、および `--reply-refresh-days` 日より長く沈黙していたスレッドへの新着返信は増分では反映されません。たまに `slackdump sync --full` でフル再取得してください。

### レート制限への対応（自動）

Slack のレート制限は「メソッド単位 × ワークスペース単位」で課されるため、並列化はせず、メソッドごとのクライアント側トークンバケットで送出レートを自動制御します（AIMD: 429 を踏んだら 0.7 倍に減速して Retry-After ぶん待機、成功が続けば +2 req/分ずつ回復）。429 は自動でリトライするので、放置しておけば完走します。

実行中は 30 秒ごとに 📊 進捗行（設定/実効レート・429 回数・取得中チャンネル）が、終了時にはメソッド別サマリが出力されます。

途中で中断・失敗しても、もう一度 `slackdump export` を実行すれば増分で続きから拾えます。

## SQLite に取り込んで集計する

`result/cache/` 配下の JSON を `result/slack.db` に流し込みます。**既存の JSON は書き換えません**。

```bash
# result/cache/ 配下の slack_*.json / channels.json / users.json を全部取り込む
slackdump import

# 既存テーブルを DROP して作り直し（スキーマを変えたとき）
slackdump import --reset

# 特定のファイルだけ取り込み
slackdump import result/cache/slack_C0123456789_general_all.json

# 出力先 DB を変える
slackdump import --db /tmp/slack.db
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

## コード構成

```
slackdump/
  cli.py        # サブコマンド定義とディスパッチ（エントリポイント）
  config.py     # パス解決（リポジトリルート基準）と config.json / .env の読み込み
  ratelimit.py  # AIMD レートリミッタ付き WebClient・進捗レポータ
  export.py     # チャンネル/ユーザー列挙・全期間/増分エクスポート
  importer.py   # JSON → SQLite 取り込み（スキーマ定義もここ）
  sync.py       # users → export → import の一括実行＋ログ保存
  util.py       # スレッドセーフな print 等
```

## ディレクトリ構成（出力）

```
result/
  slack.db                     # 成果物（SQLite）
  cache/                       # Slack から取り直せる生ダンプ（landing zone）
    channels.json
    users.json
    slack_<ID>_<name>_all.json
logs/
  sync-YYYYMMDD-HHMMSS.log     # slackdump sync の実行ログ
```
