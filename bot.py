#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Telegram 媒体组下载器

目标：
- 支持添加来源：频道、超级群 Forum 话题
- 仅下载媒体（含相册 grouped_id）到本地目录
- 按媒体组记录下载完成与“最后消息”进度
- 可选：下载完成后 rclone copy 上传到 remote，成功后删除本地

落盘结构：
单媒体：
<DOWNLOAD_ROOT_DIR>/<来源标题>/<YYYY-MM-DD>/<消息ID>.<ext>
相册（grouped_id）：
<DOWNLOAD_ROOT_DIR>/<来源标题>/<YYYY-MM-DD>/TG@BieLaoZai_<最小消息ID>/<消息ID>.<ext>

说明：
- 序号按“来源+日期”id
- 日期仅从消息文本/标题中提取 `YYYY年MM月DD日`，不再回退到消息发送日期
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

try:
    import socks
except ImportError:
    socks = None

from telethon import TelegramClient, events, functions, errors
from telethon.errors import FloodWaitError

from database import Database

logger = logging.getLogger(__name__)

DATE_RE = re.compile(
    r"(?:(?P<y1>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日)"
    r"|(?:(?P<y2>\d{4})[./-](?P<m2>\d{1,2})[./-](?P<d2>\d{1,2}))"
)

_TELETHON_DEDUP_LAST_TS: Dict[str, float] = {}
_ACTIVE_MEDIA_DOWNLOADS = 0


def has_active_media_downloads() -> bool:
    return int(_ACTIVE_MEDIA_DOWNLOADS) > 0


@dataclass(frozen=True)
class PreparedDownloadGroup:
    group_uid: str
    date_str: str
    seq: int
    group_folder: str
    local_dir: Path
    remote_dir: Optional[str]
    last_message_id: int


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return str(value)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字")


def normalize_rclone_remote(remote: Optional[str]) -> Optional[str]:
    if remote is None:
        return None
    r = str(remote).strip()
    if not r:
        return None
    return r if r.endswith(":") else f"{r}:"


def is_valid_rclone_remote_path(path: Optional[str]) -> bool:
    if not path:
        return False
    return re.match(r"^[A-Za-z0-9_\\-]+:", str(path)) is not None


def build_proxy() -> Optional[Tuple[int, str, int, bool, Optional[str], Optional[str]]]:
    if not _env_bool("PROXY_ENABLED", False):
        return None

    if socks is None:
        raise RuntimeError(
            "代理已启用但未安装 PySocks（pysocks）。请运行：python -m pip install -r requirements.txt；"
            "或将 PROXY_ENABLED=false 关闭代理。"
        )

    proxy_type_raw = (_env_str("PROXY_TYPE", "socks5") or "socks5").lower()
    if proxy_type_raw == "socks5":
        proxy_type = socks.SOCKS5
    elif proxy_type_raw == "socks4":
        proxy_type = socks.SOCKS4
    elif proxy_type_raw == "http":
        proxy_type = socks.HTTP
    else:
        proxy_type = socks.SOCKS5

    host = _env_str("PROXY_HOST", "127.0.0.1") or "127.0.0.1"
    port = _env_int("PROXY_PORT", 1080) or 1080
    username = _env_str("PROXY_USERNAME")
    password = _env_str("PROXY_PASSWORD")

    return (proxy_type, host, int(port), True, username, password)


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    user_phone: Optional[str]
    download_root_dir: Path
    media_group_flush_seconds: float
    max_download_concurrency: int
    max_group_concurrency: int
    rclone_enabled: bool
    rclone_remote: Optional[str]
    rclone_remote_base_dir: Optional[str]
    rclone_delete_local_after_upload: bool
    rclone_cleanup_db_after_upload: bool
    rclone_extra_args: List[str]


def load_config() -> AppConfig:
    load_dotenv()
    try:
        import cryptg  # noqa: F401
    except Exception:
        logger.warning("未安装 cryptg，Telethon 将使用纯 Python AES，下载可能很慢；请在 venv 中安装 cryptg。")

    api_id = _env_int("API_ID")
    api_hash = _env_str("API_HASH")
    user_phone = _env_str("USER_PHONE")

    if not api_id or not api_hash:
        raise RuntimeError("请在 .env 中配置 API_ID 与 API_HASH")

    download_root = _env_str("DOWNLOAD_ROOT_DIR", "downloads") or "downloads"
    flush_seconds = _env_float("MEDIA_GROUP_FLUSH_SECONDS", 2.0)
    max_download = int(_env_int("MAX_DOWNLOAD_CONCURRENCY", 5) or 5)
    max_download = max(1, min(30, max_download))
    max_groups = int(_env_int("MAX_GROUP_CONCURRENCY", max_download) or max_download)
    max_groups = max(1, min(50, max_groups))

    rclone_enabled = _env_bool("RCLONE_ENABLED", False)
    rclone_remote = normalize_rclone_remote(_env_str("RCLONE_REMOTE"))
    rclone_base = _env_str("RCLONE_REMOTE_BASE_DIR")
    rclone_delete = _env_bool("RCLONE_DELETE_LOCAL_AFTER_UPLOAD", True)
    rclone_cleanup_db_after_upload = _env_bool("RCLONE_CLEANUP_DB_AFTER_UPLOAD", True)

    extra_args_raw = _env_str("RCLONE_EXTRA_ARGS", "")
    rclone_extra_args = [arg for arg in (extra_args_raw or "").split() if arg.strip()]

    if rclone_enabled and not rclone_remote:
        raise RuntimeError("启用 RCLONE_ENABLED 时必须配置 RCLONE_REMOTE（例如 pikpak:）")

    return AppConfig(
        api_id=int(api_id),
        api_hash=str(api_hash),
        user_phone=user_phone,
        download_root_dir=Path(download_root),
        media_group_flush_seconds=float(flush_seconds),
        max_download_concurrency=int(max_download),
        max_group_concurrency=int(max_groups),
        rclone_enabled=rclone_enabled,
        rclone_remote=rclone_remote,
        rclone_remote_base_dir=rclone_base,
        rclone_delete_local_after_upload=rclone_delete,
        rclone_cleanup_db_after_upload=rclone_cleanup_db_after_upload,
        rclone_extra_args=rclone_extra_args,
    )


def setup_logging(debug: bool) -> None:
    level_str = (_env_str("LOG_LEVEL") or "").strip().upper()
    if level_str:
        level = getattr(logging, level_str, logging.INFO)
    else:
        level = logging.DEBUG if debug else logging.INFO

    log_console = _env_bool("LOG_CONSOLE", True)
    log_file_enabled = _env_bool("LOG_FILE_ENABLED", True)
    log_file_path = Path(_env_str("LOG_FILE_PATH", "./logs/app.log") or "./logs/app.log")
    max_bytes = int(_env_int("LOG_FILE_MAX_BYTES", 10 * 1024 * 1024) or (10 * 1024 * 1024))
    backup_count = int(_env_int("LOG_FILE_BACKUP_COUNT", 5) or 5)

    handlers: List[logging.Handler] = []
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    if log_console:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        handlers.append(sh)

    if log_file_enabled:
        try:
            log_file_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                filename=str(log_file_path),
                maxBytes=max(0, int(max_bytes)),
                backupCount=max(0, int(backup_count)),
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            handlers.append(fh)
        except Exception:
            # 文件日志失败不应阻断程序
            pass

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)

    _apply_third_party_log_levels()


def should_log_each_download_message() -> bool:
    return _env_bool("LOG_DOWNLOAD_EACH_MESSAGE", False)


def should_log_rclone_command() -> bool:
    return _env_bool("LOG_RCLONE_COMMAND", False)


def resolve_rclone_executable() -> str:
    if Path("./rclone.exe").exists():
        return str(Path("./rclone.exe"))
    if Path("./rclone").exists():
        return str(Path("./rclone"))
    return "rclone"


def _apply_third_party_log_levels() -> None:
    level_str = (_env_str("LOG_TELETHON_LEVEL", "WARNING") or "WARNING").strip().upper()
    level = getattr(logging, level_str, logging.WARNING)
    logging.getLogger("telethon").setLevel(level)

    if _env_bool("LOG_TELETHON_TRANSLATE", True):
        dedup_seconds = float(_env_float("LOG_TELETHON_DEDUP_SECONDS", 5.0))

        class _TelethonNoiseFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                try:
                    name = str(record.name or "")
                    msg = record.getMessage()
                except Exception:
                    return True

                if name == "telethon.network.connection.connection" and "Server closed the connection" in msg:
                    now = time.monotonic()
                    key = "telethon_server_closed"
                    last = _TELETHON_DEDUP_LAST_TS.get(key, 0.0)
                    if dedup_seconds > 0 and (now - last) < dedup_seconds:
                        return False
                    _TELETHON_DEDUP_LAST_TS[key] = now
                    record.msg = f"连接被服务器关闭（网络/代理波动），客户端将尝试自动重连。原始信息：{msg}"
                    record.args = ()
                return True

        logging.getLogger("telethon").addFilter(_TelethonNoiseFilter())


def _download_timeout_seconds() -> float:
    return float(_env_float("TG_DOWNLOAD_TIMEOUT_SECONDS", 300.0))


