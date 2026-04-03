import requests
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()
SLACK_TOKEN = os.getenv("SLACK_TOKEN")

# config.json を読み込む
with open("config.json", "r") as f:
    config = json.load(f)

default_channel = config.get("channel_id", "")
default_date    = config.get("date", datetime.now().strftime("%Y-%m-%d"))

parser = argparse.ArgumentParser(description="Slack チャンネルのメッセージをJSONで保存")
parser.add_argument("channel_id", nargs="?", default=default_channel, help=f"チャンネルID (デフォルト: {default_channel})")
parser.add_argument("date",       nargs="?", default=default_date,    help=f"取得する日付 (デフォルト: {default_date})")
args = parser.parse_args()

if not args.channel_id:
    print("❌ チャンネルIDを指定してください (config.json の channel_id または引数)")
    exit(1)

CHANNEL_ID  = args.channel_id
TARGET_DATE = args.date

dt     = datetime.strptime(TARGET_DATE, "%Y-%m-%d")
oldest = dt.replace(hour=0,  minute=0,  second=0).timestamp()
latest = dt.replace(hour=23, minute=59, second=59).timestamp()

messages, cursor = [], None

while True:
    params = {"channel": CHANNEL_ID, "oldest": oldest, "latest": latest, "limit": 200}
    if cursor:
        params["cursor"] = cursor

    res = requests.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        params=params,
    ).json()

    if not res.get("ok"):
        print(f"❌ エラー: {res.get('error')}")
        exit(1)

    messages.extend(res.get("messages", []))
    cursor = res.get("response_metadata", {}).get("next_cursor")
    if not cursor:
        break

output = {"channel": CHANNEL_ID, "date": TARGET_DATE, "messages": messages}
os.makedirs("result", exist_ok=True)
filename = f"result/slack_{CHANNEL_ID}_{TARGET_DATE}.json"

with open(filename, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"✅ {len(messages)}件のメッセージを {filename} に保存しました")
