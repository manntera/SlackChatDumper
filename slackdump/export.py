"""Slack からのエクスポート（チャンネル一覧・ユーザー一覧・全チャンネルの全期間取得）。

取得対象はワークスペースの公開チャンネルのみ（参加・未参加を問わない。アーカイブ済みも
既定で含む）。非公開チャンネル・DM・グループDM は対象外。

増分取得（incremental=True。CLI の既定）について:
    既に出力した JSON があるチャンネルは、保存済みメッセージの中で最も新しい ts 以降だけを
    conversations.history で取得して既存ぶんとマージする。
    既存ファイルが無いチャンネルは通常どおり全期間を取得する。
    スレッド返信(conversations.replies)は全スレッド親を取り直すと増分でも全取得と同じ時間が
    かかる（replies の呼び出し回数が history のページ数を桁違いに上回るため）ので、
      (1) 今回新しく取れたスレッド親、
      (2) 既存スレッド親のうち最終返信(latest_reply)が直近 reply_refresh_days 日以内のもの、
    だけ取り直し、それ以外は保存済みの返信をそのまま流用する（デフォルト 7 日。0 で新規
    スレッドのみ・十分大きな値で全スレッド再取得相当）。
    ※ 仕様上の限界: 前回取得より古いメッセージの「編集・削除」、古いメッセージに後から
      ぶら下がった「新規スレッド」、および reply_refresh_days 日より長く沈黙していた
      スレッドへの新着返信は反映されません。これらも正確に取りたい時は --full で
      たまにフル再取得してください。
"""

import gc
import json
import os
import re
import time
from datetime import datetime

from slack_sdk.errors import SlackApiError

from .config import CACHE_DIR, load_config
from .ratelimit import MethodLimiter, StatusReporter
from .util import fmt_secs, log

DEFAULT_REPLY_REFRESH_DAYS = 7.0
PROGRESS_INTERVAL_SECS = 30.0  # 進捗行（📊）の出力間隔


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "channel"


def output_path(channel):
    """チャンネルの出力 JSON パス。既存キャッシュとの互換のため `_all` サフィックスを維持。"""
    channel_id = channel["id"]
    name = channel.get("name") or channel_id
    stem = f"slack_{channel_id}" + (f"_{safe_filename(name)}" if name != channel_id else "")
    return CACHE_DIR / f"{stem}_all.json"


# --------------------------------------------------------------------------- #
# 増分取得まわり
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
def list_channels(client, include_archived=True):
    """ワークスペースの公開チャンネル一覧を返す（参加・未参加を問わない）。

    User トークンなら未参加の公開チャンネルも履歴を読めるため、参加有無で絞らず
    conversations.list で全公開チャンネルを列挙する。include_archived=True（デフォルト）
    のときはアーカイブ済みも含める。非公開チャンネル・DM は対象外。
    """
    channels = []
    for page in client.conversations_list(types="public_channel", limit=200,
                                           exclude_archived=not include_archived):
        channels.extend(page["channels"])
    channels.sort(key=lambda c: c.get("name") or c.get("id", ""))
    return channels


def filter_excluded(channels, exclude):
    """config の exclude_channels（名前 or ID のリスト）に該当するチャンネルを除く。

    ID は完全一致、名前は先頭 `#` の有無と大文字小文字を無視して一致させる。
    戻り値は (残すチャンネル, 除外したチャンネル)。
    """
    if not exclude:
        return channels, []
    ids = {str(e).strip() for e in exclude if str(e).strip()}            # ID は完全一致
    names = {str(e).strip().lstrip("#").lower() for e in exclude if str(e).strip()}
    kept, removed = [], []
    for c in channels:
        name = (c.get("name") or "").lower()
        if c.get("id") in ids or name in names:
            removed.append(c)
        else:
            kept.append(c)
    return kept, removed


def load_filtered_channels(client, include_archived=True):
    """公開チャンネルを列挙 → config の exclude_channels を除外 → 一覧を保存して返す。"""
    channels = list_channels(client, include_archived=include_archived)
    channels, excluded = filter_excluded(channels, load_config().get("exclude_channels"))
    dump_channel_list(channels, excluded)
    return channels


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


def fetch_history(client, channel_id, oldest=None, inclusive=False):
    # limit は 1 ページあたりの取得件数（最大 999）。大きくするほどページ数＝API 呼び出し数が減る。
    kwargs = {"channel": channel_id, "limit": 999}
    if oldest is not None:
        kwargs["oldest"] = oldest
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


def _needs_reply_refresh(msg, resume_f, refresh_cutoff):
    """増分取得でこのスレッド親の replies を取り直す必要があるか。

    取り直す: (1) resume_ts 以降に取れた新しい親、(2) まだ replies を持たない親、
    (3) 最終返信(latest_reply)が refresh_cutoff 以降の親。
    それ以外（=最終返信が窓より古い親）は保存済みの replies を流用する。
    """
    ts = msg.get("ts")
    if ts is not None and float(ts) >= resume_f:
        return True
    if not isinstance(msg.get("replies"), list):
        return True
    latest = msg.get("latest_reply")
    if latest is None:
        return True  # 最終返信時刻が不明なら安全側で取り直す
    return float(latest) >= refresh_cutoff


