#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Telegram 媒体组下载器

目标：
- 支持添加来源：频道、超级群 Forum 话题
- 仅下载媒体（含相册 grouped_id）到本地目录
- 按媒体组记录下载完成与“最后消息”进度
- 可选：下载完成后 rclone copy 上传到 remote，成功后删除本地

落盘结构：
<DOWNLOAD_ROOT_DIR>/<来源标题>/<YYYY-MM-DD>/TG@BieLaoZai_001/...

说明：
- 序号按“来源+日期”递增（001/002/...）
- 日期优先从消息文本/标题中提取 `YYYY年MM月DD日`，否则回退消息发送日期
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import secrets
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

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError

from database import Database

logger = logging.getLogger(__name__)

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

_TELETHON_DEDUP_LAST_TS: Dict[str, float] = {}
_ACTIVE_MEDIA_DOWNLOADS = 0


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


def build_proxy() -> Optional[Tuple[int, str, int, bool, Optional[str], Optional[str]]]:
    if not _env_bool("PROXY_ENABLED", False):
        return None

    if socks is None:
        logger.warning("代理已启用但未安装 PySocks（pysocks）")
        return None

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
    rclone_extra_args: List[str]


def load_config() -> AppConfig:
    load_dotenv()

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
    rclone_remote = _env_str("RCLONE_REMOTE")
    rclone_base = _env_str("RCLONE_REMOTE_BASE_DIR")
    rclone_delete = _env_bool("RCLONE_DELETE_LOCAL_AFTER_UPLOAD", True)

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


def _compute_download_timeout_seconds(expected_size: Optional[int]) -> float:
    hard = max(10.0, float(_download_timeout_seconds()))
    if expected_size is None or expected_size <= 0:
        return hard
    base = max(0.0, float(_download_timeout_base_seconds()))
    min_bps = max(1, int(_download_min_bytes_per_sec()))
    adaptive = base + (float(expected_size) / float(min_bps))
    return max(hard, adaptive)


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
            await client.connect()
        if await client.is_user_authorized():
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


