"""Slack のチャンネル一覧取得と、メッセージ（スレッド返信込み）の JSON エクスポート。

使い方の例:
    python export_slack.py                         # config.json のデフォルト値で単一チャンネルを取得
    python export_slack.py <channel_id> <date>     # 日付(YYYY-MM-DD)指定
    python export_slack.py <channel_id> all        # 全期間
    python export_slack.py --all                   # config のチャンネルを全期間
    python export_slack.py --list                  # BOT が参照可能なチャンネル一覧
    python export_slack.py --all-channels          # 参照可能な全チャンネルを全期間エクスポート
    python export_slack.py --all-channels --skip-existing  # 取得済みは飛ばす（中断後の再開）
    python export_slack.py --all-channels --include-dms    # DM / グループDM も含める
    python export_slack.py --all-channels --no-threads     # スレッド返信は取得しない
    python export_slack.py --all-channels --workers 6      # 並列ワーカー数（デフォルト 3）
    python export_slack.py --all-channels --start-rate 12  # 初期送出レート(req/分・メソッド毎)
    python export_slack.py --join-public           # 全公開チャンネルに BOT を自動参加（要 channels:join）
    python export_slack.py --all-channels --join-public   # 全公開チャンネルに参加してエクスポート

レート制限と並列取得について:
    Slack のレート制限は「メソッド単位 × ワークスペース単位」で課されます。コネクション（並列数）
    を増やしても同一メソッドの合計スループットは増えません。そこで本ツールは:
      - メソッドごとに「クライアント側のトークンバケット」で送出レートを制御し、
      - 429 を踏んだら送出レートを半減＋Retry-After ぶん全員待機、成功が続けば少しずつ増やす
        （AIMD。実効レートが自動でレート枠付近に収束する）、
      - 429 は Retry-After に従って（実質）無制限に再試行する（--max-retries で上限変更）、
      - --all-channels はチャンネルとスレッド返信(conversations.replies)を ThreadPoolExecutor で
        並列に取得する（往復レイテンシの隠蔽と history / replies の同時進行のため。--workers で調整）。
    つまり「並列数を上げる」ではなく「送出レートを適応制御」して、レート枠が許す範囲で最速に近づけます。
    途中で落ちても --skip-existing を付けて再実行すれば未取得ぶんだけ拾えます。
"""

import argparse
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import ConnectionErrorRetryHandler

load_dotenv()

RESULT_DIR = "result"
DEFAULT_WORKERS = 3
DEFAULT_START_RATE_PER_MIN = 24.0   # メソッドあたりの初期送出レート（req/分）
MIN_RATE_PER_MIN = 1.0              # これ以上は下げない（実際の待機は Retry-After が担保）
MAX_RATE_PER_MIN = 120.0            # これ以上は上げない
DEFAULT_MAX_RETRIES = 50            # 1 リクエストあたりの 429 リトライ上限（0 = 無制限）

# 並列実行時に print の行が混ざらないようにするためのロック
_print_lock = threading.Lock()


