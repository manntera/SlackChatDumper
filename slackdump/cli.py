"""slackdump コマンドのエントリポイント（サブコマンド定義とディスパッチ）。"""

import argparse

import argcomplete
from slack_sdk.errors import SlackApiError

from . import export, importer
from .export import DEFAULT_REPLY_REFRESH_DAYS
from .ratelimit import build_client


def _cmd_channels(args) -> int:
    client = build_client()
    export.load_filtered_channels(client, include_archived=not args.exclude_archived)
    return 0


def _cmd_users(args) -> int:
    client = build_client()
    export.dump_user_list(export.list_users(client))
    return 0


def _cmd_export(args) -> int:
    client = build_client()
    channels = export.load_filtered_channels(client,
                                             include_archived=not args.exclude_archived)
    export.export_all_channels(client, channels, incremental=not args.full,
                               reply_refresh_days=args.reply_refresh_days)
    return 0


def _cmd_import(args) -> int:
    return importer.run(paths=args.paths, reset=args.reset, db_path=args.db)


def _cmd_sync(args) -> int:
    from .sync import run_sync
    return run_sync(full=args.full, reply_refresh_days=args.reply_refresh_days)


def _add_reply_refresh_days(p: argparse.ArgumentParser) -> None:
    p.add_argument("--reply-refresh-days", type=float, default=DEFAULT_REPLY_REFRESH_DAYS,
                   dest="reply_refresh_days",
                   help=("増分取得時、既存スレッドの返信(conversations.replies)を取り直す窓（日）。"
                         "最終返信(latest_reply)がこの日数以内のスレッドだけ再取得し、それ以外は"
                         "保存済みの返信を流用する。0 で新規スレッドのみ・十分大きな値で全スレッド"
                         f"再取得相当（デフォルト: {DEFAULT_REPLY_REFRESH_DAYS:.0f}）"))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slackdump",
        description="Slack 公開チャンネルのエクスポート（JSON）と SQLite 取り込み")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("channels", help="参照可能な公開チャンネル一覧を表示し result/cache/channels.json に保存")
    sp.add_argument("--exclude-archived", action="store_true",
                    help="アーカイブ済みチャンネルを除外する（既定は含める）")
    sp.set_defaults(func=_cmd_channels)

    sp = sub.add_parser("users", help="全ユーザー一覧を result/cache/users.json に保存（要 users:read）")
    sp.set_defaults(func=_cmd_users)

    sp = sub.add_parser("export", help="全チャンネルをエクスポート（既定は増分取得。--full でフル取得）")
    sp.add_argument("--full", action="store_true",
                    help="増分ではなく全期間を取り直す（初回 or 定期フル取得）")
    sp.add_argument("--exclude-archived", action="store_true",
                    help="アーカイブ済みチャンネルを対象から除外する（既定は含める）")
    _add_reply_refresh_days(sp)
    sp.set_defaults(func=_cmd_export)

    sp = sub.add_parser("import", help="result/cache/ の JSON を SQLite(result/slack.db) に取り込む")
    sp.add_argument("paths", nargs="*",
                    help="取り込む slack_*.json のパス（省略時は result/cache/ 配下を自動探索）")
    sp.add_argument("--reset", action="store_true", help="既存テーブルを DROP してから作り直す")
    sp.add_argument("--db", default=None, help="出力 SQLite ファイル（既定: result/slack.db）")
    sp.set_defaults(func=_cmd_import)

    sp = sub.add_parser("sync", help="users → export → import をまとめて実行（ログを logs/ に保存）")
    sp.add_argument("--full", action="store_true",
                    help="増分ではなく全期間を取り直す（初回 or 定期フル取得）")
    _add_reply_refresh_days(sp)
    sp.set_defaults(func=_cmd_sync)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    # シェルのタブ補完（zsh/bash）。.zshrc 側の register-python-argcomplete と対で動く
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SlackApiError as e:
        err = e.response.get("error")
        msg = f"❌ Slack API エラー: {err}"
        if err == "missing_scope":
            msg += f" (必要: {e.response.get('needed')} / 現在: {e.response.get('provided')})"
        print(msg)
        return 1
    except KeyboardInterrupt:
        print("\n⛔ 中断しました（`slackdump export` を再実行すれば増分で続きから拾えます）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