def extract_date_yyyy_mm_dd(text: str, fallback_dt: datetime) -> str:
    match = DATE_RE.search(text or "")
    if match:
        y, m, d = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return f"{y:04d}-{m:02d}-{d:02d}"

    local_dt = fallback_dt
    if fallback_dt.tzinfo is not None:
        local_dt = fallback_dt.astimezone()
    return local_dt.date().isoformat()


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
        self._queue: "asyncio.Queue[Tuple[int, str, Path, str]]" = asyncio.Queue()
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

    async def enqueue(self, source_id: int, group_uid: str, local_dir: Path, remote_dir: str) -> None:
        await self._queue.put((int(source_id), str(group_uid), local_dir, remote_dir))

    async def _run(self) -> None:
        while True:
            source_id, group_uid, local_dir, remote_dir = await self._queue.get()
            try:
                ok = await self._rclone_copy(local_dir, remote_dir)
                if not ok:
                    self._db.mark_download_group_uploaded(
                        source_id, group_uid, uploaded=False, remote_dir=remote_dir, local_deleted=False
                    )
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
                                shutil.rmtree(local_dir, ignore_errors=False)
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
            except Exception as e:
                logger.error(f"rclone 上传流程异常: {e}", exc_info=True)
                self._db.mark_download_group_failed(source_id, group_uid, str(e))
            finally:
                self._queue.task_done()

    async def _rclone_copy(self, local_dir: Path, remote_dir: str) -> bool:
        args = ["rclone", "copy", str(local_dir), str(remote_dir), "--create-empty-src-dirs"]
        args.extend(self._cfg.rclone_extra_args)

        logger.info(f"开始上传: {local_dir} -> {remote_dir}")
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
) -> None:
    if not messages:
        return

    if not any(getattr(m, "media", None) for m in messages):
        return

    if group_semaphore is not None:
        await group_semaphore.acquire()
    prepared_group: Optional[PreparedDownloadGroup] = prepared
    try:
        if prepared_group is None:
            prepared_group = _prepare_download_group(cfg, db, source_row, messages, header_text=header_text)
        if not prepared_group:
            return

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
                "开始下载媒体组: source=%s(%s) group_uid=%s msgs=%s ids=%s-%s dir=%s (%s/%s)",
                str(source_row.get("id")),
                source_title,
                prepared_group.group_uid,
                str(len(messages)),
                str(min_id),
                str(max_id),
                str(prepared_group.local_dir),
                str(group_attempt),
                str(group_attempts),
            )

            # 目录清理策略：
            # 1. 首次尝试时总是清空（避免旧文件残留）
            # 2. 重试时如果上次有错误，也清空（避免部分下载残留污染重试）
            if group_attempt == 1:
                # 重启/重试时如果目录已存在，Telethon 会自动重命名(1)(2)...；这里直接清空目录实现"覆盖替换"
                ensure_empty_dir(prepared_group.local_dir)
            elif last_group_err is not None:
                # 重试时：如果上次有错误，清空目录避免残留文件污染
                logger.info(f"组级重试，清空目录避免残留: {prepared_group.local_dir}")
                ensure_empty_dir(prepared_group.local_dir)

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
                                        return  # 仅在验证通过后才跳过
                                    except ValueError:
                                        # 大小不对，文件已被删除，继续重新下载
                                        logger.info(f"文件大小验证失败，将重新下载: {final_path}")
                                        pass
                                else:
                                    return  # 不验证大小时，直接跳过
                        except Exception:
                            pass  # 其他异常（如文件损坏）也重新下载

                    timeout_s = float(_compute_download_timeout_seconds(expected_size))
                    attempts = max(1, int(_download_retry_attempts()))
                    last_err: Optional[Exception] = None
                    for attempt in range(1, attempts + 1):
                        try:
                            global _ACTIVE_MEDIA_DOWNLOADS
                            if _atomic_replace_media() and msg_id > 0:
                                tmp_name = f".tmp_{msg_id_prefix}_{secrets.token_hex(4)}{ext}"
                                tmp_path = prepared_group.local_dir / tmp_name
                                try:
                                    _ACTIVE_MEDIA_DOWNLOADS += 1
                                    try:
                                        await asyncio.wait_for(
                                            client.download_media(msg, file=str(tmp_path)),
                                            timeout=timeout_s,
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
                                    await asyncio.wait_for(client.download_media(msg, file=str(target)), timeout=timeout_s)
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
                        except asyncio.TimeoutError as e:
                            last_err = e
                            logger.warning(
                                "下载超时（%ss），将重试: source=%s group_uid=%s msg_id=%s expected=%s (%s/%s)",
                                int(timeout_s),
                                str(source_row.get("id")),
                                prepared_group.group_uid,
                                str(msg_id),
                                str(expected_size or 0),
                                str(attempt),
                                str(attempts),
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
            logger.warning("媒体组下载异常，将断开并重连后重试：%s", err_text)
            try:
                await client.disconnect()
            except Exception:
                pass
            if group_delay > 0:
                await asyncio.sleep(group_delay)
            await ensure_connected(client, "Downloader")

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
        logger.info(
            f"下载完成: source={source_row['id']} date={prepared_group.date_str} id={prepared_group.seq} last={prepared_group.last_message_id}"
        )

        if cfg.rclone_enabled and prepared_group.remote_dir:
            await uploader.enqueue(
                source_row["id"], prepared_group.group_uid, prepared_group.local_dir, prepared_group.remote_dir
            )

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

    folder_id = int(min(int(m.id) for m in messages))
    preferred_folder = f"TG@BieLaoZai_{folder_id}"

    group_uid = build_group_uid(chat_id, topic_root_message_id, grouped_id, messages[0].id)
    existing = db.get_download_group(int(source_row["id"]), str(group_uid))
    if existing:
        # 已成功的组直接跳过；失败的组允许重试，但复用原有 seq/目录，避免编号跳号
        if str(existing.get("status")) != "failed":
            if should_log_each_download_message():
                logger.info(
                    f"跳过已下载的媒体组: source={source_row['id']} group_uid={group_uid} status={existing.get('status')} ids={[m.id for m in messages]}"
                )
            return None

        date_str = str(existing.get("date") or "")
        seq = int(existing.get("seq") or 0)
        local_dir_raw = existing.get("local_dir")
        local_dir = Path(str(local_dir_raw)) if local_dir_raw else None

        text_for_date = header_text or _pick_text_for_date(messages)
        # 失败重试时：如果能从文本中提取到日期且与旧日期不同，则以新日期为准（旧数据可能是 fallback 到消息时间导致）
        if DATE_RE.search(text_for_date or ""):
            extracted_date = extract_date_yyyy_mm_dd(text_for_date, messages[0].date)
            if extracted_date and extracted_date != date_str:
                date_str = extracted_date
                seq = 0
                local_dir = None

        if not date_str:
            date_str = extract_date_yyyy_mm_dd(text_for_date, messages[0].date)

        if seq <= 0 or local_dir is None:
            seq = folder_id

        group_folder = local_dir.name if local_dir else preferred_folder

        source_title = sanitize_path_component(source_row.get("display_title") or f"chat_{abs(chat_id)}")
        if local_dir is None:
            local_dir = cfg.download_root_dir / source_title / date_str / group_folder

        remote_dir = existing.get("remote_dir")
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
    date_str = extract_date_yyyy_mm_dd(text_for_date, messages[0].date)

    seq = folder_id
    group_folder = preferred_folder

    source_title = sanitize_path_component(source_row.get("display_title") or f"chat_{abs(chat_id)}")
    local_dir = cfg.download_root_dir / source_title / date_str / group_folder

    remote_dir = None
    if cfg.rclone_enabled:
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
    header = getattr(message, "reply_to", None)
    if not header:
        return False

    top_id = getattr(header, "reply_to_top_id", None)
    if top_id is None:
        top_id = getattr(header, "top_msg_id", None)

    reply_id = getattr(header, "reply_to_msg_id", None)

    return int(top_id or reply_id or 0) == int(topic_root_message_id)


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
        current.update(fields)
        current["updated_at"] = float(time.time())
        self._clone_progress[source_id] = current

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

        self._update_clone_progress(
            source_id,
            status="queued",
            start_from_message_id=int(start_from_message_id or 0),
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

        if start_from_message_id is not None:
            start_from = int(start_from_message_id)
            if start_from > 0:
                # 无论起始ID大于、小于还是等于已记录的 last_message_id，都重置为指定位置
                # 确保从用户指定的起始ID开始克隆（Telethon min_id 是严格大于，所以用 start-1）
                self._db.set_download_source_last_message(source_id, max(0, start_from - 1))
                source = self._db.get_download_source(source_id) or source

        self._update_clone_progress(
            source_id,
            status="starting",
            started_at=float(time.time()),
            groups_done=0,
            current_message_id=max(0, int(start_from_message_id or 0) - 1)
            if start_from_message_id is not None
            else int(source.get("last_message_id") or 0),
            latest_message_id=0,
            start_from_message_id=int(start_from_message_id or 0),
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

                # 预估最新消息ID（用于进度展示）
                try:
                    entity = await self._client.get_entity(int(source["chat_id"]))
                    reply_to = None
                    if source["source_type"] == "forum_topic" and source.get("topic_root_message_id"):
                        reply_to = int(source["topic_root_message_id"])

                    latest_id = 0
                    async for m in self._client.iter_messages(entity, limit=1, reply_to=reply_to):
                        latest_id = int(getattr(m, "id", 0) or 0)
                        break
                    self._update_clone_progress(source_id, latest_message_id=latest_id)
                except Exception as e:
                    self._update_clone_progress(source_id, latest_message_id=0, latest_error=str(e))

                groups_done = 0

                def on_group_done(group_last_message_id: int) -> None:
                    nonlocal groups_done
                    groups_done += 1
                    self._update_clone_progress(
                        source_id,
                        status="running",
                        groups_done=groups_done,
                        current_message_id=int(group_last_message_id or 0),
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
                    progress_hook=on_group_done,
                )
                # 再跑一次兜底 catch-up，降低“克隆过程中新增消息漏掉”的概率
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
                    progress_hook=on_group_done,
                )

                self._update_clone_progress(source_id, status="done", finished_at=float(time.time()))
                ok = True
                last_err = None
                break
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
    progress_hook: Optional[Callable[[int], None]] = None,
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
        if not pending_messages:
            _reset_pending()
            return

        await _drain_inflight_if_needed()

        messages_snapshot = list(pending_messages)
        header_snapshot = pending_header_text
        last_id = int(max(m.id for m in messages_snapshot))
        _reset_pending()

        if progress_hook:
            try:
                progress_hook(last_id)
            except Exception:
                pass

        # 这里按“消息遍历顺序”预分配日期/编号，避免并发导致 TG@BieLaoZai_002/009 这种乱序编号
        prepared = _prepare_download_group(cfg, db, source, messages_snapshot, header_text=header_snapshot)
        if not prepared:
            return

        task = asyncio.create_task(
            download_media_group(
                client,
                cfg,
                db,
                source,
                messages_snapshot,
                uploader,
                download_semaphore=download_semaphore,
                group_semaphore=group_semaphore,
                header_text=header_snapshot,
                prepared=prepared,
            )
        )
        inflight.add(task)
        await _drain_inflight_if_needed()

    async def _handle_message(msg) -> None:
        nonlocal pending_group_key, pending_messages, pending_header_text, last_header_text

        msg_text = (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()
        has_media = bool(getattr(msg, "media", None))

        # 标题/说明文本：用于后续媒体组抽取日期
        if (not has_media) and msg_text:
            last_header_text = msg_text
            # 有些话题是“先发媒体组，后补一条文字标题/日期”，这里把后补标题绑定给当前 pending 组
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
            if message_count <= 5 or message_count % 10 == 0:
                logger.info(f"遍历消息: id={msg.id} has_media={bool(getattr(msg, 'media', None))}")
            await _handle_message(msg)
        logger.info(f"消息遍历完成: source={source['id']} 共遍历 {message_count} 条消息")
    except Exception as e:
        # forum 话题在某些场景下 reply_to 会触发 GetRepliesRequest，可能报 TOPIC_ID_INVALID。
        # 兜底：改为遍历全群消息，并用 reply_to_top_id/top_msg_id 过滤话题消息（更慢但更稳）。
        if reply_to and source.get("source_type") == "forum_topic" and "TOPIC_ID_INVALID" in str(e):
            used_fallback = True
            logger.warning(
                f"forum_topic reply_to 无效，降级为全群遍历过滤: source={source.get('id')} chat_id={chat_id} topic_root={reply_to} err={e}"
            )
            await _flush_pending()
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram 媒体组下载器（仅媒体）")
    parser.add_argument("--backfill", action="store_true", help="启动时先执行一次历史克隆下载")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        asyncio.run(run_downloader(backfill=bool(args.backfill)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