def log(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# --------------------------------------------------------------------------- #
# 適応レートリミッタ（メソッドごとに 1 個 / AIMD）
# --------------------------------------------------------------------------- #
class _MethodLimiter:
    """1 メソッド分のトークンバケット。429 で送出レートを半減、成功が続けば少しずつ増やす。"""

    INCREASE_AFTER = 8       # この回数だけ連続成功したらレートを上げる
    INCREASE_FACTOR = 1.1
    DECREASE_FACTOR = 0.5

    def __init__(self, rate_per_sec, min_rate, max_rate, capacity=2.0):
        self.rate = rate_per_sec
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.blocked_until = 0.0       # 429 を踏んだら Retry-After ぶんここまで全員待つ
        self.success_streak = 0
        self._cv = threading.Condition()

    def acquire(self):
        """1 リクエストぶんのトークンを取れるまで（必要なら）待つ。"""
        with self._cv:
            while True:
                now = time.monotonic()
                if now < self.blocked_until:
                    self._cv.wait(timeout=self.blocked_until - now)
                    continue
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                self._cv.wait(timeout=max(0.01, (1.0 - self.tokens) / self.rate))

    def on_success(self):
        with self._cv:
            self.success_streak += 1
            if self.success_streak >= self.INCREASE_AFTER and self.rate < self.max_rate:
                self.success_streak = 0
                self.rate = min(self.max_rate, self.rate * self.INCREASE_FACTOR)

    def on_rate_limited(self, retry_after):
        """429 を受けた。減速したら (old_rate, new_rate)、既に減速＆待機中なら None を返す。"""
        with self._cv:
            now = time.monotonic()
            already_waiting = now < self.blocked_until
            self.blocked_until = max(self.blocked_until, now + max(1.0, retry_after))
            # 待機が明けた時点で 1 リクエストぶんだけ即送れるようにしておく（Retry-After で
            # バケツが回復する想定）。それ以上は新レートで貯まるので過剰なバーストにはならない。
            self.tokens = min(self.capacity, 1.0)
            self.updated = now
            self.success_streak = 0
            self._cv.notify_all()
            if already_waiting:        # 同時多発の 429 で何重にも半減させない
                return None
            old = self.rate
            self.rate = max(self.min_rate, self.rate * self.DECREASE_FACTOR)
            return old, self.rate

    def rate_per_min(self):
        with self._cv:
            return self.rate * 60.0


# --------------------------------------------------------------------------- #
# ペーシング付き WebClient
# --------------------------------------------------------------------------- #
class PacedWebClient(WebClient):
    """全 HTTP リクエストの直前にメソッド単位のレートリミッタを通し、429 は Retry-After に
    従って自前で待ち、（実質）無制限に再試行する。

    conversations.history / conversations.replies などのカーソルページネーション（SlackResponse
    のイテレート）も同じ低レベルメソッド _perform_urllib_http_request を通るので、まとめて対象になる。
    """

    def __init__(self, *args, start_rate_per_sec, min_rate_per_sec, max_rate_per_sec,
                 max_retries, **kwargs):
        super().__init__(*args, **kwargs)
        self._start_rate = start_rate_per_sec
        self._min_rate = min_rate_per_sec
        self._max_rate = max_rate_per_sec
        self._max_retries = max_retries  # 0 = 無制限
        self._limiters = {}
        self._limiters_lock = threading.Lock()

    def _limiter(self, method_name):
        with self._limiters_lock:
            lim = self._limiters.get(method_name)
            if lim is None:
                lim = _MethodLimiter(self._start_rate, self._min_rate, self._max_rate)
                self._limiters[method_name] = lim
            return lim

    @staticmethod
    def _retry_after_seconds(headers):
        for k, v in (headers or {}).items():
            if str(k).lower() == "retry-after":
                try:
                    return max(1.0, float(v))
                except (TypeError, ValueError):
                    break
        return 5.0

    # NOTE: slack_sdk の内部メソッドをフックしている（3.x で安定。requirements.txt で版を固定）。
    def _perform_urllib_http_request(self, *, url, args):
        method_name = url.rsplit("/", 1)[-1]
        lim = self._limiter(method_name)
        attempt = 0
        while True:
            attempt += 1
            lim.acquire()
            resp = super()._perform_urllib_http_request(url=url, args=args)
            status = resp.get("status")
            if status == 429:
                wait = self._retry_after_seconds(resp.get("headers"))
                changed = lim.on_rate_limited(wait)
                if changed is not None:
                    old, new = changed
                    log(f"⏳ {method_name}: レート制限(429)。送出レートを "
                        f"{old * 60:.0f}→{new * 60:.0f} req/分に下げ、~{wait:.0f}s 待機します")
                if self._max_retries and attempt > self._max_retries:
                    log(f"❌ {method_name}: 429 が {self._max_retries} 回続いたため断念します")
                    return resp  # 上位で SlackApiError 化される
                continue
            if isinstance(status, int) and 200 <= status < 300:
                lim.on_success()
            return resp

    def limiter_summary(self):
        with self._limiters_lock:
            return {name: lim.rate_per_min() for name, lim in self._limiters.items()}


# --------------------------------------------------------------------------- #
# セットアップ
# --------------------------------------------------------------------------- #
def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def build_client(start_rate_per_min, max_retries):
    token = os.getenv("SLACK_TOKEN")
    if not token:
        print("❌ SLACK_TOKEN が設定されていません (.env を確認してください)")
        raise SystemExit(1)
    start = min(MAX_RATE_PER_MIN, max(MIN_RATE_PER_MIN, start_rate_per_min)) / 60.0
    client = PacedWebClient(
        token=token,
        start_rate_per_sec=start,
        min_rate_per_sec=MIN_RATE_PER_MIN / 60.0,
        max_rate_per_sec=MAX_RATE_PER_MIN / 60.0,
        max_retries=max(0, int(max_retries)),
    )
    # ネットワーク系の一時エラーは slack_sdk 側でリトライ（429 は PacedWebClient が担当）
    client.retry_handlers.append(ConnectionErrorRetryHandler(max_retry_count=3))
    return client


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "channel"


def output_path(channel, period):
    channel_id = channel["id"]
    name = channel.get("name") or channel_id
    stem = f"slack_{channel_id}" + (f"_{safe_filename(name)}" if name != channel_id else "")
    return os.path.join(RESULT_DIR, f"{stem}_{period}.json")


# --------------------------------------------------------------------------- #
# Slack API 呼び出し（SlackResponse はイテレートすると自動でページネーションされる）
# --------------------------------------------------------------------------- #
def list_channels(client, types):
    """BOT 自身がメンバーになっている（＝履歴を読める）会話の一覧。

    conversations.list はワークスペースの全公開チャンネルを返してしまうため、
    「BOT が確認可能なチャンネル」は users.conversations を使う。
    """
    def _fetch(t):
        out = []
        for page in client.users_conversations(types=t, limit=200, exclude_archived=True):
            out.extend(page["channels"])
        return out

    try:
        channels = _fetch(types)
    except SlackApiError as e:
        # スコープ不足で一部の種別が取れないときは public_channel だけにフォールバック
        if e.response.get("error") == "missing_scope" and types != "public_channel":
            print(f"⚠️  {e.response.get('needed')} スコープが無いため public_channel のみを対象にします")
            channels = _fetch("public_channel")
        else:
            raise
    channels.sort(key=lambda c: c.get("name") or c.get("id", ""))
    return channels


def list_all_public_channels(client):
    channels = []
    for page in client.conversations_list(types="public_channel", limit=200, exclude_archived=True):
        channels.extend(page["channels"])
    channels.sort(key=lambda c: c.get("name") or c.get("id", ""))
    return channels


def join_public_channels(client):
    """ワークスペースの全公開チャンネルを列挙し、未参加のものに BOT を参加させる。

    参加に成功した（または元から参加済みの）チャンネルだけを返す。
    """
    channels = list_all_public_channels(client)
    to_join = [c for c in channels if not c.get("is_member")]
    if not to_join:
        print(f"➕ 公開チャンネル {len(channels)} 件すべてに参加済みです")
        return channels

    print(f"➕ 未参加の公開チャンネル {len(to_join)} 件に BOT を参加させます ...")
    for i, c in enumerate(to_join, 1):
        name = c.get("name", c["id"])
        try:
            client.conversations_join(channel=c["id"])
            c["is_member"] = True
            print(f"  [{i}/{len(to_join)}] joined #{name}")
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "missing_scope":
                print(f"❌ チャンネル参加には channels:join スコープが必要です "
                      f"(必要: {e.response.get('needed')} / 現在: {e.response.get('provided')})")
                print("   Slack App 設定で channels:join を追加し、ワークスペースに再インストールしてください。")
                raise SystemExit(1)
            print(f"  [{i}/{len(to_join)}] ⚠️ #{name} 参加失敗: {err}")
    return [c for c in channels if c.get("is_member")]


def fetch_history(client, channel_id, oldest=None, latest=None):
    kwargs = {"channel": channel_id, "limit": 200}
    if oldest is not None:
        kwargs["oldest"] = oldest
    if latest is not None:
        kwargs["latest"] = latest
    messages = []
    for page in client.conversations_history(**kwargs):
        messages.extend(page["messages"])
    return messages


def fetch_replies(client, channel_id, thread_ts):
    replies = []
    for page in client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200):
        replies.extend(page["messages"])
    # conversations.replies は先頭に親メッセージを含むので除外する
    return replies[1:] if replies else []