def attach_threads(client, channel_id, messages, resume_ts=None, refresh_cutoff=None):
    """history で得たメッセージのうちスレッド親に、replies を付与する（直列取得）。

    並列にしてもレート枠が律速するので速くはならず、429 と一時メモリだけが増える。

    増分取得（resume_ts を渡したとき）は全スレッド親を取り直さず、_needs_reply_refresh が
    True を返した親だけ replies を取り直し、それ以外は保存済みの replies を流用する。
    戻り値: (スレッド親数, 返信総数, replies を実際に取得した親数)。
    """
    parents = [m for m in messages
               if m.get("ts") and m.get("thread_ts") == m["ts"] and m.get("reply_count", 0) > 0]
    if not parents:
        return 0, 0, 0

    resume_f = float(resume_ts) if resume_ts is not None else None
    reply_total = 0
    fetched = 0
    for msg in parents:
        if resume_f is not None and not _needs_reply_refresh(msg, resume_f, refresh_cutoff):
            reply_total += len(msg.get("replies") or [])  # 保存済みの replies を流用
            continue
        replies = fetch_replies(client, channel_id, msg["ts"])
        msg["replies"] = replies
        # 次回の増分取得の窓判定が正確になるよう、スレッドのメタ情報も最新化しておく
        msg["reply_count"] = len(replies)
        if replies:
            msg["latest_reply"] = replies[-1].get("ts") or msg.get("latest_reply")
        reply_total += len(replies)
        fetched += 1
    return len(parents), reply_total, fetched