def _download_retry_attempts() -> int:
    return int(_env_int("TG_DOWNLOAD_RETRY_ATTEMPTS", 2) or 2)


def _download_timeout_base_seconds() -> float:
    return float(_env_float("TG_DOWNLOAD_TIMEOUT_BASE_SECONDS", 30.0))


def _download_min_bytes_per_sec() -> int:
    return int(_env_int("TG_DOWNLOAD_MIN_BYTES_PER_SEC", 256 * 1024) or (256 * 1024))


def _download_stall_seconds() -> float:
    return float(_env_float("TG_DOWNLOAD_STALL_SECONDS", 180.0))


def _download_total_timeout_seconds() -> float:
    # 0 表示关闭“下载总时长硬超时”，仅依赖“无进度超时”来判断卡死。
    return float(_env_float("TG_DOWNLOAD_TOTAL_TIMEOUT_SECONDS", 0.0))


def _compute_download_timeout_seconds(expected_size: Optional[int]) -> float:
    hard = max(10.0, float(_download_timeout_seconds()))
    if expected_size is None or expected_size <= 0:
        return hard
    base = max(0.0, float(_download_timeout_base_seconds()))
    min_bps = max(1, int(_download_min_bytes_per_sec()))
    adaptive = base + (float(expected_size) / float(min_bps))
    return max(hard, adaptive)


async def _download_media_with_stall_guard(
    client: TelegramClient,
    message,
    *,
    file: str,
    total_timeout_s: float,
    stall_timeout_s: float,
) -> None:
    stall_timeout_s = max(10.0, float(stall_timeout_s))
    last_progress_ts = time.monotonic()

    def _progress_cb(current: int, total: Optional[int]) -> None:
        nonlocal last_progress_ts
        # 只要 Telethon 报告了任何进度，就认为连接是活的
        last_progress_ts = time.monotonic()

    async def _run() -> None:
        await client.download_media(message, file=file, progress_callback=_progress_cb)

    task = asyncio.create_task(_run())
    start_ts = time.monotonic()
    try:
        while True:
            if task.done():
                return await task
            now = time.monotonic()
            if float(total_timeout_s) > 0 and (now - start_ts) > float(total_timeout_s):
                task.cancel()
                raise TimeoutError("下载总超时")
            if (now - last_progress_ts) > float(stall_timeout_s):
                task.cancel()
                raise TimeoutError("下载无进度超时")
            await asyncio.sleep(1.0)
    finally:
        if not task.done():
            task.cancel()


def _group_retry_attempts() -> int:
    return int(_env_int("TG_GROUP_RETRY_ATTEMPTS", 2) or 2)


def _group_retry_delay_seconds() -> float:
    return float(_env_float("TG_GROUP_RETRY_DELAY_SECONDS", 2.0))


def _clone_retry_attempts() -> int:
    return int(_env_int("TG_CLONE_RETRY_ATTEMPTS", 3) or 3)


def _skip_existing_media() -> bool:
    return _env_bool("TG_SKIP_EXISTING_MEDIA", True)


def _atomic_replace_media() -> bool:
    return _env_bool("TG_ATOMIC_REPLACE_MEDIA", True)


def _verify_media_size() -> bool:
    return _env_bool("TG_VERIFY_MEDIA_SIZE", True)


def _size_mismatch_tolerance_bytes() -> int:
    return int(_env_int("TG_SIZE_MISMATCH_TOLERANCE_BYTES", 1024 * 1024) or (1024 * 1024))


def _guess_media_ext(message) -> str:
    try:
        # Photo 默认保存为 jpg；如果不指定扩展名，容易导致“同 msg_id 不同扩展”从而误判已下载/未下载。
        if getattr(message, "photo", None) is not None and getattr(message, "document", None) is None:
            return ".jpg"
        f = getattr(message, "file", None)
        ext = getattr(f, "ext", None) if f else None
        if ext:
            return str(ext)
        name = getattr(f, "name", None) if f else None
        if name:
            _, ext2 = os.path.splitext(str(name))
            if ext2:
                return str(ext2)
    except Exception:
        pass
    return ""


def _expected_media_size_bytes(message) -> Optional[int]:
    try:
        f = getattr(message, "file", None)
        size = getattr(f, "size", None) if f else None
        if size is None:
            return None
        size_i = int(size)
        return size_i if size_i > 0 else None
    except Exception:
        return None


def _verify_downloaded_file_size(path: Path, expected: Optional[int], *, label: str) -> None:
    if expected is None or expected <= 0:
        return
    try:
        actual = int(path.stat().st_size)
    except Exception:
        return

    tol = max(0, int(_size_mismatch_tolerance_bytes()))
    if abs(actual - int(expected)) <= tol:
        return

    try:
        logger.warning("%s 文件大小不一致：expected=%s actual=%s，将删除并重试", label, str(expected), str(actual))
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    raise ValueError(f"文件大小不一致 expected={expected} actual={actual}")


def _verify_group_files(prepared_group: PreparedDownloadGroup, source_id: int, messages: List) -> None:
    missing: List[int] = []
    size_mismatch: List[int] = []
    for m in messages:
        if not getattr(m, "media", None):
            continue
        mid = int(getattr(m, "id", 0) or 0)
        if mid <= 0:
            continue
        ext = _guess_media_ext(m)
        expected = _expected_media_size_bytes(m) if _verify_media_size() else None
        path = prepared_group.local_dir / f"{mid}{ext}"
        try:
            if not path.exists() or not path.is_file() or int(path.stat().st_size) <= 0:
                missing.append(mid)
                continue
            if _verify_media_size():
                try:
                    _verify_downloaded_file_size(path, expected, label=f"source={source_id} msg_id={mid}")
                except ValueError:
                    # 文件大小不对，已被删除
                    size_mismatch.append(mid)
        except Exception:
            # 其他异常（如文件读取失败）也视为缺失
            missing.append(mid)

    errors = []
    if missing:
        errors.append(f"缺少 {len(missing)} 个文件：{missing[:10]}")
    if size_mismatch:
        errors.append(f"{len(size_mismatch)} 个文件大小不对：{size_mismatch[:10]}")

    if errors:
        raise ValueError(f"媒体组验证失败 - {'; '.join(errors)}")


async def ensure_connected(client: TelegramClient, label: str) -> bool:
    try:
        if not client.is_connected():
            logger.info("%s 准备重连…", label)
            await client.connect()
        if await client.is_user_authorized():
            logger.info("%s 已恢复连接", label)
            return True
    except Exception as e:
        logger.warning("%s 重连失败：%s", label, str(e))
        return False
    return False


async def _connection_watchdog(client: TelegramClient, name: str) -> None:
    if not _env_bool("TG_PING_ENABLED", True):
        return

    interval = float(_env_float("TG_PING_INTERVAL_SECONDS", 20.0))
    timeout = float(_env_float("TG_PING_TIMEOUT_SECONDS", 10.0))
    interval = max(5.0, float(interval))
    timeout = max(3.0, float(timeout))

    while True:
        await asyncio.sleep(interval)
        # 正在下载媒体时避免打断连接（切节点/网络抖动时容易误判并取消下载协程）
        if int(_ACTIVE_MEDIA_DOWNLOADS) > 0:
            continue
        try:
            await asyncio.wait_for(client(functions.updates.GetStateRequest()), timeout=timeout)
        except asyncio.CancelledError:
            return
        except Exception as e:
            # 切换代理节点时，现有连接可能“卡住但不退出”；这里主动断开让外层监督逻辑重连。
            logger.warning("%s 连接疑似卡死/超时，将触发重连：%s", name, str(e))
            try:
                await client.disconnect()
            except Exception:
                pass
            return


def sanitize_path_component(name: str) -> str:
    if not name:
        return "unknown"
    # Windows 不允许的字符：<>:"/\\|?*
    sanitized = re.sub(r"[<>:\"/\\|?*]", "_", name)
    sanitized = sanitized.strip().strip(".")
    if not sanitized:
        return "unknown"
    return sanitized


def ensure_empty_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        children = list(path.iterdir())
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return

    for child in children:
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=False)
            else:
                child.unlink(missing_ok=True)
        except Exception:
            continue


UNPARSED_DATE = "0000-00-00"


def extract_date_yyyy_mm_dd(text: str) -> Optional[str]:
    match = DATE_RE.search(text or "")
    if match:
        y = int(match.group("y1") or match.group("y2") or 0)
        m = int(match.group("m1") or match.group("m2") or 0)
        d = int(match.group("d1") or match.group("d2") or 0)
        if y > 0 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def _message_send_date_yyyy_mm_dd(message) -> Optional[str]:
    try:
        dt = getattr(message, "date", None)
        if not dt:
            return None
        return str(dt.strftime("%Y-%m-%d"))
    except Exception:
        return None


def build_group_uid(chat_id: int, topic_root_message_id: Optional[int], grouped_id: Optional[int], message_id: int) -> str:
    topic_part = str(topic_root_message_id or 0)
    group_part = str(grouped_id or message_id)
    return f"{chat_id}:{topic_part}:{group_part}"