def attach_threads(client, channel_id, messages, reply_executor=None):
    """history で得たメッセージのうちスレッド親に、replies を付与する。

    reply_executor が渡されればスレッドごとの conversations.replies を並列に取得する。
    """
    parents = [m for m in messages
               if m.get("ts") and m.get("thread_ts") == m["ts"] and m.get("reply_count", 0) > 0]
    if not parents:
        return 0, 0

    if reply_executor is None:
        replies_list = [fetch_replies(client, channel_id, m["ts"]) for m in parents]
    else:
        replies_list = list(reply_executor.map(
            lambda m: fetch_replies(client, channel_id, m["ts"]), parents))

    reply_total = 0
    for msg, replies in zip(parents, replies_list):
        msg["replies"] = replies
        reply_total += len(replies)
    return len(parents), reply_total


def get_channel_info(client, channel_id):
    try:
        return client.conversations_info(channel=channel_id)["channel"]
    except SlackApiError:
        return {"id": channel_id}


# --------------------------------------------------------------------------- #
# エクスポート
# --------------------------------------------------------------------------- #
def export_channel(client, channel, oldest=None, latest=None, period="all",
                   with_threads=True, reply_executor=None):
    """1 チャンネル分を取得して JSON に書き出す。完了行（文字列）を返す。"""
    channel_id = channel["id"]
    name = channel.get("name") or channel_id

    messages = fetch_history(client, channel_id, oldest, latest)
    thread_count = reply_total = 0
    if with_threads:
        thread_count, reply_total = attach_threads(client, channel_id, messages, reply_executor)

    payload = {
        "channel": channel_id,
        "channel_name": channel.get("name"),
        "is_private": channel.get("is_private"),
        "period": period,
        "message_count": len(messages),
        "thread_count": thread_count,
        "reply_count": reply_total,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }

    os.makedirs(RESULT_DIR, exist_ok=True)
    path = output_path(channel, period)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 中断時に壊れた JSON を残さない（--skip-existing の判定も安全に）

    detail = f" / スレッド {thread_count} 件・返信 {reply_total} 件" if with_threads else ""
    return f"✅ #{name} ({channel_id}): メッセージ {len(messages)} 件{detail} → {path}"


