"""Slack のチャンネル一覧取得と、メッセージ（スレッド返信込み）の JSON エクスポート。

使い方の例:
    python export_slack.py                         # config.json のデフォルト値で単一チャンネルを取得
    python export_slack.py <channel_id> <date>     # 日付(YYYY-MM-DD)指定
    python export_slack.py <channel_id> all        # 全期間
    python export_slack.py --all                   # config のチャンネルを全期間
    python export_slack.py --list                  # BOT が参照可能なチャンネル一覧
    python export_slack.py --list-users            # ワークスペースの全ユーザー一覧 (要 users:read[, users:read.email])
    python export_slack.py --all-channels          # 参照可能な全チャンネルを全期間エクスポート
    python export_slack.py --all-channels --skip-existing  # 取得済みは飛ばす（中断後の再開）
    python export_slack.py --all-channels --update         # 既存ファイルがあれば新規ぶんだけ取得してマージ（増分取得）
    python export_slack.py --all-channels --include-dms    # DM / グループDM も含める
    python export_slack.py --all-channels --no-threads     # スレッド返信は取得しない
    python export_slack.py --all-channels --start-rate 12  # 初期送出レート(req/分・メソッド毎)
    python export_slack.py --all-channels --max-rate 120  # 送出レート上限(req/分・メソッド毎)を引き上げて速度を探る
    python export_slack.py --all-channels --progress-interval 10  # 10 秒ごとに進捗・レート・429 を出力（0 で無効）
    python export_slack.py --join-public           # 全公開チャンネルに BOT を自動参加（要 channels:join）
    python export_slack.py --all-channels --join-public   # 全公開チャンネルに参加してエクスポート

レート制限と取得方針について:
    Slack のレート制限は「メソッド単位 × ワークスペース単位」で課されます。コネクション（並列数）
    を増やしても同一メソッドの合計スループットは増えません。そこで本ツールは:
      - メソッドごとに「クライアント側のトークンバケット」で送出レートを制御し、
      - 429 を踏んだら送出レートを 0.7 倍に下げ＋Retry-After ぶん待機、成功が続けば加算的に
        少しずつ上げる（AIMD: multiplicative-decrease / additive-increase。実効レートが自動で
        レート枠付近に収束する）、
      - 429 は Retry-After に従って（実質）無制限に再試行する（--max-retries で上限変更）、
      - --all-channels はチャンネルを 1 件ずつ直列に処理する（並列にしてもレート枠が律速で
        速くならず、429 とメモリピークだけが増えるため）。スレッド返信(conversations.replies)も
        各チャンネル内で直列に取得する。
    途中で落ちても --skip-existing を付けて再実行すれば未取得ぶんだけ拾えます。
    実行中は --progress-interval 秒ごとに「設定/実効レート・429 回数・取得中チャンネル」を、各チャンネル
    完了時にそのチャンネルの所要時間と API 呼び出し回数を、終了時にメソッド別サマリ（リクエスト数・
    429 率・スロットル待機時間・平均実効レート）と簡単なボトルネック診断を出力します。これを見ながら
    --start-rate / --max-rate を詰めると、ダウンロード速度の上限が掴めます。

増分取得（--update）について:
    既に出力した JSON があるチャンネルは、保存済みメッセージの中で最も新しい ts 以降だけを
    conversations.history で取得して既存ぶんとマージします（全期間モード時のみ有効）。スレッド返信は
    マージ後の全スレッド親について再取得するので、既存スレッドへの新着返信も拾えます。
    既存ファイルが無いチャンネルは通常どおり全期間を取得します。
    ※ 仕様上の限界: 前回取得より古いメッセージの「編集・削除」、および古いメッセージに後から
      ぶら下がった「新規スレッド」は反映されません。これらも正確に取りたい時は --update なしで
      たまにフル再取得してください。
"""

import argparse
import json
import os
import re
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import ConnectionErrorRetryHandler

load_dotenv()