class MediaGroupCollector:
    def __init__(
        self,
        flush_seconds: float,
        on_group: Callable[[int, int, Optional[str], List], asyncio.Future],
    ) -> None:
        self._flush_seconds = max(0.5, float(flush_seconds))
        self._on_group = on_group
        self._pending: Dict[Tuple[int, int], List] = {}
        self._tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._headers: Dict[Tuple[int, int], Optional[str]] = {}
        self._flush_by_key: Dict[Tuple[int, int], float] = {}

    def add(
        self,
        source_id: int,
        group_key: int,
        message,
        header_text: Optional[str] = None,
        flush_seconds: Optional[float] = None,
    ) -> None:
        key = (int(source_id), int(group_key))
        self._pending.setdefault(key, []).append(message)
        if key not in self._headers:
            self._headers[key] = header_text
        if flush_seconds is not None:
            self._flush_by_key[key] = max(0.5, float(flush_seconds))

        existing = self._tasks.get(key)
        if existing and not existing.done():
            existing.cancel()

        delay = float(self._flush_by_key.get(key, self._flush_seconds))
        self._tasks[key] = asyncio.create_task(self._flush_later(key, delay))

    async def _flush_later(self, key: Tuple[int, int], delay: float) -> None:
        try:
            await asyncio.sleep(float(delay))
        except asyncio.CancelledError:
            return

        messages = self._pending.pop(key, [])
        self._tasks.pop(key, None)
        header_text = self._headers.pop(key, None)
        self._flush_by_key.pop(key, None)
        if not messages:
            return

        messages.sort(key=lambda m: m.id)
        source_id, group_key = key
        await self._on_group(source_id, group_key, header_text, messages)


class RcloneUploader:
    def __init__(self, cfg: AppConfig, db: Database) -> None:
        self._cfg = cfg
        self._db = db
        self._queue: "asyncio.Queue[Tuple[int, str, Path, str, asyncio.Future[bool]]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    def start(self) -> None:
        if not self._cfg.rclone_enabled:
            return
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def enqueue(
        self, source_id: int, group_uid: str, local_dir: Path, remote_dir: str
    ) -> "asyncio.Future[bool]":
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        await self._queue.put((int(source_id), str(group_uid), local_dir, str(remote_dir), fut))
        return fut

    async def _run(self) -> None:
        while True:
            source_id, group_uid, local_dir, remote_dir, fut = await self._queue.get()
            try:
                ok = await self._rclone_copy(local_dir, remote_dir)
                if not ok:
                    self._db.mark_download_group_uploaded(
                        source_id, group_uid, uploaded=False, remote_dir=remote_dir, local_deleted=False
                    )
                    if fut and (not fut.done()):
                        fut.set_result(False)
                    continue

                # 先标记“已上传”，避免删除本地失败时把整个组误记为 failed
                self._db.mark_download_group_uploaded(
                    source_id, group_uid, uploaded=True, remote_dir=remote_dir, local_deleted=False
                )

                if self._cfg.rclone_delete_local_after_upload:
                    deleted = False
                    if not local_dir.exists():
                        deleted = True
                    else:
                        last_err: Optional[Exception] = None
                        for attempt in range(6):
                            try:
                                if local_dir.is_dir():
                                    shutil.rmtree(local_dir, ignore_errors=False)
                                else:
                                    local_dir.unlink()
                                deleted = True
                                last_err = None
                                break
                            except Exception as e:
                                last_err = e
                                # Windows 偶发文件占用：退避重试
                                await asyncio.sleep(min(30.0, 2.0**attempt))

                        if not deleted and last_err is not None:
                            logger.error(f"上传成功但删除本地失败: {local_dir} err={last_err}", exc_info=True)

                    self._db.mark_download_group_uploaded(
                        source_id, group_uid, uploaded=True, remote_dir=remote_dir, local_deleted=deleted
                    )

                if self._cfg.rclone_cleanup_db_after_upload:
                    self._db.delete_download_group(source_id, group_uid)
                if fut and (not fut.done()):
                    fut.set_result(True)
            except Exception as e:
                logger.error(f"rclone 上传流程异常: {e}", exc_info=True)
                self._db.mark_download_group_failed(source_id, group_uid, str(e))
                if fut and (not fut.done()):
                    fut.set_exception(e)
            finally:
                self._queue.task_done()

    async def _rclone_copy(self, local_dir: Path, remote_dir: str) -> bool:
        rclone_exe = resolve_rclone_executable()
        # 防止把 remote_dir 当成“本地路径”误拷贝：rclone remote 必须是 `<name>:` 前缀
        if not re.match(r"^[A-Za-z0-9_\\-]+:", str(remote_dir or "")):
            logger.error("rclone 远端路径无效（缺少 remote 前缀 `<name>:`）：%s", str(remote_dir))
            return False
        args = [rclone_exe, "copy", str(local_dir), str(remote_dir), "--create-empty-src-dirs"]
        args.extend(self._cfg.rclone_extra_args)

        logger.info(f"开始上传：{local_dir} -> {remote_dir}")
        if should_log_rclone_command():
            logger.info("rclone 命令: %s", " ".join(args))
        if rclone_exe != "rclone" and not Path(rclone_exe).exists():
            logger.error("未找到 rclone 可执行文件：%s", rclone_exe)
            return False
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for line in proc.stdout:
            logger.info(line.decode(errors="ignore").rstrip())

        code = await proc.wait()
        if code == 0:
            logger.info(f"上传完成: {remote_dir}")
            return True

        logger.error(f"上传失败(code={code}): {remote_dir}")
        return False


