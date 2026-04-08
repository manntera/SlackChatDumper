# SlackChatDumper

Slack チャンネルのメッセージを JSON ファイルとしてエクスポートするツールです。

## セットアップ

```bash
# 仮想環境を有効化
source .venv/bin/activate

# 依存パッケージをインストール
pip install -r requirements.txt
```

## 設定

### `.env`

Slack の Bot トークンを設定します。

```
SLACK_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx
```

### `config.json`

デフォルトのチャンネルIDと日付を設定します。

```json
{
  "channel_id": "C0123456789",
  "date": "2026-04-06"
}
```

## 使い方

```bash
# config.json のデフォルト値で実行
python export_slack.py

# チャンネルIDと日付を引数で指定
python export_slack.py <チャンネルID> <日付(YYYY-MM-DD)>
```

## 出力

`result/slack_{チャンネルID}_{日付}.json` に、指定日のメッセージが JSON 形式で保存されます。