def export_all_channels(client, channels, with_threads=True, workers=DEFAULT_WORKERS,
                        skip_existing=False):
    """複数チャンネルを並列にエクスポートする。

    - チャンネル単位を workers 本のスレッドプールで並列実行
    - 各チャンネル内のスレッド返信取得も、全チャンネル共有の workers 本のプールで並列実行
      （プールを分けることでネスト時のデッドロックを避けつつ、同時 API 呼び出しを ~2*workers に抑える）
    - 実際の送出ペースは PacedWebClient のメソッド単位レートリミッタが律速する
    """
    if skip_existing:
        pending = [c for c in channels if not os.path.exists(output_path(c, "all"))]
        n_skipped = len(channels) - len(pending)
        if n_skipped:
            print(f"⏭️  既に取得済みの {n_skipped} 件はスキップします")
        channels = pending
        if not channels:
            print("✅ 取得対象はすべて取得済みです")
            return

    total = len(channels)
    note = "（スレッド返信込み）" if with_threads else "（スレッド返信なし）"
    print(f"\n🚀 {total} 件のチャンネルを全期間エクスポートします{note} / 並列 {workers}")
    print("   送出レートは 429 を踏むと自動で半減・成功が続けば微増します（クライアント側ペーシング）")

    reply_executor = (ThreadPoolExecutor(max_workers=workers, thread_name_prefix="replies")
                      if with_threads else None)
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="channel") as pool:
            futures = {
                pool.submit(export_channel, client, c, period="all",
                            with_threads=with_threads, reply_executor=reply_executor): c
                for c in channels
            }
            for fut in as_completed(futures):
                c = futures[fut]
                done += 1
                cname = c.get("name", c["id"])
                try:
                    summary = fut.result()
                except SlackApiError as e:
                    log(f"⚠️  [{done}/{total}] #{cname} をスキップ: {e.response.get('error')}")
                except Exception as e:  # noqa: BLE001 - 1 チャンネルの失敗で全体を止めない
                    log(f"⚠️  [{done}/{total}] #{cname} で予期しないエラー: {type(e).__name__}: {e}")
                else:
                    log(f"[{done}/{total}] {summary}")
    finally:
        if reply_executor is not None:
            reply_executor.shutdown(wait=True)

    rates = client.limiter_summary()
    if rates:
        print("📈 最終的な送出レート: " + ", ".join(f"{m} {r:.0f}/分" for m, r in sorted(rates.items())))
    print("🎉 完了しました（スキップが出た場合は --skip-existing を付けて再実行できます）")


