#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""管理 Bot（仅管理，不推送不转发）

能力：
- 添加频道：自动读取频道标题，写入 download_sources
- 添加超级群 Forum：拉取话题列表，让管理员选择写入 download_sources
- 频道列表：暂停/恢复监听、点击克隆（历史下载），克隆完成后自动切换监听

依赖：
- 必须配置 `.env`：API_ID / API_HASH / BOT_TOKEN / ADMIN_USER_IDS
- 必须先生成用户会话：`python setup_user_session.py`（用于读取频道/群组/话题信息）
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from dotenv import load_dotenv

try:
    import socks
except ImportError:
    socks = None

from telethon import TelegramClient, events, functions, Button
from telethon.errors.rpcerrorlist import MessageNotModifiedError, ReplyMarkupInvalidError

from database import Database
from bot import DownloaderRuntime, load_config as load_downloader_config, sanitize_path_component, setup_logging


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


def _parse_admin_ids(raw: str) -> List[int]:
    parts = re.split(r"[\s,;]+", (raw or "").strip())
    ids: List[int] = []
    for p in parts:
        if not p:
            continue
        if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
            ids.append(int(p))
    return sorted(set(ids))


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


def _parse_entity_ref(value: str) -> Union[int, str]:
    value = (value or "").strip()
    if not value:
        raise ValueError("缺少频道/群组引用")

    if re.fullmatch(r"-?\d+", value):
        return int(value)

    if value.startswith("@"):
        return value

    if value.startswith("t.me/"):
        return "https://" + value
    if value.startswith(("http://", "https://")):
        return value
    return value


def _to_telegram_chat_id(raw_id: int) -> int:
    if int(raw_id) <= 0:
        return int(raw_id)
    return int(f"-100{int(raw_id)}")


def _candidate_ids_from_ref(ref: int) -> List[int]:
    s = str(int(ref))
    candidates: List[int] = [int(ref)]

    # 兼容用户粘贴的 “-100xxxxxxxxxx”（常见于 Telegram 客户端/其他库的 chat_id 口径）
    if s.startswith("-100") and s[4:].isdigit():
        raw_id = int(s[4:])
        candidates.append(raw_id)
        candidates.append(_to_telegram_chat_id(raw_id))

    if int(ref) > 0:
        candidates.append(_to_telegram_chat_id(int(ref)))

    # 去重并保持顺序
    seen: Set[int] = set()
    uniq: List[int] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        uniq.append(int(c))
    return uniq


def _parse_selection(raw: str, max_index: int) -> List[int]:
    raw = (raw or "").strip().lower()
    if raw in {"all", "a"}:
        return list(range(1, max_index + 1))

    picked: List[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            if left.isdigit() and right.isdigit():
                l, r = int(left), int(right)
                for x in range(min(l, r), max(l, r) + 1):
                    if 1 <= x <= max_index:
                        picked.append(x)
            continue
        if part.isdigit():
            x = int(part)
            if 1 <= x <= max_index:
                picked.append(x)

    return sorted(set(picked))


@dataclass
class PendingTextInput:
    kind: str
    payload: Optional[dict] = None


@dataclass
class PendingTopicSelection:
    chat_id: int
    group_title: str
    topics: List[dict]
    query: str
    hidden_count: int = 0
    page: int = 0
    selected: Set[int] = None

    def __post_init__(self) -> None:
        if self.selected is None:
            self.selected = set()


PendingState = Union[PendingTextInput, PendingTopicSelection]


def _reply_menu_keyboard() -> List[List[Button]]:
    kw = {"resize": True, "persistent": True, "placeholder": "选择操作…"}
    return [
        [Button.text("添加频道", **kw), Button.text("添加群话题", **kw)],
        [Button.text("频道列表", **kw), Button.text("超级群组列表", **kw)],
        [Button.text("状态", **kw), Button.text("帮助", **kw)],
        [Button.text("菜单", **kw)],
    ]


def _truncate_button_text(text: str, limit: int = 50) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "（无标题）"
    return text[: limit - 1] + "…"


def _topic_selection_text(state: PendingTopicSelection, page_size: int) -> str:
    total = len(state.topics)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(state.page, pages - 1))
    selected_count = len(state.selected)
    hidden_line = f"已隐藏已添加：{int(state.hidden_count)}" if int(state.hidden_count or 0) > 0 else ""
    lines = [
        f"话题列表：{state.group_title} (chat_id={state.chat_id})",
        f"筛选关键词：{state.query or '（无）'}",
        f"页码：{page + 1}/{pages}，已选择：{selected_count}",
        hidden_line,
        "",
        "点击按钮勾选/取消；点击“完成”写入工作列表。",
    ]
    return "\n".join([x for x in lines if str(x).strip() != ""])


def _filter_existing_forum_topic_rows(db: Database, chat_id: int, topic_rows: List[dict]) -> tuple[List[dict], int]:
    existing: Set[int] = set()
    try:
        for s in db.list_download_sources(active_only=False):
            if str(s.get("source_type") or "") != "forum_topic":
                continue
            if int(s.get("chat_id") or 0) != int(chat_id):
                continue
            tid = int(s.get("topic_root_message_id") or 0)
            if tid > 0:
                existing.add(tid)
    except Exception:
        existing = set()

    filtered = [t for t in (topic_rows or []) if int(t.get("id") or 0) not in existing]
    return filtered, int(len(topic_rows or []) - len(filtered))


def _topic_selection_buttons(state: PendingTopicSelection, page_size: int) -> List[List[Button]]:
    total = len(state.topics)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(state.page, pages - 1))
    start = page * page_size
    end = min(total, start + page_size)

    rows: List[List[Button]] = []
    for idx in range(start, end):
        topic = state.topics[idx]
        title = str(topic.get("title") or "")
        checked = idx in state.selected
        prefix = "[x] " if checked else "[ ] "
        label = prefix + _truncate_button_text(title)
        rows.append([Button.inline(label, f"topic:t:{idx}".encode("utf-8"))])

    nav: List[Button] = []
    if page > 0:
        nav.append(Button.inline("上一页", f"topic:p:{page - 1}".encode("utf-8")))
    if page < pages - 1:
        nav.append(Button.inline("下一页", f"topic:p:{page + 1}".encode("utf-8")))
    if nav:
        rows.append(nav)

    rows.append(
        [
            Button.inline("完成", b"topic:done"),
            Button.inline("取消", b"topic:cancel"),
        ]
    )
    return rows


