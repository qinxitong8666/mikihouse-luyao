"""Local-only serialization shared by quote publication and Favorite checkpoints."""
from __future__ import annotations

import fcntl
import inspect
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


_held = threading.local()
CHECKPOINT_FILE = "wechat_daily_production_checkpoint.json"


@contextmanager
def daily_output_lock(output_root: Path):
    root = output_root.resolve()
    key = str(root)
    held = getattr(_held, "roots", set())
    if key in held:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".daily_quote.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("当天报价/收藏流程正在运行，已停止本次操作；请勿并发重跑。") from exc
        _held.roots = held | {key}
        try:
            yield
        finally:
            _held.roots = held
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked_daily_output(function):
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        root = (Path(arguments["daily_dir"]).parent if "daily_dir" in arguments
                else Path(arguments["output_root"]))
        with daily_output_lock(root):
            return function(*args, **kwargs)
    return wrapped


def require_unprotected_quote_directory(daily_dir: Path) -> None:
    checkpoint = daily_dir / CHECKPOINT_FILE
    # Even corrupt JSON or a broken symlink is protection, never permission.
    if checkpoint.exists() or checkpoint.is_symlink():
        raise ValueError(
            "当天 bundle 已受生产 checkpoint 保护，禁止重新生成覆盖。"
            "普通生产重跑只能校验并复用现有 bundle；"
            "两条收藏均已删除时，请使用 App「安全重建已删除收藏」单次授权。"
        )
