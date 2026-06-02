"""result/ 配下の Slack エクスポート JSON を SQLite に取り込むツール。

使い方:
    python import_to_sqlite.py
    python import_to_sqlite.py --result-dir result --db result/slack.db
    python import_to_sqlite.py --reset          # 既存テーブルを DROP して作り直し
    python import_to_sqlite.py result/slack_C0123_foo_all.json  # 個別ファイルだけ取り込み

仕様:
    - 既存 JSON ファイルは一切書き換えない。
    - メッセージは (channel_id, ts) を PK に UPSERT。スレッド返信は parent_ts と is_reply=1 を立てて
      同じ messages テーブルに展開する。
    - reactions / files は (channel_id, ts) 単位で全削除→再投入することで「更新後の状態」に揃える。
    - --update で再ダンプした JSON もそのまま再投入できる。
"""

import argparse
import glob
import json
import os
import sqlite3
import sys
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    is_private      INTEGER,
    is_archived     INTEGER,
    period          TEXT,
    exported_at     TEXT,
    message_count   INTEGER,
    thread_count    INTEGER,
    reply_count     INTEGER,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    real_name       TEXT,
    display_name    TEXT,
    email           TEXT,
    is_bot          INTEGER,
    deleted         INTEGER,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    channel_id      TEXT NOT NULL,
    ts              TEXT NOT NULL,
    thread_ts       TEXT,
    parent_ts       TEXT,
    is_reply        INTEGER NOT NULL DEFAULT 0,
    user            TEXT,
    type            TEXT,
    subtype         TEXT,
    text            TEXT,
    reply_count     INTEGER,
    edited_ts       TEXT,
    client_msg_id   TEXT,
    raw             TEXT,
    PRIMARY KEY (channel_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(channel_id, thread_ts);
CREATE INDEX IF NOT EXISTS idx_messages_user   ON messages(user);
CREATE INDEX IF NOT EXISTS idx_messages_ts     ON messages(ts);

CREATE TABLE IF NOT EXISTS reactions (
    channel_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    name        TEXT NOT NULL,
    user        TEXT NOT NULL,
    PRIMARY KEY (channel_id, ts, name, user)
);

CREATE INDEX IF NOT EXISTS idx_reactions_user ON reactions(user);

CREATE TABLE IF NOT EXISTS files (
    channel_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    name        TEXT,
    title       TEXT,
    mimetype    TEXT,
    filetype    TEXT,
    size        INTEGER,
    user        TEXT,
    url_private TEXT,
    permalink   TEXT,
    raw         TEXT,
    PRIMARY KEY (channel_id, ts, file_id)
);
"""

DROP_SQL = """
DROP TABLE IF EXISTS reactions;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS channels;
"""


def _b(v):
    """JSON の真偽値を SQLite 用 0/1 に。None はそのまま None。"""
    if v is None:
        return None
    return 1 if v else 0


def _dumps(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection, reset: bool = False) -> None:
    if reset:
        conn.executescript(DROP_SQL)
    conn.executescript(SCHEMA)
    conn.commit()


def import_channels(conn: sqlite3.Connection, path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    sql = """
        INSERT INTO channels (id, name, is_private, is_archived, raw)
        VALUES (:id, :name, :is_private, :is_archived, :raw)
        ON CONFLICT(id) DO UPDATE SET
            name        = excluded.name,
            is_private  = excluded.is_private,
            is_archived = excluded.is_archived,
            raw         = excluded.raw
    """
    params = [
        {
            "id": c.get("id"),
            "name": c.get("name") or c.get("name_normalized"),
            "is_private": _b(c.get("is_private")),
            "is_archived": _b(c.get("is_archived")),
            "raw": _dumps(c),
        }
        for c in rows
        if c.get("id")
    ]
    conn.executemany(sql, params)
    conn.commit()
    return len(params)


def import_users(conn: sqlite3.Connection, path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    sql = """
        INSERT INTO users (id, name, real_name, display_name, email, is_bot, deleted, raw)
        VALUES (:id, :name, :real_name, :display_name, :email, :is_bot, :deleted, :raw)
        ON CONFLICT(id) DO UPDATE SET
            name         = excluded.name,
            real_name    = excluded.real_name,
            display_name = excluded.display_name,
            email        = excluded.email,
            is_bot       = excluded.is_bot,
            deleted      = excluded.deleted,
            raw          = excluded.raw
    """
    params = []
    for u in rows:
        if not u.get("id"):
            continue
        profile = u.get("profile") or {}
        params.append({
            "id": u.get("id"),
            "name": u.get("name"),
            "real_name": u.get("real_name") or profile.get("real_name"),
            "display_name": profile.get("display_name"),
            "email": profile.get("email"),
            "is_bot": _b(u.get("is_bot")),
            "deleted": _b(u.get("deleted")),
            "raw": _dumps(u),
        })
    conn.executemany(sql, params)
    conn.commit()
    return len(params)


MSG_UPSERT_SQL = """
    INSERT INTO messages (channel_id, ts, thread_ts, parent_ts, is_reply,
                          user, type, subtype, text, reply_count, edited_ts,
                          client_msg_id, raw)
    VALUES (:channel_id, :ts, :thread_ts, :parent_ts, :is_reply,
            :user, :type, :subtype, :text, :reply_count, :edited_ts,
            :client_msg_id, :raw)
    ON CONFLICT(channel_id, ts) DO UPDATE SET
        thread_ts     = excluded.thread_ts,
        parent_ts     = excluded.parent_ts,
        is_reply      = excluded.is_reply,
        user          = excluded.user,
        type          = excluded.type,
        subtype       = excluded.subtype,
        text          = excluded.text,
        reply_count   = excluded.reply_count,
        edited_ts     = excluded.edited_ts,
        client_msg_id = excluded.client_msg_id,
        raw           = excluded.raw
"""


def _msg_row(channel_id: str, m: dict, parent_ts: str | None) -> dict:
    edited = m.get("edited") or {}
    return {
        "channel_id": channel_id,
        "ts": m.get("ts"),
        "thread_ts": m.get("thread_ts"),
        "parent_ts": parent_ts,
        "is_reply": 1 if parent_ts else 0,
        "user": m.get("user") or m.get("bot_id"),
        "type": m.get("type"),
        "subtype": m.get("subtype"),
        "text": m.get("text"),
        "reply_count": m.get("reply_count"),
        "edited_ts": edited.get("ts") if isinstance(edited, dict) else None,
        "client_msg_id": m.get("client_msg_id"),
        "raw": _dumps(m),
    }


def _reaction_rows(channel_id: str, ts: str, reactions: Iterable[dict]) -> list[tuple]:
    rows = []
    for r in reactions or []:
        name = r.get("name")
        if not name:
            continue
        for u in r.get("users") or []:
            rows.append((channel_id, ts, name, u))
    return rows


def _file_rows(channel_id: str, ts: str, files: Iterable[dict]) -> list[dict]:
    rows = []
    for fl in files or []:
        fid = fl.get("id")
        if not fid:
            continue
        rows.append({
            "channel_id": channel_id,
            "ts": ts,
            "file_id": fid,
            "name": fl.get("name"),
            "title": fl.get("title"),
            "mimetype": fl.get("mimetype"),
            "filetype": fl.get("filetype"),
            "size": fl.get("size"),
            "user": fl.get("user"),
            "url_private": fl.get("url_private"),
            "permalink": fl.get("permalink"),
            "raw": _dumps(fl),
        })
    return rows


FILES_UPSERT_SQL = """
    INSERT INTO files (channel_id, ts, file_id, name, title, mimetype,
                       filetype, size, user, url_private, permalink, raw)
    VALUES (:channel_id, :ts, :file_id, :name, :title, :mimetype,
            :filetype, :size, :user, :url_private, :permalink, :raw)
    ON CONFLICT(channel_id, ts, file_id) DO UPDATE SET
        name        = excluded.name,
        title       = excluded.title,
        mimetype    = excluded.mimetype,
        filetype    = excluded.filetype,
        size        = excluded.size,
        user        = excluded.user,
        url_private = excluded.url_private,
        permalink   = excluded.permalink,
        raw         = excluded.raw
"""

REACTIONS_INSERT_SQL = (
    "INSERT OR IGNORE INTO reactions (channel_id, ts, name, user) VALUES (?, ?, ?, ?)"
)

# Pass 2 で executemany にまとめる「行数」の目安。大きすぎると buffer がメモリを食い、
# 小さすぎると executemany 呼び出しのオーバーヘッドが増える。1000 件 ≒ 数 MB が落としどころ。
_FLUSH_EVERY_MSGS = 1000


def import_channel_file(conn: sqlite3.Connection, path: str) -> dict[str, int]:
    """1 つの slack_*.json を取り込む。戻り値は件数。

    巨大チャンネルでもメモリピークが「JSON 全件 ＋ 全行リスト」と二重持ちにならないよう、
    messages を pop で消費しながら一定件数ごとに executemany する（commit はファイル単位で
    1 回だけ。アトミック性は従来通り維持）。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    channel_id = data.get("channel")
    if not channel_id:
        return {"messages": 0, "reactions": 0, "files": 0}

    # チャンネルメタ（個別ファイルの方が最新）
    conn.execute(
        """
        INSERT INTO channels (id, name, is_private, period, exported_at,
                              message_count, thread_count, reply_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name          = COALESCE(excluded.name, channels.name),
            is_private    = COALESCE(excluded.is_private, channels.is_private),
            period        = excluded.period,
            exported_at   = excluded.exported_at,
            message_count = excluded.message_count,
            thread_count  = excluded.thread_count,
            reply_count   = excluded.reply_count
        """,
        (
            channel_id,
            data.get("channel_name"),
            _b(data.get("is_private")),
            data.get("period"),
            data.get("exported_at"),
            data.get("message_count"),
            data.get("thread_count"),
            data.get("reply_count"),
        ),
    )

    messages_in = data.get("messages") or []
    del data  # ラッパー dict は不要。messages_in 経由でメッセージ本体だけ握る。
    # pop() は O(1) で末尾から取り出すので、元の JSON 順を保つよう先に反転しておく。
    # 反転しないと「同じ ts が top-level と reply の両方に出てくる」希少ケースで
    # UPSERT の最終勝者が変わり、parent_ts / is_reply の値が元コードと食い違う。
    messages_in.reverse()

    # Pass 1: affected_ts を集める（ts 文字列だけなので軽い）。reactions / files を消すために必須。
    affected_ts: set[str] = set()
    for m in messages_in:
        ts = m.get("ts")
        if not ts:
            continue
        affected_ts.add(ts)
        for r in m.get("replies") or []:
            rts = r.get("ts")
            if rts:
                affected_ts.add(rts)

    if affected_ts:
        ts_list = list(affected_ts)
        # SQLite のパラメータ数上限を避けるためチャンクで削除
        CHUNK = 500
        for i in range(0, len(ts_list), CHUNK):
            chunk = ts_list[i : i + CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM reactions WHERE channel_id = ? AND ts IN ({placeholders})",
                [channel_id, *chunk],
            )
            conn.execute(
                f"DELETE FROM files WHERE channel_id = ? AND ts IN ({placeholders})",
                [channel_id, *chunk],
            )
        del ts_list
    del affected_ts

    # Pass 2: pop しながら N 件単位で executemany し、その都度 buffer を捨てて RAM を返す。
    totals = {"messages": 0, "reactions": 0, "files": 0}
    msg_buf: list[dict] = []
    react_buf: list[tuple] = []
    file_buf: list[dict] = []

    def _flush() -> None:
        if msg_buf:
            conn.executemany(MSG_UPSERT_SQL, msg_buf)
            totals["messages"] += len(msg_buf)
            msg_buf.clear()
        if react_buf:
            conn.executemany(REACTIONS_INSERT_SQL, react_buf)
            totals["reactions"] += len(react_buf)
            react_buf.clear()
        if file_buf:
            conn.executemany(FILES_UPSERT_SQL, file_buf)
            totals["files"] += len(file_buf)
            file_buf.clear()

    while messages_in:
        m = messages_in.pop()
        ts = m.get("ts")
        if not ts:
            continue
        msg_buf.append(_msg_row(channel_id, m, parent_ts=None))
        react_buf.extend(_reaction_rows(channel_id, ts, m.get("reactions")))
        file_buf.extend(_file_rows(channel_id, ts, m.get("files")))
        for r in m.get("replies") or []:
            rts = r.get("ts")
            if not rts:
                continue
            msg_buf.append(_msg_row(channel_id, r, parent_ts=ts))
            react_buf.extend(_reaction_rows(channel_id, rts, r.get("reactions")))
            file_buf.extend(_file_rows(channel_id, rts, r.get("files")))
        if len(msg_buf) >= _FLUSH_EVERY_MSGS:
            _flush()

    _flush()
    conn.commit()
    return totals


def discover_channel_files(result_dir: str) -> list[str]:
    pattern = os.path.join(result_dir, "slack_*.json")
    # users.json / channels.json は別経路。slack_ プレフィックスで安全に絞り込み済み。
    return sorted(glob.glob(pattern))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Slack エクスポート JSON を SQLite に取り込む")
    p.add_argument("paths", nargs="*", help="取り込む slack_*.json のパス（省略時は --result-dir 配下を自動探索）")
    p.add_argument("--result-dir", default="result", help="JSON が置かれているディレクトリ（既定: result）")
    p.add_argument("--db", default=None, help="出力 SQLite ファイル（既定: <result-dir>/slack.db）")
    p.add_argument("--reset", action="store_true", help="既存テーブルを DROP してから作り直す")
    p.add_argument("--skip-channels", action="store_true", help="channels.json の取り込みをスキップ")
    p.add_argument("--skip-users", action="store_true", help="users.json の取り込みをスキップ")
    args = p.parse_args(argv)

    db_path = args.db or os.path.join(args.result_dir, "slack.db")

    if args.paths:
        files = list(args.paths)
    else:
        files = discover_channel_files(args.result_dir)

    conn = connect(db_path)
    try:
        init_schema(conn, reset=args.reset)

        if not args.skip_channels:
            n = import_channels(conn, os.path.join(args.result_dir, "channels.json"))
            print(f"channels.json: {n} 件")
        if not args.skip_users:
            n = import_users(conn, os.path.join(args.result_dir, "users.json"))
            print(f"users.json: {n} 件")

        total_msg = total_react = total_file = 0
        for i, path in enumerate(files, 1):
            try:
                counts = import_channel_file(conn, path)
            except Exception as e:
                print(f"[{i}/{len(files)}] {os.path.basename(path)} 失敗: {e}", file=sys.stderr)
                continue
            total_msg += counts["messages"]
            total_react += counts["reactions"]
            total_file += counts["files"]
            print(
                f"[{i}/{len(files)}] {os.path.basename(path)}: "
                f"messages={counts['messages']} reactions={counts['reactions']} files={counts['files']}"
            )

        print(
            f"\n完了: {len(files)} ファイル / messages={total_msg} reactions={total_react} files={total_file}"
        )
        print(f"DB: {db_path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