def generate_source_list_message(
    db: Database,
    runtime: DownloaderRuntime,
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, List[List[Button]]]:
    sources = db.list_download_sources(active_only=False)
    if not sources:
        return "📭 还没有添加任何频道/话题。\n\n使用“添加频道 / 添加群话题”开始。", []

    page_size = max(3, min(12, int(page_size or 8)))
    total = len(sources)
    pages = max(1, (total + page_size - 1) // page_size)
    page = int(page or 0)
    if page < 0:
        page = 0
    if page > pages - 1:
        page = pages - 1

    start = page * page_size
    end = min(total, start + page_size)
    sources_page = sources[start:end]

    text_lines: List[str] = [f"📋 频道/话题列表（{page + 1}/{pages}）\n"]
    buttons: List[List[Button]] = []

    for source in sources_page:
        source_id = int(source["id"])
        title = str(source.get("display_title") or f"source_{source_id}")
        status_icon = "✅" if source.get("is_active") else "⏸️"

        # 简洁显示模式，不显示详细克隆进度
        if runtime.is_cloning(source_id):
            mode_text = "📡 模式: 克隆中"
        elif source.get("is_active"):
            mode_text = "📡 模式: 实时监听"
        else:
            mode_text = "📡 模式: 已暂停"

        counts = db.count_download_groups_by_status(source_id)
        failed = int(counts.get("failed", 0))
        total = int(counts.get("total", 0))
        last_msg_id = int(source.get('last_message_id') or 0)

        text_lines.append(
            f"{status_icon} {title}\n"
            f"   {mode_text}\n"
            f"   📊 最后同步: 消息 {last_msg_id}\n"
        )

        toggle_label = "⏸️ 暂停监听" if source.get("is_active") else "▶️ 开始监听"
        buttons.append(
            [Button.inline(f"{status_icon} {_truncate_button_text(title, 40)}", f"src:noop:{source_id}".encode("utf-8"))]
        )
        action_row: List[Button] = [Button.inline(toggle_label, f"src:toggle:{source_id}".encode("utf-8"))]
        if runtime.is_cloning(source_id):
            action_row.append(Button.inline("⏹ 停止克隆", f"src:clonestop:{source_id}".encode("utf-8")))
        else:
            action_row.append(Button.inline("📦 克隆", f"src:clone:{source_id}".encode("utf-8")))

        action_row.append(Button.inline("✏️ 重命名", f"src:rename:{source_id}".encode("utf-8")))
        action_row.append(Button.inline("⬆ 一键上传", f"src:uploadsrc:{source_id}".encode("utf-8")))
        action_row.append(Button.inline("🗑️ 删除", f"src:del:{source_id}".encode("utf-8")))
        buttons.append(action_row[:4])
        if len(action_row) > 4:
            buttons.append(action_row[4:])

    nav: List[Button] = []
    if page > 0:
        nav.append(Button.inline("⬅️ 上一页", f"src:page:{page - 1}".encode("utf-8")))
    if page < pages - 1:
        nav.append(Button.inline("下一页 ➡️", f"src:page:{page + 1}".encode("utf-8")))
    if nav:
        buttons.append(nav)

    buttons.append([Button.inline("🔄 刷新列表", f"src:refresh:{page}".encode("utf-8"))])
    return "\n".join(text_lines).strip(), buttons


def generate_forum_group_list_message(db: Database) -> tuple[str, List[List[Button]]]:
    groups = db.list_forum_groups()
    if not groups:
        return (
            "👥 超级群组列表为空。\n\n"
            "请先用“添加群话题”添加一次群组（可不选话题也没关系），之后就能在这里一键重新获取话题。",
            [[Button.inline("➕ 添加群话题", b"grp:add")]],
        )

    text_lines: List[str] = ["👥 超级群组列表\n"]
    buttons: List[List[Button]] = []

    for g in groups:
        group_id = int(g["id"])
        title = str(g.get("title") or f"group_{group_id}")
        chat_id = int(g.get("chat_id") or 0)
        text_lines.append(f"• {title}\n   chat_id: {chat_id}\n")
        buttons.append(
            [Button.inline(f"👥 {_truncate_button_text(title, 40)}", f"grp:noop:{group_id}".encode("utf-8"))]
        )
        buttons.append(
            [
                Button.inline("🧵 获取话题", f"grp:topics:{group_id}".encode("utf-8")),
                Button.inline("🗑️ 删除记录", f"grp:del:{group_id}".encode("utf-8")),
            ]
        )

    buttons.append(
        [
            Button.inline("➕ 添加群话题", b"grp:add"),
            Button.inline("🔄 刷新列表", b"grp:refresh"),
        ]
    )
    return "\n".join(text_lines).strip(), buttons


async def _fetch_forum_topics(client: TelegramClient, peer, query: str) -> List:
    result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=peer,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q=query or "",
        )
    )
    topics = getattr(result, "topics", None)
    if topics is None:
        topics = getattr(result, "forum_topics", None)
    return list(topics or [])


async def _resolve_entity(client: TelegramClient, ref: Union[int, str]):
    last_error: Optional[Exception] = None

    if isinstance(ref, int):
        candidates: List[Union[int, str]] = [int(x) for x in _candidate_ids_from_ref(ref)]
    else:
        candidates = [ref]

    for cand in candidates:
        try:
            return await client.get_entity(cand)
        except Exception as e:
            last_error = e

    # 私密群/频道：可能需要从 dialogs 中找到（前提：账号确实在群里/见过该会话）
    if isinstance(ref, int):
        cand_ids = set(int(x) for x in _candidate_ids_from_ref(ref))
        async for dialog in client.iter_dialogs(limit=10000):
            try:
                dialog_id = int(getattr(dialog, "id", 0) or 0)
                if dialog_id and dialog_id in cand_ids:
                    return dialog.entity

                raw_id = int(getattr(dialog.entity, "id", 0) or 0)
                if raw_id and (raw_id in cand_ids or _to_telegram_chat_id(raw_id) in cand_ids):
                    return dialog.entity
            except Exception:
                continue

    hint = ""
    if isinstance(ref, int) and str(int(ref)).startswith("-100"):
        hint = (
            "\n\n提示：这是私密超级群/频道常见的 chat_id 口径（-100...）。\n"
            "1) 请确认 `setup_user_session.py` 登录的“用户账号”已经加入该群/频道。\n"
            "2) 用该账号在 Telegram 客户端里打开一次该群/频道（让它出现在会话列表），再重试。\n"
            "3) 如果你并未加入该私密群/频道，仅凭 ID 无法解析实体（需要先加入或提供可加入的邀请链接）。"
        )

    raise RuntimeError(f"Cannot find any entity corresponding to {ref!r}{hint}") from last_error


