"""適応レートリミッタ付き Slack WebClient。

Slack のレート制限は「メソッド単位 × ワークスペース単位」で課される。コネクション
（並列数）を増やしても同一メソッドの合計スループットは増えないため:
  - メソッドごとに「クライアント側のトークンバケット」で送出レートを制御し、
  - 429 を踏んだら送出レートを 0.7 倍に下げ＋Retry-After ぶん待機、成功が続けば加算的に
    少しずつ上げる（AIMD: multiplicative-decrease / additive-increase。実効レートが自動で
    レート枠付近に収束する）、
  - 429 は Retry-After に従って再試行する（MAX_RETRIES 回まで）。
"""

import threading
import time

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import ConnectionErrorRetryHandler

from .config import slack_token
from .util import fmt_secs, log

START_RATE_PER_MIN = 30.0   # メソッドあたりの初期送出レート（req/分）
MIN_RATE_PER_MIN = 1.0      # これ以上は下げない（実際の待機は Retry-After が担保）
# AIMD の上限。実測ログでは conversations.replies は 60 を超えると 429 を頻発する。上げすぎると
# 429→Retry-After 待機のロスが増えてむしろ遅くなるので、控えめに 60。
MAX_RATE_PER_MIN = 60.0
MAX_RETRIES = 50            # 1 リクエストあたりの 429 リトライ上限


class MethodLimiter:
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


class PacedWebClient(WebClient):
    """全 HTTP リクエストの直前にメソッド単位のレートリミッタを通し、429 は Retry-After に
    従って自前で待って再試行する WebClient。

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
                lim = MethodLimiter(self._start_rate, self._min_rate, self._max_rate)
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

    # NOTE: slack_sdk の内部メソッドをフックしている（3.x で安定。pyproject.toml で <4 に固定）。
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

    def method_stats(self):
        """メソッド名 → stats() の dict（ログ・最終サマリ用）。"""
        with self._limiters_lock:
            items = list(self._limiters.items())
        return {name: lim.stats() for name, lim in items}


class StatusReporter(threading.Thread):
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
        elapsed = fmt_secs(now - self._started_at)
        log(f"📊 [{elapsed}] 進捗 {done}/{total} ｜ "
            + " ｜ ".join(parts)
            + f" ｜ 取得中: {current_str}")


def build_client() -> PacedWebClient:
    token = slack_token()
    if not token:
        print("❌ SLACK_TOKEN が設定されていません (.env を確認してください)")
        raise SystemExit(1)
    client = PacedWebClient(
        token=token,
        start_rate_per_sec=START_RATE_PER_MIN / 60.0,
        min_rate_per_sec=MIN_RATE_PER_MIN / 60.0,
        max_rate_per_sec=MAX_RATE_PER_MIN / 60.0,
        max_retries=MAX_RETRIES,
    )
    # ネットワーク系の一時エラーは slack_sdk 側でリトライ（429 は PacedWebClient が担当）
    client.retry_handlers.append(ConnectionErrorRetryHandler(max_retry_count=3))
    return client
