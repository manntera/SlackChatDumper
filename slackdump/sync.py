"""ワンコマンド運用: users 更新 → 全チャンネル取得 → SQLite 取り込み（旧 update.sh 相当）。

標準出力・標準エラーを画面と logs/sync-YYYYMMDD-HHMMSS.log の両方へ書き出す。
"""

import sys
from datetime import datetime

from . import export, importer
from .config import LOG_DIR
from .ratelimit import build_client


class _Tee:
    """write を複数ストリームへ複製する file-like オブジェクト。"""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def run_sync(full: bool = False,
             reply_refresh_days: float = export.DEFAULT_REPLY_REFRESH_DAYS) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"sync-{datetime.now():%Y%m%d-%H%M%S}.log"
    orig_out, orig_err = sys.stdout, sys.stderr
    # buffering=1（行バッファ）で、実行中でも tail -f でログを追える
    with open(log_path, "w", encoding="utf-8", buffering=1) as fh:
        sys.stdout, sys.stderr = _Tee(orig_out, fh), _Tee(orig_err, fh)
        try:
            mode = "full" if full else "update"
            print(f"=== {datetime.now().isoformat(timespec='seconds')} start (mode={mode}) ===")

            client = build_client()
            export.dump_user_list(export.list_users(client))
            channels = export.load_filtered_channels(client)
            export.export_all_channels(client, channels, incremental=not full,
                                       reply_refresh_days=reply_refresh_days)
            importer.run()

            print(f"=== {datetime.now().isoformat(timespec='seconds')} done ===")
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err
    print(f"log: {log_path}")
    return 0
