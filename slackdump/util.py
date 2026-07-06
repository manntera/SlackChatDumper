"""共有ユーティリティ（スレッドセーフな print と時間フォーマット）。"""

import threading

# StatusReporter（別スレッド）と main の print が混ざらないようにするためのロック
_print_lock = threading.Lock()


def log(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


def fmt_secs(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"
