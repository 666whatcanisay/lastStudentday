# -*- coding: utf-8 -*-
"""
写入门禁（方案 B 备份一致性守卫，单进程内有效）

备份的关键矛盾：备份期间若有写入落地（上传建记录、OCR 发布版本、索引提交分块、
问答落记录），ZIP 里的数据库与原件就可能对不上。本模块是**全进程统一的写入口**：

- 所有写路径用 `with write_section(...)` 包裹提交段；门禁关闭时进入直接抛
  BackupInProgress（路由层转 409"备份中，请稍后重试"）。
- 备份用 `with backup_window(timeout=60)`：先关门拒新写入，再等已进场的
  写入全部结束（最多 60s），超时自动开门并取消本次备份（不强杀任务）。
- OCR/索引整体作为可重入写任务计数；已入场任务可以完成嵌套提交。
  备份等待超过 60 秒即取消备份，不能以备份为由中断已入场任务。

⚠️ 单进程内协调写入；runtime_guard 的操作系统锁禁止同一数据目录启动多个进程。
"""
import logging
import threading
import time
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)


class BackupInProgress(RuntimeError):
    """门禁关闭期间尝试写入 / 长任务在阶段边界主动中止"""


class BackupTimeout(RuntimeError):
    """等待在途写入结束超时（60s），备份取消"""


class WriteGate:
    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._closed = False
        self._inflight = 0
        self._local = threading.local()

    def writer(self, fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with self.write_section(fn.__name__):
                return fn(*args, **kwargs)
        return wrapped

    # ---------- 写路径侧 ----------
    @contextmanager
    def write_section(self, name: str = ""):
        """写入临界区：进入时若门禁关闭则拒绝。"""
        if getattr(self._local, 'depth', 0):
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        with self._cond:
            if self._closed:
                raise BackupInProgress(
                    f"系统正在备份，写入被拒绝（{name or 'write'}），请稍后重试")
            self._inflight += 1
            self._local.depth = 1
        try:
            yield
        finally:
            with self._cond:
                self._inflight -= 1
                self._local.depth = 0
                if self._inflight == 0:
                    self._cond.notify_all()

    def abort_if_closed(self, name: str = ""):
        """长任务阶段边界检查点：门禁已关则主动中止当前任务。"""
        with self._cond:
            if self._closed and not getattr(self._local, 'depth', 0):
                raise BackupInProgress(
                    f"系统开始备份，任务已中止（{name or 'task'}），可稍后重试")

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    # ---------- 备份侧 ----------
    @contextmanager
    def backup_window(self, timeout: float = 60.0):
        """备份窗口：关门 → 等在途归零 → 交给调用方做一致性拷贝 → 开门。"""
        with self._cond:
            self._closed = True
        try:
            deadline = time.monotonic() + timeout
            with self._cond:
                while self._inflight > 0:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        raise BackupTimeout(
                            "等待在途写入结束超过 60 秒，本次备份已取消，请稍后重试")
                    self._cond.wait(remain)
            yield
        finally:
            with self._cond:
                self._closed = False
            logger.info("备份窗口关闭，写入恢复")


# 全进程唯一实例
GATE = WriteGate()