def _parse_message_ref(raw: str) -> Tuple[Optional[Union[int, str]], Optional[int], int]:
    """
    返回：(chat_hint, thread_id, message_id)
    - 纯数字：chat_hint=None
    - t.me/c/<internal>/<msg>：chat_hint=-100<internal>
    - t.me/c/<internal>/<thread>/<msg>：chat_hint=-100<internal>，thread_id=<thread>
    - t.me/<username>/<msg>：chat_hint='@username'
    - t.me/<username>/<thread>/<msg>：chat_hint='@username'，thread_id=<thread>
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("缺少消息引用")

    if re.fullmatch(r"\d+", raw):
        return None, None, int(raw)

    if raw.startswith("@") and raw[1:].isdigit():
        return None, None, int(raw[1:])

    if raw.startswith("t.me/"):
        raw = "https://" + raw

    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"t.me", "telegram.me", "telegram.dog"}:
            raise ValueError("不支持的链接域名")

        path = (parsed.path or "").strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError("链接缺少路径")

        if parts[0] == "c":
            if len(parts) < 3:
                raise ValueError("c 链接缺少频道或消息 ID")
            internal = "".join(ch for ch in parts[1] if ch.isdigit())
            if len(parts) >= 4:
                thread_id = "".join(ch for ch in parts[2] if ch.isdigit())
                msg_id = "".join(ch for ch in parts[3] if ch.isdigit())
            else:
                thread_id = ""
                msg_id = "".join(ch for ch in parts[2] if ch.isdigit())
            if not internal or not msg_id:
                raise ValueError("链接格式无效")
            return int(f"-100{internal}"), (int(thread_id) if thread_id else None), int(msg_id)

        username = parts[0]
        if len(parts) < 2:
            raise ValueError("链接缺少消息 ID")
        if len(parts) >= 3:
            thread_id = "".join(ch for ch in parts[1] if ch.isdigit())
            msg_id = "".join(ch for ch in parts[2] if ch.isdigit())
        else:
            thread_id = ""
            msg_id = "".join(ch for ch in parts[1] if ch.isdigit())
        if not msg_id:
            raise ValueError("链接缺少消息 ID")
        return f"@{username}", (int(thread_id) if thread_id else None), int(msg_id)

    raise ValueError("不支持的消息引用格式")

def _help_text() -> str:
    return (
        "管理方式：\n"
        "- 推荐：使用输入框下方的菜单键盘（/start 或 /menu）\n"
        "- 频道列表里可：暂停/恢复监听、点击“克隆”（历史下载），克隆完成自动切换监听\n"
        "- 超级群组列表：保存群组快捷记录，一键重新拉取话题并选择加入工作列表\n"
        "- 兼容：仍支持文本命令（不推荐）\n"
        "\n"
        "文本命令：\n"
        "/addchannel <频道用户名/链接/ID>\n"
        "/addgroup <群组用户名/链接/ID> [可选关键词过滤]\n"
        "/sources  # 显示频道列表（带按钮）\n"
        "/status\n"
    )


def _normalize_rclone_remote(remote: str) -> str:
    remote = (remote or "").strip()
    if not remote:
        return ""
    return remote if remote.endswith(":") else f"{remote}:"


async def main_async() -> None:
    load_dotenv()
    setup_logging(_env_bool("DEBUG", False))

    api_id = _env_int("API_ID")
    api_hash = _env_str("API_HASH")
    bot_token = _env_str("BOT_TOKEN")

    admin_raw = _env_str("ADMIN_USER_IDS", "") or ""
    admin_ids = _parse_admin_ids(admin_raw)

    if not api_id or not api_hash:
        raise RuntimeError("请在 .env 中配置 API_ID 与 API_HASH")
    if not bot_token:
        raise RuntimeError("请在 .env 中配置 BOT_TOKEN")
    if not admin_ids:
        raise RuntimeError("请在 .env 中配置 ADMIN_USER_IDS（管理员用户ID列表）")

    proxy = build_proxy()

    Path("sessions").mkdir(parents=True, exist_ok=True)

    db = Database()

    user_client = TelegramClient("sessions/user_session", int(api_id), str(api_hash), proxy=proxy)
    bot_client = TelegramClient("sessions/manager_bot", int(api_id), str(api_hash), proxy=proxy)

    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.disconnect()
        raise RuntimeError("未检测到用户会话，请先运行: python setup_user_session.py")

    await bot_client.start(bot_token=bot_token)
    me = await bot_client.get_me()
    if not getattr(me, "bot", False):
        await bot_client.disconnect()
        await user_client.disconnect()
        raise RuntimeError(
            "检测到管理 Bot 的 session 被“用户账号”占用，导致 bot_token 未生效。\n"
            "解决：删除 sessions/manager_bot.session（或更换一个全新的 session 名称）后重试。"
        )

    # 下载器运行时（监听 + 下载）
    downloader_cfg = load_downloader_config()
    downloader_cfg.download_root_dir.mkdir(parents=True, exist_ok=True)
    runtime = DownloaderRuntime(user_client, downloader_cfg, db)
    runtime.start()

    upload_tasks: Dict[int, asyncio.Task] = {}

    pending: Dict[int, PendingState] = {}
    topic_page_size = int(_env_int("BOT_TOPIC_PAGE_SIZE", 10) or 10)
    topic_page_size = max(5, min(20, topic_page_size))
    source_page_size = int(_env_int("BOT_SOURCE_PAGE_SIZE", 8) or 8)
    source_page_size = max(3, min(12, source_page_size))
    source_list_page_by_user: Dict[int, int] = {}

    def is_admin(user_id: Optional[int]) -> bool:
        return user_id is not None and int(user_id) in admin_ids

    def _format_clone_progress_text(title: str, source_id: int) -> str:
        prog = None
        try:
            prog = runtime.get_clone_progress(source_id)
        except Exception:
            prog = None

        if not prog:
            return f"📦 正在克隆频道: {title}\n状态：初始化中…"

        status = str(prog.get("status") or "running")
        groups_done = int(prog.get("groups_done") or 0)
        current_msg_id = int(prog.get("current_message_id") or 0)

        # 简洁格式：统计数量而不是显示ID
        # 根据状态显示不同的标题
        if status == "done":
            lines = [f"✅ 克隆完成: {title}"]
        elif status == "cancelled":
            lines = [f"⏹️ 克隆已取消: {title}"]
        elif status == "failed":
            lines = [f"❌ 克隆失败: {title}"]
        elif status in ("starting", "queued"):
            lines = [f"📦 准备克隆频道: {title}"]
        elif status == "retrying":
            lines = [f"🔄 重试中: {title}"]
        else:
            lines = [f"📦 正在克隆频道: {title}"]

        # 当前进度：当前并发窗口中“最小未上传完成”的消息ID
        if current_msg_id > 0:
            lines.append(f"📍 当前进度: 消息 {current_msg_id}")

        # 进度：不再展示“遍历消息数”，仅展示媒体组完成数量（以 rclone 上传完成为准）
        if status in ("done", "cancelled", "failed"):
            if groups_done > 0:
                lines.append(f"📊 进度: 已完成（共 {groups_done} 个媒体组）")
            else:
                lines.append(f"📊 进度: 已完成")
        elif groups_done > 0:
            lines.append(f"📊 进度: 进行中（已完成 {groups_done} 个媒体组）")
        else:
            lines.append(f"📊 进度: 初始化中…")

        # 已完成媒体组数（总是显示，提供更直观的进度信息）
        if groups_done > 0:
            lines.append(f"✅ 已完成媒体组: {groups_done}")

        # 错误信息（如果有）
        last_error = str(prog.get("last_error") or prog.get("latest_error") or "").strip()
        if last_error:
            lines.append(f"\n⚠️ 最近错误: {last_error}")

        return "\n".join(lines)

    def _build_status_text() -> str:
        sources_all = db.list_download_sources(active_only=False)
        sources_active = db.list_download_sources(active_only=True)
        forum_groups = []
        try:
            forum_groups = db.list_forum_groups()
        except Exception:
            forum_groups = []

        cloning_lines: List[str] = []
        total_downloaded = 0
        total_failed = 0
        for s in sources_all:
            sid = int(s["id"])
            counts = db.count_download_groups_by_status(sid)
            total_downloaded += int(counts.get("total", 0))
            total_failed += int(counts.get("failed", 0))
            if runtime.is_cloning(sid):
                prog = None
                try:
                    prog = runtime.get_clone_progress(sid)
                except Exception:
                    prog = None
                title = str(s.get("display_title") or f"source_{sid}")
                if prog:
                    cur = int(prog.get("current_message_id") or 0)
                    latest = int(prog.get("latest_message_id") or 0)
                    groups_done = int(prog.get("groups_done") or 0)
                    if latest > 0 and cur > 0:
                        cloning_lines.append(f"- {title}: {cur}/{latest}（组 {groups_done}）")
                    elif cur > 0:
                        cloning_lines.append(f"- {title}: 到 {cur}（组 {groups_done}）")
                    else:
                        cloning_lines.append(f"- {title}: 克隆中…")
                else:
                    cloning_lines.append(f"- {title}: 克隆中…")

        upload_q = None
        try:
            upload_q = int(getattr(getattr(runtime, "_uploader", None), "_queue", None).qsize())  # type: ignore[attr-defined]
        except Exception:
            upload_q = None

        lines: List[str] = [
            "📈 状态",
            f"- 来源：{len(sources_all)}（启用 {len(sources_active)}）",
            f"- 超级群组记录：{len(forum_groups)}",
            f"- 已下载媒体组：{total_downloaded}（失败 {total_failed}）",
        ]
        if upload_q is not None:
            lines.append(f"- 待上传队列：{upload_q}")
        if cloning_lines:
            lines.append("")
            lines.append("📦 正在克隆：")
            lines.extend(cloning_lines[:10])
            if len(cloning_lines) > 10:
                lines.append(f"... 还有 {len(cloning_lines) - 10} 个")
        else:
            lines.append("- 当前无克隆任务")

        return "\n".join(lines)

    async def _run_clone_with_progress(
        chat_id: int,
        source_id: int,
        title: str,
        task: asyncio.Task,
        list_message_id: Optional[int] = None,
    ) -> None:
        """简单的克隆通知（只通知开始和完成）"""
        try:
            # 发送开始消息
            await bot_client.send_message(
                int(chat_id),
                f"📦 开始克隆频道：{title}",
                buttons=_reply_menu_keyboard(),
            )

            # 等待克隆完成
            try:
                await task
                ok = True
                err = None
            except Exception as e:
                ok = False
                err = e

            # 更新频道列表
            if list_message_id:
                try:
                    list_text, list_buttons = generate_source_list_message(db, runtime)
                    await bot_client.edit_message(int(chat_id), int(list_message_id), list_text, buttons=list_buttons)
                except Exception:
                    pass

            # 发送完成通知
            final_status = f"✅ 克隆完成并已开始监听：{title}" if ok else f"❌ 克隆失败：{title}\n原因：{err}"
            try:
                await bot_client.send_message(int(chat_id), final_status, buttons=_reply_menu_keyboard())
            except Exception:
                pass

        except Exception as e:
            print(f"\n[ERROR] 克隆处理失败: {e}\n", file=sys.stderr, flush=True)

    async def deny(event, text: str = "无权限") -> None:
        try:
            await event.reply(text)
        except Exception:
            pass

    async def show_home(event, text: Optional[str] = None) -> None:
        msg = text or "请选择操作："
        await event.reply(msg, buttons=_reply_menu_keyboard())

    async def send_source_list(event) -> None:
        source_list_page_by_user[int(event.sender_id or 0)] = 0
        text, buttons = generate_source_list_message(db, runtime, page=0, page_size=source_page_size)
        try:
            await event.reply(text, buttons=buttons)
        except ReplyMarkupInvalidError:
            await event.reply(text)

    async def send_group_list(event) -> None:
        text, buttons = generate_forum_group_list_message(db)
        await event.reply(text, buttons=buttons)

    @bot_client.on(events.NewMessage(pattern=r"^/start\b"))
    async def on_start(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        pending.pop(int(event.sender_id), None)
        await show_home(event)

    @bot_client.on(events.NewMessage(pattern=r"^/help\b"))
    async def on_help(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        await event.reply(_help_text(), buttons=_reply_menu_keyboard())

    @bot_client.on(events.NewMessage(pattern=r"^/sources\b"))
    async def on_sources(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        await send_source_list(event)

    @bot_client.on(events.NewMessage(pattern=r"^/status\b"))
    async def on_status(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        await event.reply(_build_status_text(), buttons=_reply_menu_keyboard())

    @bot_client.on(events.NewMessage(pattern=r"^/clone\s+(\d+)(?:\s+(\d+))?"))
    async def on_clone(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        source_id = int(event.pattern_match.group(1))
        start_from_id = None
        if event.pattern_match.group(2):
            start_from_id = int(event.pattern_match.group(2))

        source = db.get_download_source(source_id)
        if not source:
            await event.reply("未找到来源", buttons=_reply_menu_keyboard())
            return
        if runtime.is_cloning(source_id):
            await event.reply("该来源正在克隆中", buttons=_reply_menu_keyboard())
            return
        title = str(source.get("display_title") or f"source_{source_id}")
        db.set_download_source_active(source_id, False)
        runtime.refresh_sources_cache(force=True)
        task = runtime.start_clone(source_id, auto_listen=True, start_from_message_id=start_from_id)
        asyncio.create_task(_run_clone_with_progress(int(event.chat_id), source_id, title, task))

    @bot_client.on(events.NewMessage(pattern=r"^/addchannel\s+(.+)$"))
    async def on_add_channel(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return

        ref = event.pattern_match.group(1).strip()
        try:
            entity = await _resolve_entity(user_client, _parse_entity_ref(ref))
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(ref)
            raw_id = int(getattr(entity, "id", None) or 0)
            if not raw_id:
                raise RuntimeError("无法解析 chat_id")

            chat_id = _to_telegram_chat_id(raw_id)
            source_id = db.add_download_source(
                source_type="channel",
                chat_id=chat_id,
                display_title=str(title),
            )
            await event.reply(
                f"已添加频道：{title}\n\n去“频道列表”里点击“克隆”即可开始历史下载，完成后会自动开始监听。",
                buttons=_reply_menu_keyboard(),
            )
        except Exception as e:
            await event.reply(f"添加失败: {e}", buttons=_reply_menu_keyboard())

    @bot_client.on(events.NewMessage(pattern=r"^/addgroup\s+(.+)$"))
    async def on_add_group(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return

        raw = event.pattern_match.group(1).strip()
        if not raw:
            await event.reply("用法: /addgroup <群组> [可选关键词]")
            return

        parts = raw.split(maxsplit=1)
        ref = parts[0]
        query = parts[1].strip() if len(parts) > 1 else ""

        try:
            entity = await _resolve_entity(user_client, _parse_entity_ref(ref))
            group_title = getattr(entity, "title", None) or str(ref)
            raw_id = int(getattr(entity, "id", None) or 0)
            if not raw_id:
                raise RuntimeError("无法解析 chat_id")

            chat_id = _to_telegram_chat_id(raw_id)
            try:
                db.upsert_forum_group(chat_id, str(group_title))
            except Exception:
                pass

            topics = await _fetch_forum_topics(user_client, entity, query=query)
            if not topics:
                await event.reply("未获取到任何话题，请确认该群已启用 Forum Topics")
                return

            topic_rows: List[dict] = []
            for t in topics:
                topic_rows.append(
                    {
                        # 注意：t.id 才是 thread_id（t.me/c/.../<thread_id>/<msg_id>）
                        "id": int(getattr(t, "id", 0) or 0),
                        "title": str(getattr(t, "title", "") or ""),
                        # top_message 在很多群里是“该话题最新消息ID”，不是 thread_id
                        "top_message": int(getattr(t, "top_message", 0) or 0),
                    }
                )

            topic_rows, hidden_count = _filter_existing_forum_topic_rows(db, chat_id, topic_rows)
            if not topic_rows:
                await event.reply("该群话题已全部添加，无需重复操作。")
                return

            sel = PendingTopicSelection(
                chat_id=chat_id,
                group_title=str(group_title),
                topics=topic_rows,
                query=query,
                hidden_count=hidden_count,
            )
            pending[int(event.sender_id)] = sel
            await event.reply(
                _topic_selection_text(sel, topic_page_size),
                buttons=_topic_selection_buttons(sel, topic_page_size),
            )

        except Exception as e:
            await event.reply(f"获取话题失败: {e}", buttons=_reply_menu_keyboard())

    @bot_client.on(events.NewMessage(pattern=r"^/menu\b"))
    async def on_menu(event):
        if not is_admin(event.sender_id):
            await deny(event)
            return
        pending.pop(int(event.sender_id), None)
        await show_home(event)

    @bot_client.on(events.CallbackQuery)
    async def on_callback(event):
        if not is_admin(event.sender_id):
            await event.answer("无权限", alert=True)
            return

        data = event.data or b""
        user_id = int(event.sender_id)

        if data.startswith(b"src:"):
            parts = data.decode("utf-8", errors="ignore").split(":")
            action = parts[1] if len(parts) > 1 else ""

            def _current_page() -> int:
                return int(source_list_page_by_user.get(user_id, 0) or 0)

            async def _refresh(page: Optional[int] = None) -> None:
                if page is None:
                    page = _current_page()
                source_list_page_by_user[user_id] = int(page or 0)
                text, buttons = generate_source_list_message(db, runtime, page=int(page or 0), page_size=source_page_size)
                try:
                    await event.edit(text, buttons=buttons)
                except MessageNotModifiedError:
                    return
                except ReplyMarkupInvalidError:
                    await event.edit(text)

            if action == "refresh":
                page = _current_page()
                if len(parts) >= 3 and parts[2].isdigit():
                    page = int(parts[2])
                await _refresh(page=page)
                return

            if action == "page":
                if len(parts) < 3 or not parts[2].isdigit():
                    await event.answer("无效页码", alert=True)
                    return
                await _refresh(page=int(parts[2]))
                return

            if action == "noop":
                await event.answer("请选择下方操作按钮")
                return

            if len(parts) < 3 or not parts[2].isdigit():
                await event.answer("无效操作", alert=True)
                return

            source_id = int(parts[2])
            source = db.get_download_source(source_id)
            if not source:
                await event.answer("来源不存在", alert=True)
                await _refresh()
                return

            if action == "toggle":
                ok = db.set_download_source_active(source_id, not bool(source.get("is_active")))
                runtime.refresh_sources_cache(force=True)
                await event.answer("OK" if ok else "失败")
                await _refresh()
                return

            if action == "clone":
                if runtime.is_cloning(source_id):
                    await event.answer("已在克隆中")
                    return

                title = str(source.get("display_title") or f"source_{source_id}")
                await event.answer("请选择克隆起始方式")
                await bot_client.send_message(
                    int(event.chat_id),
                    f"📦 克隆：{title}\n请选择起始方式：\n- 继续克隆：从该来源上次记录的 last_message_id 开始\n- 指定ID/链接：从指定消息开始（包含该消息）",
                    buttons=[
                        [
                            Button.inline("▶ 继续克隆", f"src:clonerun:{source_id}".encode("utf-8")),
                            Button.inline("🔢 指定ID/链接", f"src:cloneask:{source_id}".encode("utf-8")),
                        ],
                        [Button.inline("🧹 从头克隆", f"src:clonefrom0:{source_id}".encode("utf-8"))],
                        [Button.inline("🔁 补克隆(URL)", f"src:patch:{source_id}".encode("utf-8"))],
                        [Button.inline("关闭", b"src:close")],
                    ],
                )
                await _refresh()
                return

            if action == "patch":
                pending[user_id] = PendingTextInput(kind="patchclone", payload={"source_id": source_id})
                await event.answer("请发送要补的消息链接")
                await bot_client.send_message(
                    int(event.chat_id),
                    f"🔁 补克隆：{source.get('display_title')}\n"
                    "请直接发送“消息链接/消息ID”，可一次发送多条（每行一条）。\n"
                    "示例：\n"
                    "https://t.me/c/3406401277/6779/7304\n"
                    "https://t.me/c/3406401277/6779/7310\n"
                    "或：7304\n",
                    buttons=_reply_menu_keyboard(),
                )
                return

            if action == "rename":
                pending[user_id] = PendingTextInput(kind="renamesource", payload={"source_id": source_id})
                old = str(source.get("display_title") or f"source_{source_id}")
                await event.answer("请发送新名称")
                await bot_client.send_message(
                    int(event.chat_id),
                    f"✏️ 重命名来源：{old}\n"
                    "请发送新的显示名称（将用于本地/云盘文件夹名）。\n"
                    "提示：只影响后续新下载；已存在的历史文件夹不会自动改名。",
                    buttons=_reply_menu_keyboard(),
                )
                return

            if action == "clonerun":
                if runtime.is_cloning(source_id):
                    await event.answer("已在克隆中")
                    return
                title = str(source.get("display_title") or f"source_{source_id}")
                await event.answer("开始克隆…")
                db.set_download_source_active(source_id, False)
                runtime.refresh_sources_cache(force=True)
                task = runtime.start_clone(source_id, auto_listen=True)
                asyncio.create_task(
                    _run_clone_with_progress(
                        int(event.chat_id),
                        source_id,
                        title,
                        task,
                        list_message_id=int(getattr(event, "message_id", 0) or 0) or None,
                    )
                )
                await _refresh()
                return

            if action == "clonestop":
                if not runtime.is_cloning(source_id):
                    await event.answer("当前未在克隆")
                    return
                ok = runtime.stop_clone(source_id)
                db.set_download_source_active(source_id, True)
                runtime.refresh_sources_cache(force=True)
                await event.answer("已停止" if ok else "停止失败")
                await _refresh()
                return

            if action == "uploadsrc":
                if source_id in upload_tasks and not upload_tasks[source_id].done():
                    await event.answer("该来源正在上传中")
                    return

                rclone_remote = _normalize_rclone_remote(os.environ.get("RCLONE_REMOTE") or "")
                if not rclone_remote:
                    await event.answer("请先在 .env 配置 RCLONE_REMOTE", alert=True)
                    return

                source_title = sanitize_path_component(str(source.get("display_title") or f"source_{source_id}"))
                local_dir = downloader_cfg.download_root_dir / source_title
                if not local_dir.exists():
                    await event.answer("本地目录不存在", alert=True)
                    return

                remote_base = (os.environ.get("RCLONE_REMOTE_BASE_DIR") or "").strip("/\\")
                remote_rel = "/".join(part for part in [remote_base, source_title] if part)
                remote_dir = f"{rclone_remote}{remote_rel}"
                if not re.match(r"^[A-Za-z0-9_\\-]+:", str(remote_dir or "")):
                    await event.answer("远端路径无效（缺少 remote 前缀 `<name>:`）", alert=True)
                    return

                rclone_exe = "rclone"
                if Path("./rclone.exe").exists():
                    rclone_exe = str(Path("./rclone.exe"))
                elif Path("./rclone").exists():
                    rclone_exe = str(Path("./rclone"))

                extra_args_raw = os.environ.get("RCLONE_EXTRA_ARGS") or ""
                extra_args = [a for a in extra_args_raw.split() if a.strip()]

                title = str(source.get("display_title") or f"source_{source_id}")
                await event.answer("开始上传…")
                msg = await bot_client.send_message(
                    int(event.chat_id),
                    f"⬆ 正在上传来源目录：{title}\n本地：{local_dir}\n远端：{remote_dir}",
                    buttons=_reply_menu_keyboard(),
                )

                async def _run_upload() -> None:
                    try:
                        args = [rclone_exe, "copy", str(local_dir), str(remote_dir), "--create-empty-src-dirs"]
                        args.extend(extra_args)
                        try:
                            await bot_client.edit_message(
                                int(event.chat_id),
                                int(msg.id),
                                f"⬆ 正在上传来源目录：{title}\n本地：{local_dir}\n远端：{remote_dir}\n命令：{' '.join(args)}",
                                buttons=_reply_menu_keyboard(),
                            )
                        except Exception:
                            pass
                        proc = await asyncio.create_subprocess_exec(
                            *args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        assert proc.stdout is not None
                        async for line in proc.stdout:
                            text = line.decode(errors="ignore").rstrip()
                            logger.info(text)
                        code = await proc.wait()
                        if code == 0:
                            await bot_client.edit_message(
                                int(event.chat_id),
                                int(msg.id),
                                f"✅ 上传完成：{title}\n远端：{remote_dir}",
                                buttons=_reply_menu_keyboard(),
                            )
                        else:
                            await bot_client.edit_message(
                                int(event.chat_id),
                                int(msg.id),
                                f"❌ 上传失败(code={code})：{title}\n远端：{remote_dir}",
                                buttons=_reply_menu_keyboard(),
                            )
                    except Exception as e:
                        try:
                            await bot_client.edit_message(
                                int(event.chat_id),
                                int(msg.id),
                                f"❌ 上传异常：{title}\n原因：{e}",
                                buttons=_reply_menu_keyboard(),
                            )
                        except Exception:
                            pass

                upload_tasks[source_id] = asyncio.create_task(_run_upload())
                await _refresh()
                return

            if action == "clonefrom0":
                if runtime.is_cloning(source_id):
                    await event.answer("已在克隆中")
                    return
                title = str(source.get("display_title") or f"source_{source_id}")
                await event.answer("从头克隆…")
                db.set_download_source_active(source_id, False)
                runtime.refresh_sources_cache(force=True)
                task = runtime.start_clone(source_id, auto_listen=True, start_from_message_id=1)
                asyncio.create_task(
                    _run_clone_with_progress(
                        int(event.chat_id),
                        source_id,
                        title,
                        task,
                        list_message_id=int(getattr(event, "message_id", 0) or 0) or None,
                    )
                )
                await _refresh()
                return

            if action == "cloneask":
                pending[user_id] = PendingTextInput(kind="clone_start", payload={"source_id": source_id})
                await event.answer("请发送起始消息ID/链接")
                await bot_client.send_message(
                    int(event.chat_id),
                    f"🔢 指定起始：{source.get('display_title')}\n"
                    "请发送起始消息 ID（数字）或消息链接（t.me/...）。\n"
                    "例如：7304\n"
                    "例如：https://t.me/c/3406401277/6779/7304\n",
                    buttons=_reply_menu_keyboard(),
                )
                return

            if action == "close":
                try:
                    await event.delete()
                except Exception:
                    pass
                return

            if action == "del":
                title = str(source.get("display_title") or f"source_{source_id}")
                await event.edit(
                    f"确认删除来源？\n\n{title}\n\n（删除后将无法继续监听/克隆该来源，并会一并删除该来源的下载记录/序号计数）",
                    buttons=[
                        [
                            Button.inline("确认删除", f"src:delok:{source_id}".encode("utf-8")),
                            Button.inline("取消", b"src:refresh"),
                        ]
                    ],
                )
                return

            if action == "delok":
                ok = db.delete_download_source(source_id)
                runtime.refresh_sources_cache(force=True)
                await event.answer("已删除" if ok else "删除失败")
                await _refresh()
                return

        if data.startswith(b"grp:"):
            parts = data.decode("utf-8", errors="ignore").split(":")
            action = parts[1] if len(parts) > 1 else ""

            async def _grefresh() -> None:
                text, buttons = generate_forum_group_list_message(db)
                try:
                    await event.edit(text, buttons=buttons)
                except MessageNotModifiedError:
                    return

            if action == "refresh":
                await _grefresh()
                return

            if action == "add":
                pending[user_id] = PendingTextInput(kind="addgroup")
                await event.answer("请发送超级群引用（可带关键词）")
                await bot_client.send_message(
                    int(event.chat_id),
                    "请发送超级群引用（用户名/链接/ID），可追加关键词过滤。\n"
                    "示例：-1001234567890 付费\n"
                    "示例：@mygroup",
                    buttons=_reply_menu_keyboard(),
                )
                return

            if action == "noop":
                await event.answer("请选择下方操作按钮")
                return

            if len(parts) < 3 or not parts[2].isdigit():
                await event.answer("无效操作", alert=True)
                return

            group_id = int(parts[2])
            group = db.get_forum_group(group_id)
            if not group:
                await event.answer("群组记录不存在", alert=True)
                await _grefresh()
                return

            if action == "topics":
                try:
                    entity = await _resolve_entity(user_client, int(group["chat_id"]))
                    group_title = str(group.get("title") or getattr(entity, "title", None) or f"group_{group_id}")
                    topics = await _fetch_forum_topics(user_client, entity, query="")
                    if not topics:
                        await event.answer("未获取到任何话题，请确认该群已启用 Forum Topics", alert=True)
                        return

                    topic_rows: List[dict] = []
                    for t in topics:
                        topic_rows.append(
                            {
                                "id": int(getattr(t, "id", 0) or 0),
                                "title": str(getattr(t, "title", "") or ""),
                                "top_message": int(getattr(t, "top_message", 0) or 0),
                            }
                        )

                    topic_rows, hidden_count = _filter_existing_forum_topic_rows(db, int(group["chat_id"]), topic_rows)
                    if not topic_rows:
                        await event.answer("该群话题已全部添加，无需重复操作。", alert=True)
                        return

                    sel = PendingTopicSelection(
                        chat_id=int(group["chat_id"]),
                        group_title=group_title,
                        topics=topic_rows,
                        query="",
                        hidden_count=hidden_count,
                    )
                    pending[user_id] = sel
                    await event.edit(
                        _topic_selection_text(sel, topic_page_size),
                        buttons=_topic_selection_buttons(sel, topic_page_size),
                    )
                    return
                except Exception as e:
                    await event.answer(f"获取话题失败: {e}", alert=True)
                    return

            if action == "del":
                title = str(group.get("title") or f"group_{group_id}")
                await event.edit(
                    f"确认删除群组记录？\n\n{title}\n\n（只删除群组快捷记录，不会删除已添加的话题来源）",
                    buttons=[
                        [
                            Button.inline("确认删除", f"grp:delok:{group_id}".encode("utf-8")),
                            Button.inline("取消", b"grp:refresh"),
                        ]
                    ],
                )
                return

            if action == "delok":
                ok = db.delete_forum_group(group_id)
                await event.answer("已删除" if ok else "删除失败")
                await _grefresh()
                return

        if data.startswith(b"topic:"):
            state = pending.get(user_id)
            if not isinstance(state, PendingTopicSelection):
                await event.answer("当前没有进行话题选择", alert=True)
                return

            if data == b"topic:done":
                if not state.selected:
                    await event.answer("未选择任何话题", alert=True)
                    return

                ok_count = 0
                for idx in sorted(state.selected):
                    topic = state.topics[idx]
                    display_title = (
                        f"{state.group_title}__{topic.get('title')}"
                        if topic.get("title")
                        else state.group_title
                    )
                    source_id = db.add_download_source(
                        source_type="forum_topic",
                        chat_id=state.chat_id,
                        display_title=str(display_title),
                        topic_id=int(topic.get("id") or 0) or None,
                        topic_root_message_id=int(topic.get("id") or 0) or None,
                    )
                    if source_id:
                        ok_count += 1

                pending.pop(user_id, None)
                await event.edit(
                    f"已添加 {ok_count} 个话题来源。\n\n去“频道列表”里点击“克隆”即可开始历史下载，完成后会自动开始监听。"
                )
                return

            if data == b"topic:cancel":
                pending.pop(user_id, None)
                await event.edit("已取消。")
                return

            parts = data.decode("utf-8", errors="ignore").split(":")
            if len(parts) >= 3 and parts[1] == "p":
                try:
                    state.page = int(parts[2])
                except Exception:
                    state.page = 0
                await event.edit(
                    _topic_selection_text(state, topic_page_size),
                    buttons=_topic_selection_buttons(state, topic_page_size),
                )
                return

            if len(parts) >= 3 and parts[1] == "t":
                try:
                    idx = int(parts[2])
                except Exception:
                    await event.answer("无效选择", alert=True)
                    return
                if 0 <= idx < len(state.topics):
                    if idx in state.selected:
                        state.selected.remove(idx)
                    else:
                        state.selected.add(idx)
                await event.edit(
                    _topic_selection_text(state, topic_page_size),
                    buttons=_topic_selection_buttons(state, topic_page_size),
                )
                return

    @bot_client.on(events.NewMessage())
    async def on_selection_reply(event):
        if not is_admin(event.sender_id):
            return

        user_id = int(event.sender_id)
        state = pending.get(user_id)
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return

        # 处理回复键盘的主菜单按钮（输入框下方的那种）
        if text in {"菜单", "返回", "返回菜单"}:
            pending.pop(user_id, None)
            await show_home(event)
            return
        if text == "帮助":
            pending.pop(user_id, None)
            await event.reply(_help_text(), buttons=_reply_menu_keyboard())
            return
        if text == "频道列表":
            pending.pop(user_id, None)
            await send_source_list(event)
            return
        if text == "超级群组列表":
            pending.pop(user_id, None)
            await send_group_list(event)
            return
        if text == "状态":
            pending.pop(user_id, None)
            await event.reply(_build_status_text(), buttons=_reply_menu_keyboard())
            return
        if text == "添加频道":
            pending[user_id] = PendingTextInput(kind="addchannel")
            await event.reply("请发送频道引用（用户名/链接/ID），例如：@example_channel 或 -1001234567890", buttons=_reply_menu_keyboard())
            return
        if text == "添加群话题":
            pending[user_id] = PendingTextInput(kind="addgroup")
            await event.reply(
                "请发送超级群引用（用户名/链接/ID），可追加关键词过滤。\n"
                "示例：-1001234567890 付费\n"
                "示例：@mygroup",
                buttons=_reply_menu_keyboard(),
            )
            return

        if not state:
            return

        if isinstance(state, PendingTextInput):
            try:
                if state.kind == "addchannel":
                    entity = await _resolve_entity(user_client, _parse_entity_ref(text))
                    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or text
                    raw_id = int(getattr(entity, "id", None) or 0)
                    if not raw_id:
                        raise RuntimeError("无法解析 chat_id")
                    chat_id = _to_telegram_chat_id(raw_id)
                    source_id = db.add_download_source(
                        source_type="channel",
                        chat_id=chat_id,
                        display_title=str(title),
                    )
                    pending.pop(user_id, None)
                    await event.reply(
                        f"已添加频道：{title}\n\n去“频道列表”里点击“克隆”即可开始历史下载，完成后会自动开始监听。",
                        buttons=_reply_menu_keyboard(),
                    )
                    return

                if state.kind == "clone_start":
                    src_id = int((state.payload or {}).get("source_id") or 0)
                    source = db.get_download_source(src_id)
                    if not source:
                        pending.pop(user_id, None)
                        await event.reply("来源不存在", buttons=_reply_menu_keyboard())
                        return

                    chat_hint, thread_id, msg_id = _parse_message_ref(text)
                    if msg_id <= 0:
                        raise ValueError("消息 ID 无效")

                    # 校验链接所属 chat 与来源一致（如提供了 chat_hint）
                    if isinstance(chat_hint, int):
                        if int(chat_hint) != int(source["chat_id"]):
                            raise ValueError("该链接不属于当前来源（chat_id 不一致）")
                    elif isinstance(chat_hint, str):
                        ent = await _resolve_entity(user_client, _parse_entity_ref(chat_hint))
                        raw_id = int(getattr(ent, "id", 0) or 0)
                        if not raw_id:
                            raise ValueError("无法解析链接所属 chat_id")
                        if _to_telegram_chat_id(raw_id) != int(source["chat_id"]):
                            raise ValueError("该链接不属于当前来源（chat_id 不一致）")

                    # Forum 话题：链接里的 thread_id 必须匹配当前来源 topic_id（如果提供了）
                    if source["source_type"] == "forum_topic" and thread_id:
                        topic_id = int(source.get("topic_id") or 0)
                        if topic_id and int(thread_id) != topic_id:
                            raise ValueError("该链接不属于当前话题（thread_id 不一致）")

                    pending.pop(user_id, None)
                    # 如果起始ID小于已记录 last_message_id，则先重置进度（否则会看起来“没从指定ID开始”）
                    prev_last = int(source.get("last_message_id") or 0)
                    if int(msg_id) <= prev_last:
                        db.set_download_source_last_message(src_id, max(0, int(msg_id) - 1))
                        runtime.refresh_sources_cache(force=True)
                        try:
                            await event.reply(
                                f"已重置克隆起点：last_message_id {prev_last} -> {max(0, int(msg_id) - 1)}",
                                buttons=_reply_menu_keyboard(),
                            )
                        except Exception:
                            pass
                    db.set_download_source_active(src_id, False)
                    runtime.refresh_sources_cache(force=True)
                    task = runtime.start_clone(src_id, auto_listen=True, start_from_message_id=msg_id)
                    title = str(source.get("display_title") or f"source_{src_id}")
                    asyncio.create_task(_run_clone_with_progress(int(event.chat_id), src_id, title, task))
                    await event.reply(
                        f"已开始克隆：{source.get('display_title')}\n起始消息ID: {msg_id}\n\n完成后会自动开始监听。",
                        buttons=_reply_menu_keyboard(),
                    )
                    return

                if state.kind == "renamesource":
                    src_id = int((state.payload or {}).get("source_id") or 0)
                    source = db.get_download_source(src_id)
                    if not source:
                        pending.pop(user_id, None)
                        await event.reply("来源不存在", buttons=_reply_menu_keyboard())
                        return

                    new_title = (text or "").strip()
                    if not new_title:
                        raise ValueError("名称不能为空")
                    if len(new_title) > 80:
                        raise ValueError("名称过长（最多 80 字符）")

                    ok = db.set_download_source_display_title(src_id, new_title)
                    runtime.refresh_sources_cache(force=True)
                    pending.pop(user_id, None)
                    await event.reply(
                        f"已重命名：{source.get('display_title') or f'source_{src_id}'} -> {new_title}\n"
                        "后续下载/上传会使用新名称作为博主文件夹名。",
                        buttons=_reply_menu_keyboard(),
                    )
                    try:
                        await send_source_list(event)
                    except Exception:
                        pass
                    return

                if state.kind == "patchclone":
                    src_id = int((state.payload or {}).get("source_id") or 0)
                    source = db.get_download_source(src_id)
                    if not source:
                        pending.pop(user_id, None)
                        await event.reply("来源不存在", buttons=_reply_menu_keyboard())
                        return

                    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
                    if not lines:
                        raise ValueError("请至少提供一条消息链接/ID")

                    # 逐条解析并校验归属
                    msg_ids: List[int] = []
                    for ln in lines:
                        chat_hint, thread_id, msg_id = _parse_message_ref(ln)
                        if msg_id <= 0:
                            continue

                        if isinstance(chat_hint, int) and int(chat_hint) != int(source["chat_id"]):
                            raise ValueError("存在不属于当前来源的链接（chat_id 不一致）")

                        if source["source_type"] == "forum_topic" and thread_id:
                            topic_id = int(source.get("topic_id") or 0)
                            if topic_id and int(thread_id) != topic_id:
                                raise ValueError("存在不属于当前话题的链接（thread_id 不一致）")

                        msg_ids.append(int(msg_id))

                    if not msg_ids:
                        raise ValueError("未解析到有效的消息 ID")

                    pending.pop(user_id, None)
                    title = str(source.get("display_title") or f"source_{src_id}")

                    # 启动任务（不改变克隆状态，不影响监听开关）
                    progress_msg = await event.reply(f"🔁 补克隆已提交：0/{len(msg_ids)}\n来源：{title}", buttons=_reply_menu_keyboard())

                    async def _run_patch() -> None:
                        done = 0
                        tasks = [asyncio.create_task(runtime.download_group_by_message_id(src_id, mid)) for mid in msg_ids]
                        for fut in asyncio.as_completed(tasks):
                            try:
                                await fut
                            except Exception:
                                pass
                            done += 1
                            if done % 2 == 0 or done == len(tasks):
                                try:
                                    await bot_client.edit_message(
                                        int(event.chat_id),
                                        int(progress_msg.id),
                                        f"🔁 补克隆进行中：{done}/{len(tasks)}\n来源：{title}",
                                        buttons=_reply_menu_keyboard(),
                                    )
                                except Exception:
                                    pass
                        try:
                            await bot_client.edit_message(
                                int(event.chat_id),
                                int(progress_msg.id),
                                f"✅ 补克隆完成：{len(tasks)}/{len(tasks)}\n来源：{title}",
                                buttons=_reply_menu_keyboard(),
                            )
                        except Exception:
                            pass

                    asyncio.create_task(_run_patch())
                    return

                if state.kind == "addgroup":
                    parts = text.split(maxsplit=1)
                    ref = parts[0]
                    query = parts[1].strip() if len(parts) > 1 else ""
                    entity = await _resolve_entity(user_client, _parse_entity_ref(ref))
                    group_title = getattr(entity, "title", None) or ref
                    raw_id = int(getattr(entity, "id", None) or 0)
                    if not raw_id:
                        raise RuntimeError("无法解析 chat_id")
                    chat_id = _to_telegram_chat_id(raw_id)
                    try:
                        db.upsert_forum_group(chat_id, str(group_title))
                    except Exception:
                        pass
                    topics = await _fetch_forum_topics(user_client, entity, query=query)
                    if not topics:
                        pending.pop(user_id, None)
                        await event.reply("未获取到任何话题，请确认该群已启用 Forum Topics", buttons=_reply_menu_keyboard())
                        return

                    topic_rows: List[dict] = []
                    for t in topics:
                        topic_rows.append(
                            {
                                "id": int(getattr(t, "id", 0) or 0),
                                "title": str(getattr(t, "title", "") or ""),
                                "top_message": int(getattr(t, "top_message", 0) or 0),
                            }
                        )

                    topic_rows, hidden_count = _filter_existing_forum_topic_rows(db, chat_id, topic_rows)
                    if not topic_rows:
                        pending.pop(user_id, None)
                        await event.reply("该群话题已全部添加，无需重复操作。", buttons=_reply_menu_keyboard())
                        return

                    sel = PendingTopicSelection(
                        chat_id=chat_id,
                        group_title=str(group_title),
                        topics=topic_rows,
                        query=query,
                        hidden_count=hidden_count,
                    )
                    pending[user_id] = sel
                    await event.reply(
                        _topic_selection_text(sel, topic_page_size),
                        buttons=_topic_selection_buttons(sel, topic_page_size),
                    )
                    return

            except Exception as e:
                pending.pop(user_id, None)
                await event.reply(f"操作失败: {e}", buttons=_reply_menu_keyboard())
                return

    import logging

    logger = logging.getLogger(__name__)
    logger.info("管理 Bot 已启动")
    try:
        await bot_client.run_until_disconnected()
    finally:
        pending.clear()
        await runtime.stop()
        await bot_client.disconnect()
        await user_client.disconnect()


def main() -> None:
    try:
        asyncio.run(main_async())
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
