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
チャンネルは並列に取得し、各チャンネル内のスレッド返信（`conversations.replies`）も並列に取得します（既定 3 並列）。

```bash
# 参照可能な全チャンネルを「全期間・スレッド返信込み」でエクスポート
python export_slack.py --all-channels

# 中断後の再実行（既に出力 JSON があるチャンネルは飛ばす）
python export_slack.py --all-channels --skip-existing

# 増分取得：既存ファイルがあれば「保存済みの最新メッセージ以降」だけ取得して既存ぶんとマージ
python export_slack.py --all-channels --update

# 並列ワーカー数を指定（チャンネル並列・スレッド返信並列の各プールの本数）
python export_slack.py --all-channels --workers 6

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

そこで本ツールは「並列数を上げる」のではなく **「送出レートを適応制御」** します:

- **メソッドごとにクライアント側のトークンバケット**で送出ペースを律速します（ページネーションの 2 ページ目以降も対象）。
- `429` を踏むと、そのメソッドの**送出レートを半減**し、`Retry-After` ぶん全ワーカーが待機します。**成功が続けば少しずつレートを上げます**（AIMD。実効レートが自動でレート枠付近に収束する＝「レート枠が許す最速」に近づく）。`--start-rate` は初期値（既定 24 req/分・メソッド毎、上限 120）。
- `429` は `Retry-After` に従って **（実質）無制限に再試行**します（`--max-retries`、既定 50、`0` で無制限）。
- それでも個別チャンネルが落ちることはあり得るので、その場合は **`--skip-existing` を付けて再実行**すれば未取得ぶんだけ拾えます（出力 JSON はアトミックに書き出すので、途中で殺しても壊れた JSON は残りません）。
- `--workers` は「往復レイテンシの隠蔽」と「`history` と `replies` の同時進行」のためのもの。レートはリミッタが律速するので、3〜4 もあれば十分で、増やしても基本的に速くなりません。`--workers` は単一チャンネルモードのスレッド返信取得にも適用されます。

> 実行中は `⏳ conversations.replies: レート制限(429)。送出レートを 24→12 req/分に下げ、~30s 待機します` のようなログが出ます。これは想定どおりの自動調整です。最後に `📈 最終的な送出レート: ...` が出るので、そのワークスペース／アプリのレート枠の目安になります。とくに 2025 年以降に作成された一部アプリは `conversations.history` / `conversations.replies` が「1 リクエスト/分」級に厳しく、その場合は全体が直列に近い動きになります（それでも `--skip-existing` で再開しながら最終的には全部取れます）。

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
