#!/usr/bin/env bash
# Slack の運用取得をワンコマンドで:
#   1. users.json を更新
#   2. 全チャンネルを増分取得（--update）、`fresh` 指定時はフル取得
#   3. result/slack.db に取り込み
#
# Usage:
#   ./update.sh           # 通常運用（増分取得）
#   ./update.sh fresh     # 全期間を取り直す（初回 or 定期フル取得）
set -euo pipefail

# このスクリプトは script/ 配下。リポジトリルート（script/ の親）で実行し、
# .venv / result / logs / config.json / .env をルート基準で扱う。
cd "$(dirname "$0")/.."

mode="${1:-update}"
case "$mode" in
    update) export_args=(--all-channels --update) ;;
    fresh)  export_args=(--all-channels) ;;
    *) echo "Usage: $0 [update|fresh]" >&2; exit 2 ;;
esac

mkdir -p logs
log_file="logs/update-$(date +%Y%m%d-%H%M%S).log"

# tee 経由だと Python の stdout がブロックバッファ化して進捗が出なくなるので unbuffered に
export PYTHONUNBUFFERED=1

# 以降の標準出力/標準エラーを画面とログの両方へ
exec > >(tee -a "$log_file") 2>&1

echo "=== $(date -Iseconds) start (mode=$mode) ==="

# shellcheck disable=SC1091
source .venv/bin/activate

python script/export_slack.py --list-users
python script/export_slack.py "${export_args[@]}"
python script/import_to_sqlite.py

echo "=== $(date -Iseconds) done ==="
echo "log: $log_file"
