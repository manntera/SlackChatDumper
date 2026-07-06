"""パス解決と設定読み込み。

このパッケージは editable install（`pip install -e .`）でリポジトリ内に置かれる前提。
result/ や config.json / .env はリポジトリルート（パッケージの親ディレクトリ）基準で
解決するため、どのディレクトリから実行しても同じ場所を読み書きする。
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]

RESULT_DIR = BASE_DIR / "result"
# 生の Slack ダンプ(JSON)は成果物(slack.db)と分けて result/cache/ に置く。
# cache/ 配下は Slack から取り直せる landing zone、result 直下の slack.db が成果物。
CACHE_DIR = RESULT_DIR / "cache"
DB_PATH = RESULT_DIR / "slack.db"
LOG_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.json"


def load_config() -> dict:
    """config.json を読む。無ければ空 dict（現在使うキーは exclude_channels のみ）。"""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def slack_token() -> str | None:
    load_dotenv(BASE_DIR / ".env")
    return os.getenv("SLACK_TOKEN")
