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

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError

from database import Database

logger = logging.getLogger(__name__)

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


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
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


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
                if ok and self._cfg.rclone_delete_local_after_upload:
                    shutil.rmtree(local_dir, ignore_errors=False)
                    self._db.mark_download_group_uploaded(source_id, group_uid, uploaded=True, remote_dir=remote_dir, local_deleted=True)
                elif ok:
                    self._db.mark_download_group_uploaded(source_id, group_uid, uploaded=True, remote_dir=remote_dir, local_deleted=False)
                else:
                    self._db.mark_download_group_uploaded(source_id, group_uid, uploaded=False, remote_dir=remote_dir, local_deleted=False)
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

        # 重启/重试时如果目录已存在，Telethon 会自动重命名(1)(2)...；这里直接清空目录实现“覆盖替换”
        ensure_empty_dir(prepared_group.local_dir)

        sem = download_semaphore or asyncio.Semaphore(cfg.max_download_concurrency)

        async def _download_one(msg) -> None:
            if not getattr(msg, "media", None):
                return
            await sem.acquire()
            try:
                await client.download_media(msg, file=str(prepared_group.local_dir))
            finally:
                sem.release()

        await asyncio.gather(*[_download_one(msg) for msg in messages])

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
                current_last = int(source.get("last_message_id") or 0)
                if start_from <= current_last:
                    # 起始ID小于等于已记录 last_message_id：先重置，确保“从更早位置重新克隆”语义一致
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

        # 克隆期间暂停监听，避免并发重复下载；完成后再开启监听
        self._db.set_download_source_active(source_id, False)
        try:
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
        except Exception as e:
            self._update_clone_progress(source_id, status="failed", last_error=str(e), finished_at=float(time.time()))
            logger.error(f"克隆失败: source={source_id} err={e}", exc_info=True)
        finally:
            if auto_listen:
                self._db.set_download_source_active(source_id, True)
            self.refresh_sources_cache(force=True)


async def run_downloader(backfill: bool) -> None:
    cfg = load_config()
    setup_logging(debug=_env_bool("DEBUG", False))

    Path("sessions").mkdir(parents=True, exist_ok=True)
    cfg.download_root_dir.mkdir(parents=True, exist_ok=True)

    db = Database()
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
        await client.run_until_disconnected()
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
    try:
        async for msg in client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True,
            reply_to=reply_to,
        ):
            await _handle_message(msg)
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
        async for msg in client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True,
            reply_to=None,
        ):
            if not _message_in_topic(msg, int(reply_to)):
                continue
            await _handle_message(msg)

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