async def ensure_user_client(cfg: AppConfig) -> TelegramClient:
    proxy = build_proxy()
    client = TelegramClient("sessions/user_session", cfg.api_id, cfg.api_hash, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("未检测到用户会话，请先运行: python setup_user_session.py")
    return client


def _pick_text_for_date(messages: List) -> str:
    for m in messages:
        txt = getattr(m, "message", None) or getattr(m, "text", None) or ""
        if txt:
            return str(txt)
    return ""


async def download_media_group(
    client: TelegramClient,
    cfg: AppConfig,
    db: Database,
    source_row: dict,
    messages: List,
    uploader: RcloneUploader,
    download_semaphore: Optional[asyncio.Semaphore] = None,
    group_semaphore: Optional[asyncio.Semaphore] = None,
    header_text: Optional[str] = None,
    prepared: Optional[PreparedDownloadGroup] = None,
) -> Tuple[bool, int, Optional[asyncio.Future[bool]]]:
    if not messages:
        return False, 0, None

    if not any(getattr(m, "media", None) for m in messages):
        return False, 0, None

    if group_semaphore is not None:
        await group_semaphore.acquire()
    prepared_group: Optional[PreparedDownloadGroup] = prepared
    min_id = 0
    try:
        if prepared_group is None:
            prepared_group = _prepare_download_group(cfg, db, source_row, messages, header_text=header_text)
        if not prepared_group:
            return False, 0, None

        source_title = str(source_row.get("display_title") or source_row.get("title") or source_row.get("chat_id") or "")
        message_ids = [int(getattr(m, "id", 0) or 0) for m in messages]
        message_ids = [mid for mid in message_ids if mid > 0]
        min_id = min(message_ids) if message_ids else 0
        max_id = max(message_ids) if message_ids else 0

        group_attempts = max(1, int(_group_retry_attempts()))
        group_delay = max(0.0, float(_group_retry_delay_seconds()))
        last_group_err = None  # 记录上一次组级错误，用于决定是否清空目录

        for group_attempt in range(1, group_attempts + 1):
            logger.info(
                "开始下载媒体组：%s ID:%s-%s 目录：%s（资源数量=%s 重试=%s/%s）",
                source_title,
                str(min_id),
                str(max_id),
                str(prepared_group.local_dir),
                str(len(messages)),
                str(group_attempt),
                str(group_attempts),
            )

            # 目录清理策略（优化：不清空目录，保留已下载文件）：
            # 1. 仅当数据库记录显示该组之前下载"失败"时才清空目录（首次重试失败记录）
            # 2. 组级重试时**不清空目录**，完全依赖文件级检查和断点续传
            # 3. 这样可以避免网络波动导致清空已下载的大文件
            should_clear = False
            if group_attempt == 1:
                # 检查数据库记录状态，仅失败组才清空
                existing_record = db.get_download_group(int(source_row["id"]), prepared_group.group_uid)
                if existing_record and existing_record.get("status") == "failed":
                    logger.info(f"检测到失败记录，清空目录重新下载: {prepared_group.local_dir}")
                    should_clear = True
            # 删除了806-809行：组级重试时清空目录的逻辑
            # 改为：重试时不清空，依赖文件级检查（跳过已完成文件，重新下载缺失/损坏文件）

            if should_clear:
                if prepared_group.group_folder:
                    ensure_empty_dir(prepared_group.local_dir)
                else:
                    logger.info(
                        "检测到失败记录，但当前为日期共享目录，跳过清空: %s",
                        str(prepared_group.local_dir),
                    )

            prepared_group.local_dir.mkdir(parents=True, exist_ok=True)

            sem = download_semaphore or asyncio.Semaphore(cfg.max_download_concurrency)

            async def _download_one(msg) -> None:
                if not getattr(msg, "media", None):
                    return
                await sem.acquire()
                try:
                    msg_id = int(getattr(msg, "id", 0) or 0)
                    if should_log_each_download_message():
                        logger.info(
                            "下载单条媒体: source=%s group_uid=%s msg_id=%s",
                            str(source_row.get("id")),
                            prepared_group.group_uid,
                            str(msg_id),
                        )
                    msg_id_prefix = str(msg_id if msg_id > 0 else "unknown")

                    ext = _guess_media_ext(msg)
                    expected_size = _expected_media_size_bytes(msg)
                    final_path = prepared_group.local_dir / f"{msg_id_prefix}{ext}"

                    # 幂等：同一条消息的目标文件已存在则跳过（避免 Telethon 自动产生 (1)(2)...）
                    if msg_id > 0 and _skip_existing_media():
                        try:
                            if final_path.exists() and final_path.is_file() and final_path.stat().st_size > 0:
                                if _verify_media_size():
                                    try:
                                        _verify_downloaded_file_size(
                                            final_path,
                                            expected_size,
                                            label=f"source={source_row.get('id')} msg_id={msg_id_prefix}",
                                        )
                                        if should_log_each_download_message():
                                            logger.info(f"跳过已存在的文件: {final_path} (大小验证通过)")
                                        return  # 仅在验证通过后才跳过
                                    except ValueError:
                                        # 大小不对，文件已被删除，继续重新下载
                                        logger.info(f"文件大小验证失败，将重新下载: {final_path}")
                                        pass
                                else:
                                    if should_log_each_download_message():
                                        logger.info(f"跳过已存在的文件: {final_path} (未启用大小验证)")
                                    return  # 不验证大小时，直接跳过
                        except Exception:
                            pass  # 其他异常（如文件损坏）也重新下载

                    # 估算一个“足够长”的超时用于日志展示；实际是否启用总时长硬超时由 TG_DOWNLOAD_TOTAL_TIMEOUT_SECONDS 控制
                    timeout_s = float(_compute_download_timeout_seconds(expected_size))
                    total_timeout_s = float(_download_total_timeout_seconds())
                    if total_timeout_s > 0:
                        total_timeout_s = max(10.0, min(float(total_timeout_s), float(timeout_s)))
                    attempts = max(1, int(_download_retry_attempts()))
                    last_err: Optional[Exception] = None
                    download_msg = msg
                    for attempt in range(1, attempts + 1):
                        try:
                            global _ACTIVE_MEDIA_DOWNLOADS
                            if _atomic_replace_media() and msg_id > 0:
                                # 用固定临时文件名，避免重试/并发导致同一 msg_id 产生一堆 .tmp_* 残留文件
                                tmp_path = prepared_group.local_dir / f".tmp_{msg_id_prefix}{ext}"
                                try:
                                    for p in prepared_group.local_dir.glob(f".tmp_{msg_id_prefix}_*{ext}"):
                                        if p.is_file():
                                            p.unlink(missing_ok=True)
                                except Exception:
                                    pass
                                try:
                                    _ACTIVE_MEDIA_DOWNLOADS += 1
                                    try:
                                        await _download_media_with_stall_guard(
                                            client,
                                            download_msg,
                                            file=str(tmp_path),
                                            total_timeout_s=total_timeout_s,
                                            stall_timeout_s=_download_stall_seconds(),
                                        )
                                    finally:
                                        _ACTIVE_MEDIA_DOWNLOADS = max(0, int(_ACTIVE_MEDIA_DOWNLOADS) - 1)
                                    try:
                                        os.replace(str(tmp_path), str(final_path))
                                    finally:
                                        if tmp_path.exists():
                                            tmp_path.unlink(missing_ok=True)

                                    if _verify_media_size():
                                        _verify_downloaded_file_size(
                                            final_path,
                                            expected_size,
                                            label=f"source={source_row.get('id')} msg_id={msg_id_prefix}",
                                        )

                                    # 清理 Telethon 可能生成的重复文件（如 "9225 (1).mp4"）
                                    try:
                                        for p in prepared_group.local_dir.glob(f"{msg_id_prefix} (*).*"):
                                            if p.is_file():
                                                p.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                finally:
                                    try:
                                        if tmp_path.exists():
                                            tmp_path.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                            else:
                                target = prepared_group.local_dir / msg_id_prefix
                                _ACTIVE_MEDIA_DOWNLOADS += 1
                                try:
                                    await _download_media_with_stall_guard(
                                        client,
                                        download_msg,
                                        file=str(target),
                                        total_timeout_s=total_timeout_s,
                                        stall_timeout_s=_download_stall_seconds(),
                                    )
                                finally:
                                    _ACTIVE_MEDIA_DOWNLOADS = max(0, int(_ACTIVE_MEDIA_DOWNLOADS) - 1)
                                if _verify_media_size() and final_path.exists():
                                    _verify_downloaded_file_size(
                                        final_path,
                                        expected_size,
                                        label=f"source={source_row.get('id')} msg_id={msg_id_prefix}",
                                    )
                            last_err = None
                            break
                        except errors.FileReferenceExpiredError as e:
                            last_err = e
                            logger.warning(
                                "文件引用过期，将刷新消息后重试: source=%s group_uid=%s msg_id=%s (%s/%s)",
                                str(source_row.get("id")),
                                prepared_group.group_uid,
                                str(msg_id),
                                str(attempt),
                                str(attempts),
                            )
                            try:
                                refreshed = await client.get_messages(download_msg.peer_id, ids=download_msg.id)
                                if refreshed:
                                    download_msg = refreshed
                                    continue
                            except Exception as fetch_exc:
                                last_err = fetch_exc
                            break
                        except asyncio.TimeoutError as e:
                            last_err = e
                            logger.warning(
                                "下载超时（%ss），将重试: source=%s group_uid=%s msg_id=%s expected=%s (%s/%s) reason=%s",
                                int(timeout_s),
                                str(source_row.get("id")),
                                prepared_group.group_uid,
                                str(msg_id),
                                str(expected_size or 0),
                                str(attempt),
                                str(attempts),
                                str(e) or e.__class__.__name__,
                            )
                        except ConnectionError as e:
                            last_err = e
                            logger.warning(
                                "连接错误，将重试: source=%s group_uid=%s msg_id=%s (%s/%s) error=%s",
                                str(source_row.get("id")),
                                prepared_group.group_uid,
                                str(msg_id),
                                str(attempt),
                                str(attempts),
                                str(e),
                            )
                            # 允许单文件重试，不直接 break
                        except Exception as e:
                            last_err = e
                            logger.error(
                                "下载异常，将重试一次: source=%s group_uid=%s msg_id=%s (%s/%s) error=%s",
                                str(source_row.get("id")),
                                prepared_group.group_uid,
                                str(msg_id),
                                str(attempt),
                                str(attempts),
                                str(e),
                            )
                            # 允许重试一次，避免偶发性错误

                    if last_err is not None:
                        raise last_err
                finally:
                    sem.release()

            tasks = [asyncio.create_task(_download_one(msg)) for msg in messages]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            first_err = next((r for r in results if isinstance(r, Exception)), None)
            if first_err is None:
                # 额外校验：相册可能部分成功部分失败；确保本组每条媒体都已落盘
                try:
                    _verify_group_files(prepared_group, int(source_row.get("id") or 0), messages)
                    break  # 验证通过，退出重试循环
                except ValueError as e:
                    # 验证失败，转换为可重试错误
                    first_err = e
                    logger.warning(f"媒体组验证失败，将重试: {e}")

            # 只对"断线/超时/验证失败"做组级重试；其他错误直接失败
            retryable = isinstance(first_err, (asyncio.TimeoutError, TimeoutError, ConnectionError, ValueError))
            if not retryable or group_attempt >= group_attempts:
                raise first_err

            # 记录本次错误，下次重试时会清空目录
            last_group_err = first_err
            err_text = str(first_err) or repr(first_err) or first_err.__class__.__name__
            logger.warning(
                "媒体组下载异常，将断开并重连后重试：%s（重试=%s/%s）",
                err_text,
                str(group_attempt),
                str(group_attempts),
            )
            try:
                await client.disconnect()
            except Exception:
                pass
            if group_delay > 0:
                await asyncio.sleep(group_delay)
            if not await ensure_connected(client, "Downloader"):
                logger.warning("Downloader 重连未成功，将继续下一次重试")

        db.add_download_group(
            source_id=source_row["id"],
            group_uid=prepared_group.group_uid,
            date=prepared_group.date_str,
            seq=prepared_group.seq,
            last_message_id=prepared_group.last_message_id,
            local_dir=str(prepared_group.local_dir),
            remote_dir=prepared_group.remote_dir,
            status="downloaded",
        )

        db.update_download_source_last_message(source_row["id"], prepared_group.last_message_id)
        # 用本组的消息范围展示，更符合“下载了哪些”的直觉
        msg_ids = [int(getattr(m, "id", 0) or 0) for m in messages]
        msg_ids = [mid for mid in msg_ids if mid > 0]
        min_id = min(msg_ids) if msg_ids else 0
        max_id = max(msg_ids) if msg_ids else 0
        logger.info(
            "下载完成：资源数量=%s date=%s ID=%s-%s",
            str(len(messages)),
            str(prepared_group.date_str),
            str(min_id),
            str(max_id),
        )

        upload_fut: Optional[asyncio.Future[bool]] = None
        if cfg.rclone_enabled and prepared_group.remote_dir:
            if prepared_group.group_folder:
                upload_fut = await uploader.enqueue(
                    source_row["id"],
                    prepared_group.group_uid,
                    prepared_group.local_dir,
                    prepared_group.remote_dir,
                )
            else:
                media_msg = next((m for m in messages if getattr(m, "media", None)), None)
                msg_id = int(getattr(media_msg, "id", 0) or 0) if media_msg is not None else 0
                if msg_id <= 0:
                    logger.warning(
                        "单媒体组无法确定 msg_id，跳过 rclone 上传: source=%s group_uid=%s",
                        str(source_row.get("id")),
                        str(prepared_group.group_uid),
                    )
                else:
                    ext = _guess_media_ext(media_msg)
                    upload_path = prepared_group.local_dir / f"{msg_id}{ext}"
                    upload_fut = await uploader.enqueue(
                        source_row["id"],
                        prepared_group.group_uid,
                        upload_path,
                        prepared_group.remote_dir,
                    )
        return True, int(min_id or 0), upload_fut

    except FloodWaitError as e:
        if prepared_group:
            db.add_download_group(
                source_id=source_row["id"],
                group_uid=prepared_group.group_uid,
                date=prepared_group.date_str,
                seq=prepared_group.seq,
                last_message_id=prepared_group.last_message_id,
                local_dir=str(prepared_group.local_dir),
                remote_dir=prepared_group.remote_dir,
                status="failed",
                error_message=f"FloodWait: {e}",
            )
        logger.warning(f"触发 FloodWait，等待 {e.seconds}s")
        await asyncio.sleep(int(e.seconds))
        return False, int(min_id or 0), None
    except Exception as e:
        if prepared_group:
            db.add_download_group(
                source_id=source_row["id"],
                group_uid=prepared_group.group_uid,
                date=prepared_group.date_str,
                seq=prepared_group.seq,
                last_message_id=prepared_group.last_message_id,
                local_dir=str(prepared_group.local_dir),
                remote_dir=prepared_group.remote_dir,
                status="failed",
                error_message=str(e),
            )
        logger.error(f"下载失败: {e}", exc_info=True)
        return False, int(min_id or 0), None
    finally:
        if group_semaphore is not None:
            try:
                group_semaphore.release()
            except Exception:
                pass


def _prepare_download_group(
    cfg: AppConfig,
    db: Database,
    source_row: dict,
    messages: List,
    header_text: Optional[str] = None,
) -> Optional[PreparedDownloadGroup]:
    if not messages:
        return None
    if not any(getattr(m, "media", None) for m in messages):
        return None

    chat_id = int(source_row["chat_id"])
    topic_root_message_id = source_row.get("topic_id") or source_row.get("topic_root_message_id")
    grouped_id = getattr(messages[0], "grouped_id", None)
    is_album = grouped_id is not None

    folder_id = int(min(int(m.id) for m in messages))
    preferred_folder = f"TG@BieLaoZai_{folder_id}"

    group_uid = build_group_uid(chat_id, topic_root_message_id, grouped_id, messages[0].id)
    existing = db.get_download_group(int(source_row["id"]), str(group_uid))
    if existing:
        # 不再根据数据库状态跳过，完全依赖文件级别检查
        # 但复用已有的 seq/目录/日期，避免编号跳号和目录重复
        date_str = str(existing.get("date") or "")
        seq = int(existing.get("seq") or 0)
        local_dir_raw = existing.get("local_dir")
        local_dir = Path(str(local_dir_raw)) if local_dir_raw else None

        text_for_date = header_text or _pick_text_for_date(messages)
        extracted_date = extract_date_yyyy_mm_dd(text_for_date)
        if extracted_date and extracted_date != date_str:
            date_str = extracted_date
            seq = 0
            local_dir = None

        if not date_str:
            fallback_date = _message_send_date_yyyy_mm_dd(messages[0])
            if fallback_date:
                date_str = fallback_date
            else:
                logger.warning(
                    "未从标题/文本提取到日期，将使用占位日期=%s（source_id=%s group_uid=%s msg_id=%s）",
                    UNPARSED_DATE,
                    str(source_row.get("id")),
                    str(group_uid),
                    str(getattr(messages[0], "id", "")),
                )
                date_str = UNPARSED_DATE

        if seq <= 0 or local_dir is None:
            seq = folder_id

        source_title = sanitize_path_component(source_row.get("display_title") or f"chat_{abs(chat_id)}")
        base_dir = cfg.download_root_dir / source_title / date_str
        if local_dir is not None:
            group_folder = "" if local_dir.name == date_str else local_dir.name
        else:
            group_folder = preferred_folder if is_album else ""
        if local_dir is None:
            local_dir = base_dir / group_folder if group_folder else base_dir

        remote_dir = existing.get("remote_dir")
        # 兼容旧数据：remote_dir 若缺少 `<name>:` 前缀则视为无效，重新按当前配置生成
        if cfg.rclone_enabled and remote_dir and (not is_valid_rclone_remote_path(str(remote_dir))):
            remote_dir = None
        if cfg.rclone_enabled and not remote_dir:
            remote_base = cfg.rclone_remote_base_dir.strip("/\\") if cfg.rclone_remote_base_dir else ""
            remote_rel = "/".join(part for part in [remote_base, source_title, date_str, group_folder] if part)
            remote_dir = f"{cfg.rclone_remote}{remote_rel}"

        last_message_id = max(int(m.id) for m in messages)
        # 如果我们上面修正过 date/seq/local_dir，则把元数据先写回，避免下次重试仍走旧日期
        try:
            if str(existing.get("date") or "") != str(date_str) or int(existing.get("seq") or 0) != int(seq):
                db.add_download_group(
                    source_id=source_row["id"],
                    group_uid=str(group_uid),
                    date=str(date_str),
                    seq=int(seq),
                    last_message_id=int(last_message_id),
                    local_dir=str(local_dir),
                    remote_dir=str(remote_dir) if remote_dir else None,
                    status="failed",
                    error_message=str(existing.get("error_message") or "").strip() or None,
                )
        except Exception:
            pass
        return PreparedDownloadGroup(
            group_uid=str(group_uid),
            date_str=str(date_str),
            seq=int(seq),
            group_folder=str(group_folder),
            local_dir=local_dir,
            remote_dir=str(remote_dir) if remote_dir else None,
            last_message_id=int(last_message_id),
        )

    text_for_date = header_text or _pick_text_for_date(messages)
    extracted_date = extract_date_yyyy_mm_dd(text_for_date)
    if not extracted_date:
        fallback_date = _message_send_date_yyyy_mm_dd(messages[0])
        if fallback_date:
            date_str = fallback_date
        else:
            logger.warning(
                "未从标题/文本提取到日期，将使用占位日期=%s（source_id=%s group_uid=%s msg_id=%s）",
                UNPARSED_DATE,
                str(source_row.get("id")),
                str(group_uid),
                str(getattr(messages[0], "id", "")),
            )
            date_str = UNPARSED_DATE
    else:
        date_str = extracted_date

    seq = folder_id
    group_folder = preferred_folder if is_album else ""

    source_title = sanitize_path_component(source_row.get("display_title") or f"chat_{abs(chat_id)}")
    base_dir = cfg.download_root_dir / source_title / date_str
    local_dir = base_dir / group_folder if group_folder else base_dir

    remote_dir = None
    if cfg.rclone_enabled:
        # cfg.rclone_remote 在 load_config 中已规范化为 `<name>:`
        remote_base = cfg.rclone_remote_base_dir.strip("/\\") if cfg.rclone_remote_base_dir else ""
        remote_rel = "/".join(part for part in [remote_base, source_title, date_str, group_folder] if part)
        remote_dir = f"{cfg.rclone_remote}{remote_rel}"

    last_message_id = max(int(m.id) for m in messages)

    return PreparedDownloadGroup(
        group_uid=str(group_uid),
        date_str=str(date_str),
        seq=int(seq),
        group_folder=str(group_folder),
        local_dir=local_dir,
        remote_dir=remote_dir,
        last_message_id=int(last_message_id),
    )


def _message_in_topic(message, topic_root_message_id: int) -> bool:
    topic_root_message_id = int(topic_root_message_id or 0)
    if topic_root_message_id <= 0:
        return False

    # 某些客户端/消息类型（尤其是相册 grouped_id）在 forum 话题内可能没有 reply_to header；
    # 这里做多路径提取，尽量稳定识别“属于哪个话题(thread/top msg id)”。
    top_id: Optional[int] = None
    reply_id: Optional[int] = None

    header = getattr(message, "reply_to", None)
    if header is not None:
        top_id = getattr(header, "reply_to_top_id", None)
        if top_id is None:
            top_id = getattr(header, "top_msg_id", None)
        reply_id = getattr(header, "reply_to_msg_id", None)

    if top_id is None:
        top_id = getattr(message, "reply_to_top_id", None)
    if top_id is None:
        top_id = getattr(message, "top_msg_id", None)
    if reply_id is None:
        reply_id = getattr(message, "reply_to_msg_id", None)

    # 话题根消息本身可能没有 reply_to，但它的 message.id 就是 thread/top id
    msg_id = int(getattr(message, "id", 0) or 0)

    return int(top_id or reply_id or msg_id or 0) == topic_root_message_id


class DownloaderRuntime:
    """
    下载器运行时：
    - 动态读取数据库中启用的来源（is_active=1）
    - 监听所有新消息，按来源过滤，仅下载媒体
    - 提供按来源“克隆（历史回填）→ 自动开启监听”的能力
    """

    def __init__(self, user_client: TelegramClient, cfg: AppConfig, db: Database) -> None:
        self._client = user_client
        self._cfg = cfg
        self._db = db
        self._uploader = RcloneUploader(cfg, db)
        self._download_semaphore = asyncio.Semaphore(int(cfg.max_download_concurrency))
        self._group_semaphore = asyncio.Semaphore(int(cfg.max_group_concurrency))

        self._collector = MediaGroupCollector(
            flush_seconds=cfg.media_group_flush_seconds,
            on_group=self._on_media_group,
        )

        self._sources_cache_ts = 0.0
        self._sources_cache_ttl = float(_env_float("SOURCES_CACHE_TTL_SECONDS", 5.0))
        self._sources_by_chat: Dict[int, List[dict]] = {}

        self._clone_tasks: Dict[int, asyncio.Task] = {}
        self._clone_progress: Dict[int, dict] = {}
        self._clone_ui_callbacks: Dict[int, Callable] = {}  # UI更新回调
        self._last_header_text: Dict[int, str] = {}
        self._handler_installed = False

    def start(self) -> None:
        self._uploader.start()
        if not self._handler_installed:
            self._client.add_event_handler(self._on_new_message, events.NewMessage)
            self._handler_installed = True

    async def stop(self) -> None:
        if self._handler_installed:
            try:
                self._client.remove_event_handler(self._on_new_message, events.NewMessage)
            except Exception:
                pass
            self._handler_installed = False
        await self._uploader.stop()

        for task in list(self._clone_tasks.values()):
            if not task.done():
                task.cancel()
        self._clone_tasks.clear()

    def is_cloning(self, source_id: int) -> bool:
        task = self._clone_tasks.get(int(source_id))
        return bool(task and not task.done())

    def get_clone_progress(self, source_id: int) -> Optional[dict]:
        p = self._clone_progress.get(int(source_id))
        return dict(p) if p else None

    def _update_clone_progress(self, source_id: int, **fields) -> None:
        source_id = int(source_id)
        current = self._clone_progress.get(source_id) or {}

        # 检测 groups_done 是否真的变化了
        old_groups_done = current.get('groups_done', 0)
        new_groups_done = fields.get('groups_done', old_groups_done)
        groups_done_changed = (new_groups_done != old_groups_done)

        current.update(fields)
        current["updated_at"] = float(time.time())
        self._clone_progress[source_id] = current

        # 只在 groups_done 真正变化时触发UI回调
        if source_id in self._clone_ui_callbacks and groups_done_changed:
            try:
                callback = self._clone_ui_callbacks[source_id]
                # 异步调用回调
                asyncio.create_task(callback(source_id, current))
            except Exception as e:
                logger.warning(f"UI回调执行失败: {e}")

    def register_clone_ui_callback(self, source_id: int, callback: Callable) -> None:
        """注册克隆进度UI更新回调"""
        self._clone_ui_callbacks[int(source_id)] = callback

    def unregister_clone_ui_callback(self, source_id: int) -> None:
        """注销克隆进度UI更新回调"""
        self._clone_ui_callbacks.pop(int(source_id), None)

    def refresh_sources_cache(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._sources_cache_ts) < self._sources_cache_ttl:
            return

        sources = self._db.list_download_sources(active_only=True)
        by_chat: Dict[int, List[dict]] = {}
        for s in sources:
            by_chat.setdefault(int(s["chat_id"]), []).append(s)
        self._sources_by_chat = by_chat
        self._sources_cache_ts = now

    async def download_media_messages(self, source_id: int, messages: List, header_text: Optional[str] = None) -> None:
        source = self._db.get_download_source(int(source_id))
        if not source:
            raise RuntimeError("来源不存在")
        await download_media_group(
            self._client,
            self._cfg,
            self._db,
            source,
            messages,
            self._uploader,
            download_semaphore=self._download_semaphore,
            group_semaphore=self._group_semaphore,
            header_text=header_text,
        )

    async def download_group_by_message_id(self, source_id: int, message_id: int) -> None:
        source = self._db.get_download_source(int(source_id))
        if not source:
            raise RuntimeError("来源不存在")

        entity = await self._client.get_entity(int(source["chat_id"]))
        seed = await self._client.get_messages(entity, ids=int(message_id))
        if not seed:
            raise RuntimeError("未获取到该消息（可能无权限或消息不存在）")

        topic_root = int(source.get("topic_id") or source.get("topic_root_message_id") or 0)
        if source["source_type"] == "forum_topic" and topic_root:
            if not _message_in_topic(seed, topic_root):
                raise RuntimeError("该消息不属于当前话题")

        if not getattr(seed, "media", None):
            raise RuntimeError("该消息不是媒体消息")

        gid = getattr(seed, "grouped_id", None)
        messages: List = [seed]
        if gid:
            window_min = max(0, int(seed.id) - 300)
            window_max = int(seed.id) + 300
            uniq: Dict[int, object] = {int(seed.id): seed}
            async for m in self._client.iter_messages(
                entity,
                min_id=window_min,
                max_id=window_max,
                reverse=True,
                reply_to=None,
            ):
                if not getattr(m, "media", None):
                    continue
                if source["source_type"] == "forum_topic" and topic_root:
                    if not _message_in_topic(m, topic_root):
                        continue
                if getattr(m, "grouped_id", None) == gid:
                    uniq[int(m.id)] = m
            messages = [uniq[k] for k in sorted(uniq.keys())]

        await self.download_media_messages(int(source_id), messages, header_text=None)

    async def _on_new_message(self, event) -> None:
        message = event.message

        self.refresh_sources_cache()
        candidates = self._sources_by_chat.get(int(event.chat_id), [])
        if not candidates:
            return

        msg_text = (getattr(message, "raw_text", None) or getattr(message, "message", None) or "").strip()
        has_media = bool(getattr(message, "media", None))

        for source in candidates:
            source_id = int(source["id"])
            if self.is_cloning(source_id):
                continue

            if source["source_type"] == "forum_topic":
                topic_root = int(source.get("topic_id") or source.get("topic_root_message_id") or 0)
                if not topic_root or not _message_in_topic(message, topic_root):
                    continue

            # 记录“标题/说明”文本，供后续媒体组抽取日期（很多频道是先发文字再连发多条媒体）
            if (not has_media) and msg_text:
                self._last_header_text[source_id] = msg_text
                continue

            if not has_media:
                continue

            group_key = int(getattr(message, "grouped_id", None) or message.id)
            flush_seconds = float(self._cfg.media_group_flush_seconds)

            self._collector.add(
                source_id,
                group_key,
                message,
                header_text=self._last_header_text.get(source_id),
                flush_seconds=flush_seconds,
            )

    async def _on_media_group(self, source_id: int, group_key: int, header_text: Optional[str], messages: List) -> None:
        source = self._db.get_download_source(int(source_id))
        if not source:
            return
        await download_media_group(
            self._client,
            self._cfg,
            self._db,
            source,
            messages,
            self._uploader,
            download_semaphore=self._download_semaphore,
            group_semaphore=self._group_semaphore,
            header_text=header_text,
        )

    def start_clone(
        self,
        source_id: int,
        auto_listen: bool = True,
        start_from_message_id: Optional[int] = None,
    ) -> asyncio.Task:
        source_id = int(source_id)
        existing = self._clone_tasks.get(source_id)
        if existing and not existing.done():
            return existing

        # 预先计算初始消息ID,避免UI显示延迟
        source = self._db.get_download_source(source_id)
        if not source:
            raise RuntimeError("来源不存在")

        if start_from_message_id is not None:
            init_msg_id = max(1, int(start_from_message_id or 1))
        else:
            last_msg = int(source.get("last_message_id") or 0)
            # 对于话题,如果 last_message_id 为 0,尝试使用话题起始ID
            if last_msg == 0 and source.get("source_type") == "forum_topic":
                topic_start_id = int(source.get("topic_root_message_id") or source.get("topic_id") or 0)
                if topic_start_id > 0:
                    init_msg_id = topic_start_id
                else:
                    init_msg_id = 1
            else:
                init_msg_id = max(1, last_msg + 1)

        self._update_clone_progress(
            source_id,
            status="queued",
            start_from_message_id=int(start_from_message_id or init_msg_id),
            current_message_id=init_msg_id,  # 立即设置初始消息ID
            groups_done=0,
            messages_scanned=0,
            total_messages=0,
        )

        task = asyncio.create_task(
            self.clone_source(
                source_id,
                auto_listen=auto_listen,
                start_from_message_id=start_from_message_id,
            )
        )
        self._clone_tasks[source_id] = task
        return task

    def stop_clone(self, source_id: int) -> bool:
        source_id = int(source_id)
        task = self._clone_tasks.get(source_id)
        if not task or task.done():
            return False
        task.cancel()
        self._update_clone_progress(source_id, status="cancelled", finished_at=float(time.time()))
        return True

    async def clone_source(
        self,
        source_id: int,
        auto_listen: bool = True,
        start_from_message_id: Optional[int] = None,
    ) -> None:
        source_id = int(source_id)
        source = self._db.get_download_source(source_id)
        if not source:
            raise RuntimeError("来源不存在")

        # 清理旧的失败记录，避免重启后显示旧的失败数据
        self._db.clear_failed_download_groups(source_id)

        if start_from_message_id is not None:
            start_from = int(start_from_message_id)
            if start_from > 0:
                # 无论起始ID大于、小于还是等于已记录的 last_message_id，都重置为指定位置
                # 确保从用户指定的起始ID开始克隆（Telethon min_id 是严格大于，所以用 start-1）
                self._db.set_download_source_last_message(source_id, max(0, start_from - 1))
                source = self._db.get_download_source(source_id) or source

        # 计算初始消息 ID,确保至少为 1,避免显示"计算中"
        # 注意:start_clone 中已经预设了初始值,这里可以复用或重新计算(覆盖)
        # 对于话题(forum_topic),起始ID应该基于话题的起始消息ID(reply_to)
        prev_progress = self._clone_progress.get(source_id) or {}
        if start_from_message_id is not None:
            init_msg_id = max(1, int(start_from_message_id or 1))
        elif int(prev_progress.get("current_message_id") or 0) > 0:
            # 如果已经有初始值(在start_clone中设置),就复用
            init_msg_id = int(prev_progress.get("current_message_id"))
        else:
            last_msg = int(source.get("last_message_id") or 0)
            # 对于话题,如果 last_message_id 为 0,尝试使用话题起始ID
            if last_msg == 0 and source.get("source_type") == "forum_topic":
                topic_start_id = int(source.get("topic_root_message_id") or source.get("topic_id") or 0)
                if topic_start_id > 0:
                    init_msg_id = topic_start_id
                else:
                    init_msg_id = 1
            else:
                init_msg_id = max(1, last_msg + 1)

        self._update_clone_progress(
            source_id,
            status="starting",
            started_at=float(time.time()),
            groups_done=0,
            messages_scanned=0,  # 已遍历的消息数
            total_messages=0,    # 预估的总消息数
            current_message_id=init_msg_id,
            latest_message_id=0,
            start_from_message_id=int(start_from_message_id or init_msg_id),
        )

        # 克隆期间暂停监听，避免并发重复下载；仅在成功后再开启监听
        self._db.set_download_source_active(source_id, False)
        ok = False
        attempts = max(1, int(_clone_retry_attempts()))
        last_err: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                if not await ensure_connected(self._client, "Clone"):
                    raise ConnectionError("Telegram 未连接或未授权")

                # 预估最新消息ID和总消息数（用于进度展示）
                try:
                    entity = await self._client.get_entity(int(source["chat_id"]))
                    reply_to = None
                    if source["source_type"] == "forum_topic" and source.get("topic_root_message_id"):
                        reply_to = int(source["topic_root_message_id"])

                    latest_id = 0
                    async for m in self._client.iter_messages(entity, limit=1, reply_to=reply_to):
                        latest_id = int(getattr(m, "id", 0) or 0)
                        break

                    # 计算预估总消息数（最新ID - 起始ID，这只是粗略估计）
                    estimated_total = max(0, latest_id - init_msg_id + 1) if latest_id > 0 else 0

                    self._update_clone_progress(
                        source_id,
                        latest_message_id=latest_id,
                        total_messages=estimated_total
                    )
                except Exception as e:
                    self._update_clone_progress(source_id, latest_message_id=0, latest_error=str(e))

                groups_done = 0
                messages_scanned = 0  # 已遍历的消息数

                def on_message_progress(scanned_count: int = 0, is_group_done: bool = False, current_msg_id: int = 0) -> None:
                    """更新克隆进度
                    Args:
                        scanned_count: 已遍历的消息数（累计）
                        is_group_done: 是否是媒体组完成的回调（True=媒体组完成，False=仅消息遍历）
                        current_msg_id: 当前消息ID（用于日志）
                    """
                    nonlocal groups_done, messages_scanned
                    # 更新已遍历消息数
                    if scanned_count > 0:
                        messages_scanned = scanned_count

                    # 只在媒体组完成时增加计数
                    if is_group_done:
                        groups_done += 1
                        logger.info(f"媒体组上传完成: 已完成组数={groups_done}")

                    prev = self._clone_progress.get(int(source_id)) or {}
                    new_msg_id = int(current_msg_id) if int(current_msg_id or 0) > 0 else int(prev.get("current_message_id") or 0)
                    logger.info(f"更新克隆进度: source_id={source_id} status=running groups_done={groups_done} messages_scanned={messages_scanned} current_message_id={new_msg_id}")
                    self._update_clone_progress(
                        source_id,
                        status="running",
                        groups_done=groups_done,
                        messages_scanned=messages_scanned,
                        current_message_id=new_msg_id,
                    )

                await backfill_single_source(
                    self._client,
                    self._cfg,
                    self._db,
                    source,
                    self._uploader,
                    download_semaphore=self._download_semaphore,
                    group_semaphore=self._group_semaphore,
                    start_from_message_id=start_from_message_id,
                    progress_hook=on_message_progress,
                )
                # 再跑一次兜底 catch-up，降低"克隆过程中新增消息漏掉"的概率
                source2 = self._db.get_download_source(source_id) or source
                await backfill_single_source(
                    self._client,
                    self._cfg,
                    self._db,
                    source2,
                    self._uploader,
                    download_semaphore=self._download_semaphore,
                    group_semaphore=self._group_semaphore,
                    start_from_message_id=None,
                    progress_hook=on_message_progress,
                )

                self._update_clone_progress(source_id, status="done", finished_at=float(time.time()))
                ok = True
                last_err = None
                break
            except asyncio.CancelledError:
                self._update_clone_progress(source_id, status="cancelled", finished_at=float(time.time()))
                raise
            except ConnectionError as e:
                last_err = e
                self._update_clone_progress(source_id, status="retrying", last_error=str(e))
                logger.warning(
                    "克隆遇到断线，将重连后重试: source=%s (%s/%s) err=%s",
                    str(source_id),
                    str(attempt),
                    str(attempts),
                    str(e),
                )
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(min(30.0, 2.0 * attempt))
                continue
            except Exception as e:
                last_err = e
                break

        if not ok:
            err = last_err or RuntimeError("克隆失败（未知原因）")
            self._update_clone_progress(source_id, status="failed", last_error=str(err), finished_at=float(time.time()))
            logger.error(f"克隆失败: source={source_id} err={err}", exc_info=True)
            raise err
        if auto_listen and ok:
            self._db.set_download_source_active(source_id, True)
        self.refresh_sources_cache(force=True)


async def run_downloader(backfill: bool) -> None:
    cfg = load_config()
    setup_logging(debug=_env_bool("DEBUG", False))

    Path("sessions").mkdir(parents=True, exist_ok=True)
    cfg.download_root_dir.mkdir(parents=True, exist_ok=True)

    db = Database()

    supervise_reconnect = _env_bool("TG_SUPERVISE_RECONNECT", True)
    reconnect_delay = float(_env_float("TG_RECONNECT_DELAY_INITIAL_SECONDS", 2.0))
    reconnect_delay_max = float(_env_float("TG_RECONNECT_DELAY_MAX_SECONDS", 60.0))

    client = await ensure_user_client(cfg)

    runtime = DownloaderRuntime(client, cfg, db)
    runtime.start()

    if backfill:
        # CLI 模式：对“所有来源”执行一次克隆，然后自动开启监听
        sources = db.list_download_sources(active_only=False)
        if not sources:
            logger.warning("当前没有任何来源，请先运行: python manage_sources.py 或使用管理 Bot 添加来源")
        for s in sources:
            await runtime.clone_source(int(s["id"]), auto_listen=True)

    logger.info("开始监听新消息（仅媒体）")
    try:
        while True:
            watchdog = asyncio.create_task(_connection_watchdog(client, "Downloader"))
            await client.run_until_disconnected()
            if not watchdog.done():
                watchdog.cancel()
                try:
                    await watchdog
                except Exception:
                    pass
            if not supervise_reconnect:
                break
            logger.warning("检测到 Telegram 连接已断开，%ss 后尝试重连…", int(reconnect_delay))
            await asyncio.sleep(float(reconnect_delay))
            reconnect_delay = min(reconnect_delay * 2.0, reconnect_delay_max)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    logger.info("Telegram 连接已恢复")
                    reconnect_delay = float(_env_float("TG_RECONNECT_DELAY_INITIAL_SECONDS", 2.0))
                    continue
            except Exception as e:
                logger.warning("重连失败：%s", str(e))
                continue
    finally:
        await runtime.stop()
        await client.disconnect()


async def backfill_sources(
    client: TelegramClient,
    cfg: AppConfig,
    db: Database,
    sources: List[dict],
    uploader: RcloneUploader,
    download_semaphore: Optional[asyncio.Semaphore] = None,
) -> None:
    logger.info("开始历史克隆下载（按媒体组）")

    for source in sources:
        await backfill_single_source(
            client,
            cfg,
            db,
            source,
            uploader,
            download_semaphore=download_semaphore,
            group_semaphore=None,
            start_from_message_id=None,
        )


async def backfill_single_source(
    client: TelegramClient,
    cfg: AppConfig,
    db: Database,
    source: dict,
    uploader: RcloneUploader,
    download_semaphore: Optional[asyncio.Semaphore] = None,
    group_semaphore: Optional[asyncio.Semaphore] = None,
    start_from_message_id: Optional[int] = None,
    progress_hook: Optional[Callable[..., None]] = None,
) -> None:
    chat_id = int(source["chat_id"])
    last_message_id = int(source.get("last_message_id") or 0)
    if start_from_message_id is not None:
        # Telethon 的 min_id 是“严格大于”，这里用 start-1 实现包含 start
        last_message_id = max(0, int(start_from_message_id) - 1)

    logger.info(f"历史克隆: source={source['id']} chat_id={chat_id} last_message_id={last_message_id}")

    entity = await client.get_entity(chat_id)
    reply_to = None
    if source["source_type"] == "forum_topic":
        # Forum 话题：topic_id 才是 thread_id（链接里的那段）；topic_root_message_id 可能是旧/错数据
        thread_id = int(source.get("topic_id") or source.get("topic_root_message_id") or 0)
        if thread_id:
            reply_to = thread_id

    pending_group_key: Optional[int] = None
    pending_messages: List = []
    pending_header_text: Optional[str] = None
    last_header_text: Optional[str] = None

    inflight: Set[asyncio.Task] = set()
    max_inflight_groups = max(5, int(cfg.max_group_concurrency) * 2)
    inflight_progress_ids: Set[int] = set()
    upload_inflight: Set[asyncio.Future[bool]] = set()

    async def _drain_inflight_if_needed() -> None:
        nonlocal inflight
        if len(inflight) < max_inflight_groups:
            return
        done, pending = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
        inflight = set(pending)
        # 触发异常也不影响整体流程（download_media_group 内部会记录失败）
        for t in done:
            try:
                _ = t.exception()
            except Exception:
                pass

    def _reset_pending() -> None:
        nonlocal pending_group_key, pending_messages, pending_header_text
        pending_group_key = None
        pending_messages = []
        pending_header_text = None

    async def _flush_pending() -> None:
        nonlocal message_count  # 声明访问外层的 message_count
        if not pending_messages:
            _reset_pending()
            return

        await _drain_inflight_if_needed()

        messages_snapshot = list(pending_messages)
        header_snapshot = pending_header_text
        # 使用“当前未上传完成的并发窗口最小消息ID”作为进度标记
        min_id = int(min(m.id for m in messages_snapshot))
        last_id = int(max(m.id for m in messages_snapshot))
        _reset_pending()

        # 这里按"消息遍历顺序"预分配日期/编号，避免并发导致 TG@BieLaoZai_002/009 这种乱序编号
        # 运行中可能发生"重命名来源"等变更；这里每次落盘前以数据库最新值为准（主要是 display_title 影响目录名）。
        source_latest = db.get_download_source(int(source.get("id") or 0)) or source
        prepared = _prepare_download_group(cfg, db, source_latest, messages_snapshot, header_text=header_snapshot)
        if not prepared:
            return

        task = asyncio.create_task(
            download_media_group(
                client,
                cfg,
                db,
                source_latest,
                messages_snapshot,
                uploader,
                download_semaphore=download_semaphore,
                group_semaphore=group_semaphore,
                header_text=header_snapshot,
                prepared=prepared,
            )
        )

        inflight_progress_ids.add(min_id)

        def _notify_progress(is_group_done: bool, fallback_current: int) -> None:
            if not progress_hook:
                return
            current = int(min(inflight_progress_ids)) if inflight_progress_ids else int(fallback_current or 0)
            try:
                progress_hook(scanned_count=message_count, is_group_done=is_group_done, current_msg_id=current)
            except Exception:
                pass

        # 下载完成后：若启用 rclone，则等待 rclone 上传 future 完成再推进进度
        if progress_hook:
            def on_download_done(t: asyncio.Task) -> None:
                try:
                    if t.cancelled():
                        inflight_progress_ids.discard(min_id)
                        _notify_progress(is_group_done=False, fallback_current=min_id)
                        return
                    if t.exception() is not None:
                        inflight_progress_ids.discard(min_id)
                        _notify_progress(is_group_done=False, fallback_current=min_id)
                        return

                    ok, _pid, upload_fut = t.result()
                    if not ok:
                        inflight_progress_ids.discard(min_id)
                        _notify_progress(is_group_done=False, fallback_current=min_id)
                        return

                    if upload_fut is None:
                        inflight_progress_ids.discard(min_id)
                        _notify_progress(is_group_done=True, fallback_current=min_id)
                        return

                    upload_inflight.add(upload_fut)

                    def on_upload_done(f: asyncio.Future[bool]) -> None:
                        try:
                            upload_inflight.discard(f)
                            upload_ok = False
                            if not f.cancelled():
                                upload_ok = bool(f.result())
                        except Exception:
                            upload_ok = False
                        inflight_progress_ids.discard(min_id)
                        _notify_progress(is_group_done=bool(upload_ok), fallback_current=min_id)

                    upload_fut.add_done_callback(on_upload_done)
                except Exception:
                    pass

            task.add_done_callback(on_download_done)

        inflight.add(task)
        await _drain_inflight_if_needed()

    async def _handle_message(msg) -> None:
        nonlocal pending_group_key, pending_messages, pending_header_text, last_header_text

        msg_text = (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()
        has_media = bool(getattr(msg, "media", None))

        # 标题/说明文本：用于后续媒体组抽取日期
        if (not has_media) and msg_text:
            last_header_text = msg_text
            # 有些话题是"先发媒体组，后补一条文字标题/日期"，这里把后补标题绑定给当前 pending 组
            if pending_group_key is not None and pending_messages:
                if (not pending_header_text) or (DATE_RE.search(pending_header_text or "") is None):
                    if DATE_RE.search(msg_text):
                        pending_header_text = msg_text
            return

        if not has_media:
            return

        group_key = int(getattr(msg, "grouped_id", None) or msg.id)
        if pending_group_key is None:
            pending_group_key = group_key
            pending_header_text = last_header_text

        if group_key != pending_group_key:
            await _flush_pending()
            pending_group_key = group_key
            pending_header_text = last_header_text

        pending_messages.append(msg)

    used_fallback = False
    message_count = 0  # 统计遍历到的消息数
    last_scanned_msg_id = 0
    try:
        logger.info(
            f"开始遍历消息: source={source['id']} min_id={last_message_id} reply_to={reply_to} source_type={source.get('source_type')}"
        )
        async for msg in client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True,
            reply_to=reply_to,
        ):
            message_count += 1
            last_scanned_msg_id = int(getattr(msg, "id", 0) or 0)
            if message_count <= 5 or message_count % 10 == 0:
                logger.info(f"遍历消息: id={msg.id} has_media={bool(getattr(msg, 'media', None))}")

            await _handle_message(msg)

        logger.info(f"消息遍历完成: source={source['id']} 共遍历 {message_count} 条消息")
    except Exception as e:
        # forum 话题在某些场景下 reply_to 会触发 GetRepliesRequest：
        # - TOPIC_ID_INVALID：话题 ID 不合法或无法访问
        # - FrozenMethodInvalidError：账号被冻结/受限，禁止使用 GetRepliesRequest
        # 兜底：改为遍历全群消息，并用 reply_to_top_id/top_msg_id 过滤话题消息（更慢但更稳）。
        if reply_to and source.get("source_type") == "forum_topic":
            if isinstance(e, errors.FrozenMethodInvalidError) or "frozen accounts" in str(e).lower():
                used_fallback = True
                logger.warning(
                    "账号受限，reply_to 遍历不可用，降级为全群遍历过滤: source=%s chat_id=%s topic_root=%s err=%s",
                    str(source.get("id")),
                    str(chat_id),
                    str(reply_to),
                    str(e),
                )
                await _flush_pending()
            elif "TOPIC_ID_INVALID" in str(e):
                used_fallback = True
                logger.warning(
                    "forum_topic reply_to 无效，降级为全群遍历过滤: source=%s chat_id=%s topic_root=%s err=%s",
                    str(source.get("id")),
                    str(chat_id),
                    str(reply_to),
                    str(e),
                )
                await _flush_pending()
            else:
                raise
        else:
            raise

    if used_fallback and reply_to:
        logger.info(f"使用 fallback 模式重新遍历: source={source['id']}")
        fallback_count = 0
        filtered_count = 0
        async for msg in client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True,
            reply_to=None,
        ):
            fallback_count += 1
            if not _message_in_topic(msg, int(reply_to)):
                filtered_count += 1
                continue
            await _handle_message(msg)
        logger.info(
            f"fallback 遍历完成: source={source['id']} 共遍历 {fallback_count} 条，过滤掉 {filtered_count} 条"
        )

    if pending_messages:
        await _flush_pending()

    if inflight:
        await asyncio.gather(*list(inflight), return_exceptions=True)
    if upload_inflight:
        await asyncio.gather(*list(upload_inflight), return_exceptions=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram 媒体组下载器（仅媒体）")
    parser.add_argument("--backfill", action="store_true", help="启动时先执行一次历史克隆下载")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        asyncio.run(run_downloader(backfill=bool(args.backfill)))
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