RESULT_DIR = "result"
DEFAULT_START_RATE_PER_MIN = 30.0   # メソッドあたりの初期送出レート（req/分）
MIN_RATE_PER_MIN = 1.0              # これ以上は下げない（実際の待機は Retry-After が担保）
# AIMD の上限。実測ログでは conversations.replies は 60 を超えると 429 を頻発する。上げすぎると
# 429→Retry-After 待機のロスが増えてむしろ遅くなるので、控えめに 60。--max-rate で実行時に変更可。
DEFAULT_MAX_RATE_PER_MIN = 60.0
DEFAULT_MAX_RETRIES = 50            # 1 リクエストあたりの 429 リトライ上限（0 = 無制限）

# StatusReporter（別スレッド）と main の print が混ざらないようにするためのロック
_print_lock = threading.Lock()


def log(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


def _fmt_secs(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


# --------------------------------------------------------------------------- #
# 適応レートリミッタ（メソッドごとに 1 個 / AIMD）
# --------------------------------------------------------------------------- #
class _MethodLimiter:
    """1 メソッド分のトークンバケット。AIMD で送出レートを適応制御する。

    429 を踏んだら乗算的に下げ（multiplicative decrease）、成功が続いたら加算的に少しずつ
    上げる（additive increase）。乗算増加だと「底から天井まで指数的に駆け上がって 429 で叩き
    落とされる」のこぎり波で実効レートが伸びないので、加算増加で天井付近に滑らかに収束させる。
    """

    INCREASE_AFTER = 3                   # この回数だけ連続成功したらレートを 1 段上げる
    INCREASE_STEP_PER_SEC = 2.0 / 60.0   # 1 段の増加幅（= +2 req/分）
    DECREASE_FACTOR = 0.7                # 429 時の減速率（半減だと落としすぎ＆復帰が遅い）

    def __init__(self, rate_per_sec, min_rate, max_rate, capacity=2.0):
        self.rate = rate_per_sec
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.blocked_until = 0.0       # 429 を踏んだら Retry-After ぶんここまで全員待つ
        self.success_streak = 0
        # --- 計測用カウンタ（ログ・サマリ用） ---
        self.req_count = 0             # 送出したリクエスト数（429 含む全試行）
        self.ok_count = 0              # 2xx で返ってきた数
        self.rl_count = 0              # 429 を踏んだ数
        self.throttle_events = 0       # スロットル（待機）に入った回数
        self.throttle_wait_total = 0.0 # Retry-After 待機の累計秒（おおよそのロス時間）
        self.pace_wait_total = 0.0     # トークン待ち（自前ペーシング）の累計スレッド秒
        self._cv = threading.Condition()

    def acquire(self):
        """1 リクエストぶんのトークンを取れるまで（必要なら）待つ。"""
        with self._cv:
            t0 = time.monotonic()
            try:
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
            finally:
                self.pace_wait_total += time.monotonic() - t0

    def note_response(self, status):
        """HTTP 1 回ぶんの結果を記録する（レート制御とは別の純粋な計測）。"""
        with self._cv:
            self.req_count += 1
            if isinstance(status, int) and 200 <= status < 300:
                self.ok_count += 1
            elif status == 429:
                self.rl_count += 1

    def on_success(self):
        with self._cv:
            self.success_streak += 1
            if self.success_streak >= self.INCREASE_AFTER and self.rate < self.max_rate:
                self.success_streak = 0
                self.rate = min(self.max_rate, self.rate + self.INCREASE_STEP_PER_SEC)

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
            self.throttle_events += 1
            self.throttle_wait_total += max(1.0, retry_after)
            old = self.rate
            self.rate = max(self.min_rate, self.rate * self.DECREASE_FACTOR)
            return old, self.rate

    def rate_per_min(self):
        with self._cv:
            return self.rate * 60.0

    def stats(self):
        """現在のスナップショット（ログ・サマリ用）。"""
        with self._cv:
            return {
                "rate_per_min": self.rate * 60.0,
                "req": self.req_count,
                "ok": self.ok_count,
                "rl": self.rl_count,
                "throttle_events": self.throttle_events,
                "throttle_wait_total": self.throttle_wait_total,
                "pace_wait_total": self.pace_wait_total,
                "blocked_now": time.monotonic() < self.blocked_until,
            }


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
            lim.note_response(status)
            if status == 429:
                wait = self._retry_after_seconds(resp.get("headers"))
                changed = lim.on_rate_limited(wait)
                if changed is not None:
                    old, new = changed
                    st = lim.stats()
                    log(f"⏳ {method_name}: レート制限(429)。送出レートを "
                        f"{old * 60:.0f}→{new * 60:.0f} req/分に下げ、~{wait:.0f}s 待機します "
                        f"［この間 429 計 {st['rl']} 回 / リクエスト計 {st['req']} 回］")
                if self._max_retries and attempt > self._max_retries:
                    log(f"❌ {method_name}: 429 が {self._max_retries} 回続いたため断念します")
                    return resp  # 上位で SlackApiError 化される
                continue
            if isinstance(status, int) and 200 <= status < 300:
                lim.on_success()
            else:
                # 429 以外のエラー応答もログに出す（速度より診断目的）
                log(f"⚠️  {method_name}: HTTP {status} が返りました（リトライは slack_sdk 側に委譲）")
            return resp

    def limiter_summary(self):
        with self._limiters_lock:
            return {name: lim.rate_per_min() for name, lim in self._limiters.items()}

    def method_stats(self):
        """メソッド名 → stats() の dict（ログ・最終サマリ用）。"""
        with self._limiters_lock:
            items = list(self._limiters.items())
        return {name: lim.stats() for name, lim in items}


# --------------------------------------------------------------------------- #
# 進捗レポータ（一定間隔でレート・実効スループット・429 状況を出力）
# --------------------------------------------------------------------------- #
class _StatusReporter(threading.Thread):
    """interval 秒ごとに、メソッド別の「設定レート / 直近の実効レート / 429 回数」と
    処理中チャンネル・全体進捗を 1 行で出す。ダウンロード速度のボトルネック観測用。

    直列実行なので「処理中のチャンネル」は常に 0 or 1 件。current() は現在処理中の
    チャンネル名（無ければ None）を返す callable。
    """

    def __init__(self, client, interval, current, progress):
        super().__init__(daemon=True, name="status")
        self.client = client
        self.interval = interval
        self.current = current          # callable -> str | None
        self.progress = progress        # callable -> (done, total)
        # NOTE: 属性名 _stop は Thread の内部メソッドと衝突するので避ける
        self._stop_evt = threading.Event()
        self._started_at = time.monotonic()
        self._last = {}                 # method -> (ok, rl, req) の前回値
        self._last_at = self._started_at

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.wait(self.interval):
            try:
                self._report()
            except Exception as e:  # noqa: BLE001 - 観測スレッドが落ちても本処理は止めない
                log(f"⚠️  進捗レポータでエラー: {type(e).__name__}: {e}")

    def _report(self):
        now = time.monotonic()
        dt = max(1e-6, now - self._last_at)
        self._last_at = now
        snap = self.client.method_stats()
        if not snap:
            return
        parts = []
        for m, s in sorted(snap.items()):
            p_ok, p_rl, p_req = self._last.get(m, (0, 0, 0))
            self._last[m] = (s["ok"], s["rl"], s["req"])
            eff = (s["ok"] - p_ok) * 60.0 / dt          # 直近区間の成功 req/分
            sent = (s["req"] - p_req) * 60.0 / dt       # 直近区間の送出 req/分（429 込み）
            d_rl = s["rl"] - p_rl
            seg = f"{m} 設定{s['rate_per_min']:.0f} 実効{eff:.0f}/分"
            if sent - eff >= 1:
                seg += f"(送出{sent:.0f})"
            if d_rl:
                seg += f" ⚠️429×{d_rl}"
            if s["blocked_now"]:
                seg += " ⏸待機中"
            parts.append(seg)
        done, total = self.progress()
        current = self.current()
        current_str = f"#{current}" if current else "-"
        elapsed = _fmt_secs(now - self._started_at)
        log(f"📊 [{elapsed}] 進捗 {done}/{total} ｜ "
            + " ｜ ".join(parts)
            + f" ｜ 取得中: {current_str}")


# --------------------------------------------------------------------------- #
# セットアップ
# --------------------------------------------------------------------------- #
def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def build_client(start_rate_per_min, max_retries, max_rate_per_min=DEFAULT_MAX_RATE_PER_MIN):
    token = os.getenv("SLACK_TOKEN")
    if not token:
        print("❌ SLACK_TOKEN が設定されていません (.env を確認してください)")
        raise SystemExit(1)
    max_rate = max(MIN_RATE_PER_MIN, float(max_rate_per_min))
    start = min(max_rate, max(MIN_RATE_PER_MIN, start_rate_per_min)) / 60.0
    client = PacedWebClient(
        token=token,
        start_rate_per_sec=start,
        min_rate_per_sec=MIN_RATE_PER_MIN / 60.0,
        max_rate_per_sec=max_rate / 60.0,
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
# 増分取得（--update）まわり
# --------------------------------------------------------------------------- #
def load_existing_messages(path):
    """既存の出力 JSON があれば messages 配列を返す。無い・壊れている場合は []。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    msgs = data.get("messages")
    return msgs if isinstance(msgs, list) else []


def newest_message_ts(messages):
    """トップレベルメッセージの中で最も新しい ts（文字列）。無ければ None。"""
    best = None
    for m in messages:
        ts = m.get("ts")
        if not ts:
            continue
        if best is None or float(ts) > float(best):
            best = ts
    return best


def merge_messages(old_messages, new_messages):
    """ts をキーに新旧をマージ（同 ts は新を採用）。Slack の history と同じ「新しい順」で返す。

    NOTE: 入力 list は pop で消費するため呼び出し後は空になる。巨大チャンネルで「old+new+結果」
    の三重持ちを避けるため、要素を順次 by_ts に移し替えてから sort する。
    """
    by_ts = {}
    while old_messages:
        m = old_messages.pop()
        ts = m.get("ts")
        if ts:
            by_ts[ts] = m
    while new_messages:
        m = new_messages.pop()
        ts = m.get("ts")
        if ts:
            by_ts[ts] = m
    return sorted(by_ts.values(), key=lambda m: float(m["ts"]), reverse=True)


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


def list_users(client):
    """ワークスペースの全ユーザーを users.list で取得する（削除済み・BOT も含む）。

    members は cursor pagination されるので SlackResponse をイテレートして連結する。
    削除済みユーザーは過去メッセージの user ID 解決のために残しておく。
    """
    users = []
    for page in client.users_list(limit=200):
        users.extend(page["members"])
    # 有効ユーザーが先、その中は名前順。削除済みは末尾にまとめる。
    users.sort(key=lambda u: (bool(u.get("deleted")), (u.get("name") or u.get("id", "")).lower()))
    return users


def fetch_history(client, channel_id, oldest=None, latest=None, inclusive=False):
    # limit は 1 ページあたりの取得件数（最大 999）。大きくするほどページ数＝API 呼び出し数が減る。
    kwargs = {"channel": channel_id, "limit": 999}
    if oldest is not None:
        kwargs["oldest"] = oldest
    if latest is not None:
        kwargs["latest"] = latest
    if inclusive:
        kwargs["inclusive"] = True
    messages = []
    pages = 0
    for page in client.conversations_history(**kwargs):
        pages += 1
        messages.extend(page["messages"])
    return messages, pages  # pages = conversations.history の API 呼び出し回数


def fetch_replies(client, channel_id, thread_ts):
    replies = []
    for page in client.conversations_replies(channel=channel_id, ts=thread_ts, limit=999):
        replies.extend(page["messages"])
    # conversations.replies は先頭に親メッセージを含むので除外する
    return replies[1:] if replies else []


def attach_threads(client, channel_id, messages):
    """history で得たメッセージのうちスレッド親に、replies を付与する（直列取得）。

    並列にしてもレート枠が律速するので速くはならず、429 と一時メモリだけが増える。
    """
    parents = [m for m in messages
               if m.get("ts") and m.get("thread_ts") == m["ts"] and m.get("reply_count", 0) > 0]
    if not parents:
        return 0, 0

    reply_total = 0
    for msg in parents:
        replies = fetch_replies(client, channel_id, msg["ts"])
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
                   with_threads=True, incremental=False):
    """1 チャンネル分を取得して JSON に書き出す。完了行（文字列）を返す。

    incremental=True かつ period=="all" なら、既存 JSON があれば保存済みの最新 ts 以降だけを
    取得して既存ぶんとマージする（スレッド返信はマージ後の全スレッド親を再取得して最新化）。
    """
    channel_id = channel["id"]
    name = channel.get("name") or channel_id
    t_start = time.monotonic()
    return _export_channel_inner(client, channel, channel_id, name, oldest, latest, period,
                                 with_threads, incremental, t_start)


def _export_channel_inner(client, channel, channel_id, name, oldest, latest, period,
                          with_threads, incremental, t_start):
    path = output_path(channel, period)

    existing = load_existing_messages(path) if (incremental and period == "all") else []
    resume_ts = newest_message_ts(existing) if existing else None
    if resume_ts is not None:
        # resume_ts そのものも取り直して（境界メッセージの編集・新規返信を拾うため）ts で重複排除
        oldest, latest, fetch_inclusive = resume_ts, None, True
    else:
        fetch_inclusive = False

    new_messages, hist_pages = fetch_history(client, channel_id, oldest, latest,
                                             inclusive=fetch_inclusive)
    new_count = len(new_messages)
    if existing:
        messages = merge_messages(existing, new_messages)
        del existing, new_messages  # merge_messages が中身を pop し尽くした空 list
    else:
        messages = new_messages
        del new_messages

    thread_count = reply_total = replies_calls = 0
    if with_threads:
        thread_count, reply_total = attach_threads(client, channel_id, messages)
        replies_calls = thread_count  # conversations.replies の呼び出し回数（大きいスレッドは ≥1）
    else:
        # 既存ファイルに replies が残っていれば件数だけ拾っておく
        for m in messages:
            r = m.get("replies")
            if r:
                thread_count += 1
                reply_total += len(r)

    msg_count = len(messages)  # json.dump 後に messages を解放するため先に取っておく
    payload = {
        "channel": channel_id,
        "channel_name": channel.get("name"),
        "is_private": channel.get("is_private"),
        "period": period,
        "message_count": msg_count,
        "thread_count": thread_count,
        "reply_count": reply_total,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }

    os.makedirs(RESULT_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 中断時に壊れた JSON を残さない（--skip-existing の判定も安全に）
    # 巨大チャンネルだと完了行を組み立てている間も RAM に居座るので、ここで明示的に手放す。
    del payload, messages

    elapsed = time.monotonic() - t_start
    api_calls = hist_pages + replies_calls
    msg_per_s = msg_count / elapsed if elapsed > 0 else 0.0
    detail = f" / スレッド {thread_count} 件・返信 {reply_total} 件" if with_threads else ""
    api = (f" ｜ {_fmt_secs(elapsed)} / API {api_calls} 回(history {hist_pages}＋replies {replies_calls})"
           f" / {msg_per_s:.0f} msg/s")
    if resume_ts is not None:
        return (f"🔄 #{name} ({channel_id}): 新規 {new_count} 件 → 合計 {msg_count} 件"
                f"{detail}{api} → {path}")
    return f"✅ #{name} ({channel_id}): メッセージ {msg_count} 件{detail}{api} → {path}"


def export_all_channels(client, channels, with_threads=True, skip_existing=False,
                        incremental=False, progress_interval=30.0):
    """複数チャンネルを直列にエクスポートする。

    - 1 チャンネルずつ順番に処理する（並列にしてもレート枠が律速で速度は変わらず、
      429 とメモリピークだけが増えるため）
    - 実際の送出ペースは PacedWebClient のメソッド単位レートリミッタが律速する
    - incremental=True なら既存 JSON があるチャンネルは新規ぶんだけ取得してマージする
    - progress_interval 秒ごとに送出レート・実効スループット・429 状況を出力（0 で無効）
    """
    if skip_existing and incremental:
        print("ℹ️  --update 指定時は --skip-existing は無視します（既存ファイルは増分更新します）")
        skip_existing = False
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
    if incremental:
        n_update = sum(1 for c in channels if os.path.exists(output_path(c, "all")))
        mode = f"増分取得（既存 {n_update} 件は新規ぶんのみ・残り {total - n_update} 件は全期間）"
    else:
        mode = "全期間エクスポート"
    print(f"\n🚀 {total} 件のチャンネルを{mode}します{note} / 直列実行")
    print(f"   送出レート: 初期 {client._start_rate * 60:.0f}/分・上限 {client._max_rate * 60:.0f}/分"
          f"（メソッド毎）。429 で 0.7 倍に減速＋Retry-After ぶん待機、成功が続けば +{_MethodLimiter.INCREASE_STEP_PER_SEC * 60:.0f}/分ずつ復帰（AIMD）")
    if progress_interval > 0:
        print(f"   {progress_interval:.0f} 秒ごとに 📊 進捗行（設定/実効レート・429・取得中チャンネル）を出します")

    state = {"done": 0, "current": None}
    reporter = None
    if progress_interval > 0:
        reporter = _StatusReporter(client, progress_interval,
                                   lambda: state["current"],
                                   lambda: (state["done"], total))
        reporter.start()

    t_wall0 = time.monotonic()
    ok = err = 0
    try:
        for c in channels:
            cname = c.get("name", c["id"])
            state["current"] = cname
            try:
                summary = export_channel(client, c, period="all",
                                         with_threads=with_threads,
                                         incremental=incremental)
            except SlackApiError as e:
                err += 1
                state["done"] += 1
                log(f"⚠️  [{state['done']}/{total}] #{cname} をスキップ: {e.response.get('error')}")
            except Exception as e:  # noqa: BLE001 - 1 チャンネルの失敗で全体を止めない
                err += 1
                state["done"] += 1
                log(f"⚠️  [{state['done']}/{total}] #{cname} で予期しないエラー: {type(e).__name__}: {e}")
            else:
                ok += 1
                state["done"] += 1
                log(f"[{state['done']}/{total}] {summary}")
            finally:
                state["current"] = None
    finally:
        if reporter is not None:
            reporter.stop()
            reporter.join(timeout=2.0)

    wall = time.monotonic() - t_wall0
    print(f"\n📊 メソッド別の最終サマリ（{_fmt_secs(wall)} 経過 / 成功 {ok} 件・失敗 {err} 件）:")
    stats = client.method_stats()
    for m, s in sorted(stats.items()):
        avg = s["ok"] / wall * 60.0 if wall > 0 else 0.0
        ratio = (s["rl"] / s["req"] * 100.0) if s["req"] else 0.0
        print(f"   {m}: リクエスト {s['req']} 回（成功 {s['ok']} / 429 {s['rl']} = {ratio:.1f}%）"
              f" / 平均実効 {avg:.0f}/分・最終 {s['rate_per_min']:.0f}/分"
              f" / スロットル {s['throttle_events']} 回・待機計 {_fmt_secs(s['throttle_wait_total'])}"
              f" / ペーシング待ち計 {_fmt_secs(s['pace_wait_total'])}")
    if stats:
        # ボトルネック診断のヒント
        worst = max(stats.items(), key=lambda kv: kv[1]["req"])
        ws = worst[1]
        hint = []
        if ws["rl"] / max(1, ws["req"]) > 0.15:
            hint.append(f"{worst[0]} の 429 率が高め → --max-rate / --start-rate を下げると待機ロスが減るかも")
        if ws["throttle_wait_total"] > wall * 0.2:
            hint.append("スロットル待機が総時間の 2 割超 → レート枠が真のボトルネック。別 App/別トークンでの並走が有効")
        if not hint:
            hint.append("429 率は低め＆送出レートが --max-rate に張り付いているなら --max-rate を上げると更に速くなる可能性")
        print("   💡 " + " / ".join(hint))
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


def dump_user_list(users):
    """users.list の結果を result/users.json に保存し、サマリを表示する。

    メッセージ JSON の `user` フィールド（U0123ABC456 形式）をこの users.json で引けば
    誰の発言か特定できる。出力は users.list の生のメンバー配列をそのまま保持する。
    """
    os.makedirs(RESULT_DIR, exist_ok=True)
    path = os.path.join(RESULT_DIR, "users.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

    active = [u for u in users if not u.get("deleted")]
    bots = [u for u in active if u.get("is_bot")]
    print(f"📋 ユーザー {len(users)} 件"
          f"（有効 {len(active)} / うち BOT {len(bots)} / 削除済み {len(users) - len(active)}）:")
    for u in users:
        if u.get("deleted"):
            mark = "❌"
        elif u.get("is_bot"):
            mark = "🤖"
        else:
            mark = "👤"
        profile = u.get("profile") or {}
        real = u.get("real_name") or profile.get("real_name") or ""
        display = profile.get("display_name") or ""
        email = profile.get("email") or ""
        label_parts = [s for s in (real, f"@{display}" if display else "", email) if s]
        label = " / ".join(label_parts) if label_parts else (u.get("name") or "")
        print(f"  {mark} {u['id']:<12} {label}")
    print(f"→ 一覧を {path} に保存しました（メッセージ JSON の `user` フィールドはここから引けます）")
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
    p.add_argument("--list-users", dest="list_users", action="store_true",
                   help="ワークスペースの全ユーザー一覧を result/users.json に保存する（要 users:read）")
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
    p.add_argument("--update", "--incremental", dest="update", action="store_true",
                   help=("増分取得。既存の出力 JSON があれば保存済みの最新メッセージ以降だけ取得して"
                         "マージする（全期間モード時のみ有効。--skip-existing より優先）"))
    p.add_argument("--start-rate", type=float, default=DEFAULT_START_RATE_PER_MIN, dest="start_rate",
                   help=("メソッドごとの初期送出レート（req/分）。429 を踏むと自動で 0.7 倍に減速し、"
                         f"成功が続けば --max-rate まで増える（デフォルト: {DEFAULT_START_RATE_PER_MIN:.0f}）"))
    p.add_argument("--max-rate", type=float, default=DEFAULT_MAX_RATE_PER_MIN, dest="max_rate",
                   help=("AIMD の送出レート上限（req/分・メソッド毎）。実測の 429 率を見ながら上げ下げする。"
                         f"上げすぎると 429→待機のロスが増える（デフォルト: {DEFAULT_MAX_RATE_PER_MIN:.0f}）"))
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, dest="max_retries",
                   help=("1 リクエストあたりの 429 リトライ上限（0 = 無制限）。各回 Retry-After ぶん"
                         f"待機する（デフォルト: {DEFAULT_MAX_RETRIES}）"))
    p.add_argument("--progress-interval", type=float, default=30.0, dest="progress_interval",
                   help=("--all-channels で N 秒ごとに進捗・レート・429 状況を出力する（0 で無効 / "
                         "デフォルト: 30）"))
    return p.parse_args()


def main():
    args = parse_args()
    client = build_client(args.start_rate, args.max_retries, max_rate_per_min=args.max_rate)
    with_threads = not args.no_threads
    types = "public_channel,private_channel" + (",im,mpim" if args.include_dms else "")

    try:
        if args.list_users:
            users = list_users(client)
            dump_user_list(users)
            return

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
                                    skip_existing=args.skip_existing,
                                    incremental=args.update,
                                    progress_interval=max(0.0, args.progress_interval))
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

        incremental = args.update and period == "all"
        if args.update and not incremental:
            print("ℹ️  --update は全期間モード（--all / 日付 all）のときだけ有効です。無視します。")

        channel = get_channel_info(client, args.channel_id)
        if args.skip_existing and not incremental and os.path.exists(output_path(channel, period)):
            print(f"⏭️  既に取得済みです: {output_path(channel, period)}")
            return

        log(export_channel(client, channel, oldest=oldest, latest=latest,
                           period=period, with_threads=with_threads,
                           incremental=incremental))

    except SlackApiError as e:
        err = e.response.get("error")
        msg = f"❌ Slack API エラー: {err}"
        if err == "missing_scope":
            msg += f" (必要: {e.response.get('needed')} / 現在: {e.response.get('provided')})"
        print(msg)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