def dump_channel_list(channels):
    os.makedirs(RESULT_DIR, exist_ok=True)
    path = os.path.join(RESULT_DIR, "channels.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)
    print(f"📋 BOT が参照可能なチャンネル {len(channels)} 件:")
    for c in channels:
        if c.get("is_im") or c.get("is_mpim"):
            mark = "💬"
        elif c.get("is_private"):
            mark = "🔒"
        else:
            mark = "＃ "
        members = c.get("num_members")
        members = f"  ({members}名)" if members is not None else ""
        print(f"  {mark} {c.get('name', c['id']):<32} {c['id']}{members}")
    print(f"→ 一覧を {path} に保存しました")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args():
    config = load_config()
    default_channel = config.get("channel_id", "")
    default_date = config.get("date", datetime.now().strftime("%Y-%m-%d"))

    p = argparse.ArgumentParser(description="Slack チャンネルの一覧取得・メッセージ（スレッド込み）エクスポート")
    p.add_argument("channel_id", nargs="?", default=default_channel,
                   help=f"チャンネルID (デフォルト: {default_channel or '未設定'})")
    p.add_argument("date", nargs="?", default=default_date,
                   help=f"取得する日付 YYYY-MM-DD または all (デフォルト: {default_date})")
    p.add_argument("--all", action="store_true", help="全期間のメッセージを取得する")
    p.add_argument("--list", dest="list_only", action="store_true",
                   help="BOT が参照可能なチャンネル一覧を表示し result/channels.json に保存する")
    p.add_argument("--all-channels", dest="all_channels", action="store_true",
                   help="参照可能な全チャンネルを全期間エクスポートする")
    p.add_argument("--join-public", dest="join_public", action="store_true",
                   help="ワークスペースの全公開チャンネルに BOT を自動参加させてから対象にする")
    p.add_argument("--no-threads", dest="no_threads", action="store_true",
                   help="スレッド返信を取得しない")
    p.add_argument("--include-dms", dest="include_dms", action="store_true",
                   help="DM / グループDM も対象に含める（--join-public とは併用不可）")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="出力 JSON が既にあるチャンネルはスキップする（中断後の再実行に便利）")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"並列ワーカー数（チャンネル並列・スレッド返信並列の各プールの本数 / デフォルト: {DEFAULT_WORKERS}）")
    p.add_argument("--start-rate", type=float, default=DEFAULT_START_RATE_PER_MIN, dest="start_rate",
                   help=("メソッドごとの初期送出レート（req/分）。429 を踏むと自動で半減し、成功が"
                         f"続けば最大 {MAX_RATE_PER_MIN:.0f}/分まで増える（デフォルト: {DEFAULT_START_RATE_PER_MIN:.0f}）"))
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, dest="max_retries",
                   help=("1 リクエストあたりの 429 リトライ上限（0 = 無制限）。各回 Retry-After ぶん"
                         f"待機する（デフォルト: {DEFAULT_MAX_RETRIES}）"))
    return p.parse_args()


def main():
    args = parse_args()
    client = build_client(args.start_rate, args.max_retries)
    with_threads = not args.no_threads
    workers = max(1, args.workers)
    types = "public_channel,private_channel" + (",im,mpim" if args.include_dms else "")

    try:
        if args.list_only or args.all_channels or args.join_public:
            if args.join_public:
                if args.include_dms:
                    print("⚠️  --join-public 指定時は --include-dms は無視されます（公開チャンネルのみ対象）")
                channels = join_public_channels(client)
            else:
                channels = list_channels(client, types)
            dump_channel_list(channels)

            if args.all_channels:
                export_all_channels(client, channels, with_threads=with_threads,
                                    workers=workers, skip_existing=args.skip_existing)
            elif args.join_public:
                print("\nℹ️  参加だけ完了しました。エクスポートも行うには --all-channels を付けて再実行してください。")
            return

        # --- 単一チャンネルモード（従来挙動） ---
        if not args.channel_id:
            print("❌ チャンネルIDを指定してください (引数 / config.json / --all-channels / --list)")
            raise SystemExit(1)

        if args.all or args.date == "all":
            oldest = latest = None
            period = "all"
        else:
            dt = datetime.strptime(args.date, "%Y-%m-%d")
            oldest = dt.replace(hour=0, minute=0, second=0).timestamp()
            latest = dt.replace(hour=23, minute=59, second=59).timestamp()
            period = args.date

        channel = get_channel_info(client, args.channel_id)
        if args.skip_existing and os.path.exists(output_path(channel, period)):
            print(f"⏭️  既に取得済みです: {output_path(channel, period)}")
            return

        reply_executor = (ThreadPoolExecutor(max_workers=workers, thread_name_prefix="replies")
                          if with_threads else None)
        try:
            log(export_channel(client, channel, oldest=oldest, latest=latest,
                               period=period, with_threads=with_threads,
                               reply_executor=reply_executor))
        finally:
            if reply_executor is not None:
                reply_executor.shutdown(wait=True)

    except SlackApiError as e:
        err = e.response.get("error")
        msg = f"❌ Slack API エラー: {err}"
        if err == "missing_scope":
            msg += f" (必要: {e.response.get('needed')} / 現在: {e.response.get('provided')})"
        print(msg)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