# --------------------------------------------------------------------------- #
# エクスポート
# --------------------------------------------------------------------------- #
def export_channel(client, channel, incremental=False,
                   reply_refresh_days=DEFAULT_REPLY_REFRESH_DAYS):
    """1 チャンネル分（全期間・スレッド返信込み）を取得して JSON に書き出す。完了行（文字列）を返す。

    incremental=True なら、既存 JSON があれば保存済みの最新 ts 以降だけを取得して既存ぶんと
    マージする。スレッド返信は新規スレッドと、最終返信が直近 reply_refresh_days 日以内の
    既存スレッドだけ取り直し、それ以外は保存済みの返信を流用する。
    """
    channel_id = channel["id"]
    name = channel.get("name") or channel_id
    t_start = time.monotonic()
    path = output_path(channel)

    existing = load_existing_messages(path) if incremental else []
    resume_ts = newest_message_ts(existing) if existing else None
    if resume_ts is not None:
        # resume_ts そのものも取り直して（境界メッセージの編集・新規返信を拾うため）ts で重複排除
        oldest, fetch_inclusive = resume_ts, True
    else:
        oldest, fetch_inclusive = None, False

    new_messages, hist_pages = fetch_history(client, channel_id, oldest,
                                             inclusive=fetch_inclusive)
    if resume_ts is not None:
        # inclusive=True で境界メッセージ（resume_ts そのもの）が必ず 1 件返るため、
        # それを除いた「真に新しい」件数だけを新規として数える（新着ゼロなら 0 件）。
        new_count = sum(1 for m in new_messages
                        if m.get("ts") and float(m["ts"]) > float(resume_ts))
    else:
        new_count = len(new_messages)
    if existing:
        messages = merge_messages(existing, new_messages)
        del existing, new_messages  # merge_messages が中身を pop し尽くした空 list
    else:
        messages = new_messages
        del new_messages

    refresh_cutoff = None
    if resume_ts is not None:
        # 最終返信がこの時刻以降の既存スレッドだけ replies を取り直す
        refresh_cutoff = time.time() - max(0.0, reply_refresh_days) * 86400.0
    # replies_calls = conversations.replies を実際に呼んだ親数（増分時は窓内のぶんだけ）
    thread_count, reply_total, replies_calls = attach_threads(
        client, channel_id, messages, resume_ts=resume_ts, refresh_cutoff=refresh_cutoff)

    msg_count = len(messages)  # json.dump 後に messages を解放するため先に取っておく
    payload = {
        "channel": channel_id,
        "channel_name": channel.get("name"),
        "is_private": channel.get("is_private"),
        "period": "all",
        "message_count": msg_count,
        "thread_count": thread_count,
        "reply_count": reply_total,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 中断時に壊れた JSON を残さない
    # 巨大チャンネルだと完了行を組み立てている間も RAM に居座るので、ここで明示的に手放す。
    del payload, messages

    elapsed = time.monotonic() - t_start
    api_calls = hist_pages + replies_calls
    msg_per_s = msg_count / elapsed if elapsed > 0 else 0.0
    if resume_ts is not None:
        # 増分時は「replies を取り直した親数 / 保存済みを流用した親数」を明示する
        detail = (f" / スレッド {thread_count} 件・返信 {reply_total} 件"
                  f"（replies 再取得 {replies_calls}・流用 {thread_count - replies_calls}）")
    else:
        detail = f" / スレッド {thread_count} 件・返信 {reply_total} 件"
    api = (f" ｜ {fmt_secs(elapsed)} / API {api_calls} 回(history {hist_pages}＋replies {replies_calls})"
           f" / {msg_per_s:.0f} msg/s")
    if resume_ts is not None:
        return (f"🔄 #{name} ({channel_id}): 新規 {new_count} 件 → 合計 {msg_count} 件"
                f"{detail}{api} → {path}")
    return f"✅ #{name} ({channel_id}): メッセージ {msg_count} 件{detail}{api} → {path}"


def export_all_channels(client, channels, incremental=True,
                        reply_refresh_days=DEFAULT_REPLY_REFRESH_DAYS):
    """複数チャンネルを直列にエクスポートする。

    - 1 チャンネルずつ順番に処理する（並列にしてもレート枠が律速で速度は変わらず、
      429 とメモリピークだけが増えるため）
    - 実際の送出ペースは PacedWebClient のメソッド単位レートリミッタが律速する
    - incremental=True なら既存 JSON があるチャンネルは新規ぶんだけ取得してマージする
      （既存スレッドの返信は最終返信が直近 reply_refresh_days 日以内のものだけ取り直す）
    - PROGRESS_INTERVAL_SECS 秒ごとに送出レート・実効スループット・429 状況を出力する
    """
    total = len(channels)
    if incremental:
        n_update = sum(1 for c in channels if os.path.exists(output_path(c)))
        mode = f"増分取得（既存 {n_update} 件は新規ぶんのみ・残り {total - n_update} 件は全期間）"
    else:
        mode = "全期間エクスポート"
    print(f"\n🚀 {total} 件のチャンネルを{mode}します（スレッド返信込み） / 直列実行")
    print(f"   送出レート: 初期 {client._start_rate * 60:.0f}/分・上限 {client._max_rate * 60:.0f}/分"
          f"（メソッド毎）。429 で 0.7 倍に減速＋Retry-After ぶん待機、成功が続けば"
          f" +{MethodLimiter.INCREASE_STEP_PER_SEC * 60:.0f}/分ずつ復帰（AIMD）")
    if incremental:
        print(f"   スレッド返信: 新規スレッドと、最終返信が直近 {reply_refresh_days:.0f} 日以内の"
              f"既存スレッドだけ取り直します（--reply-refresh-days）")
    print(f"   {PROGRESS_INTERVAL_SECS:.0f} 秒ごとに 📊 進捗行（設定/実効レート・429・取得中チャンネル）を出します")

    state = {"done": 0, "current": None}
    reporter = StatusReporter(client, PROGRESS_INTERVAL_SECS,
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
                summary = export_channel(client, c, incremental=incremental,
                                         reply_refresh_days=reply_refresh_days)
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
                # 巨大チャンネル直後にスレッド返信由来の循環参照が残ると次チャンネル処理開始時の
                # RSS ピークが下がらないので、ここで明示的に回収する（高々数十 ms）
                gc.collect()
    finally:
        reporter.stop()
        reporter.join(timeout=2.0)

    wall = time.monotonic() - t_wall0
    print(f"\n📊 メソッド別の最終サマリ（{fmt_secs(wall)} 経過 / 成功 {ok} 件・失敗 {err} 件）:")
    stats = client.method_stats()
    for m, s in sorted(stats.items()):
        avg = s["ok"] / wall * 60.0 if wall > 0 else 0.0
        ratio = (s["rl"] / s["req"] * 100.0) if s["req"] else 0.0
        print(f"   {m}: リクエスト {s['req']} 回（成功 {s['ok']} / 429 {s['rl']} = {ratio:.1f}%）"
              f" / 平均実効 {avg:.0f}/分・最終 {s['rate_per_min']:.0f}/分"
              f" / スロットル {s['throttle_events']} 回・待機計 {fmt_secs(s['throttle_wait_total'])}"
              f" / ペーシング待ち計 {fmt_secs(s['pace_wait_total'])}")
    print("🎉 完了しました（失敗が出た場合はもう一度 `slackdump export` を実行すると増分で拾い直せます）")


def dump_channel_list(channels, excluded=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = CACHE_DIR / "channels.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)
    print(f"📋 参照可能な公開チャンネル {len(channels)} 件:")
    for c in channels:
        members = c.get("num_members")
        members = f"  ({members}名)" if members is not None else ""
        print(f"  ＃ {c.get('name', c['id']):<32} {c['id']}{members}")
    if excluded:
        names = ", ".join(f"#{c.get('name', c['id'])}" for c in excluded)
        print(f"🚫 除外 {len(excluded)} 件（config.json の exclude_channels）: {names}")
    print(f"→ 一覧を {path} に保存しました")
    return path


def dump_user_list(users):
    """users.list の結果を result/cache/users.json に保存し、サマリを表示する。

    メッセージ JSON の `user` フィールド（U0123ABC456 形式）をこの users.json で引けば
    誰の発言か特定できる。出力は users.list の生のメンバー配列をそのまま保持する。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = CACHE_DIR / "users.json"
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
