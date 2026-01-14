#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""下载来源管理工具

用途：
- 添加频道：自动读取频道标题
- 添加超级群 Forum：拉取话题列表并选择要加入工作列表的标题
- 查看/启用/停用来源

注意：
- 需要已生成用户会话：`python setup_user_session.py`
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple

from dotenv import load_dotenv

try:
    import socks
except ImportError:
    socks = None

from telethon import TelegramClient, functions

from database import Database


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


def build_proxy() -> Optional[Tuple[int, str, int, bool, Optional[str], Optional[str]]]:
    if not _env_bool("PROXY_ENABLED", False):
        return None

    if socks is None:
        print("⚠️ 代理已启用但未安装 PySocks（pysocks）")
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


async def ensure_user_client() -> TelegramClient:
    load_dotenv()

    Path("sessions").mkdir(parents=True, exist_ok=True)

    api_id = _env_int("API_ID")
    api_hash = _env_str("API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError("请在 .env 中配置 API_ID 与 API_HASH")

    proxy = build_proxy()
    client = TelegramClient("sessions/user_session", int(api_id), str(api_hash), proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("未检测到用户会话，请先运行: python setup_user_session.py")

    return client


def _parse_entity_ref(value: str):
    value = (value or "").strip()
    if not value:
        raise ValueError("缺少频道/群组引用")

    if value.lstrip("-").isdigit():
        return int(value)

    if value.startswith("t.me/"):
        return "https://" + value
    if value.startswith(("http://", "https://")):
        return value
    return value


async def _resolve_entity(client: TelegramClient, ref):
    last_error: Optional[Exception] = None

    def candidate_ids_from_ref(v: int) -> List[int]:
        s = str(int(v))
        candidates: List[int] = [int(v)]
        if s.startswith("-100") and s[4:].isdigit():
            raw_id = int(s[4:])
            candidates.append(raw_id)
            candidates.append(_to_telegram_chat_id(raw_id))
        if int(v) > 0:
            candidates.append(_to_telegram_chat_id(int(v)))
        seen: Set[int] = set()
        uniq: List[int] = []
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            uniq.append(int(c))
        return uniq

    candidates: List = [ref]
    if isinstance(ref, int):
        candidates = [int(x) for x in candidate_ids_from_ref(ref)]

    for cand in candidates:
        try:
            return await client.get_entity(cand)
        except Exception as e:
            last_error = e

    if isinstance(ref, int):
        cand_ids = set(int(x) for x in candidate_ids_from_ref(ref))
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
            "\n提示：私密超级群/频道仅凭 -100... ID 可能无法解析，请确认用户账号已加入并在会话列表中可见。"
        )
    raise RuntimeError(f"Cannot find any entity corresponding to {ref!r}{hint}") from last_error


def _to_telegram_chat_id(raw_id: int) -> int:
    # Telethon 的 entity.id 通常为正数；消息中的 chat_id 常见为 -100 前缀形式
    if int(raw_id) <= 0:
        return int(raw_id)
    return int(f"-100{int(raw_id)}")


async def cmd_list(db: Database, active_only: bool) -> None:
    rows = db.list_download_sources(active_only=active_only)
    if not rows:
        print("暂无来源")
        return

    for r in rows:
        extra = ""
        if r["source_type"] == "forum_topic":
            extra = f" topic_id={r['topic_id']} root={r['topic_root_message_id']}"
        print(
            f"[{r['id']}] {r['source_type']} chat_id={r['chat_id']}{extra} active={int(r['is_active'])} last={r['last_message_id']} title={r['display_title']}"
        )


async def cmd_enable(db: Database, source_id: int, enabled: bool) -> None:
    ok = db.set_download_source_active(source_id, enabled)
    print("OK" if ok else "未找到来源")


async def cmd_add_channel(db: Database, ref: str) -> None:
    client = await ensure_user_client()
    try:
        entity = await _resolve_entity(client, _parse_entity_ref(ref))
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(ref)
        chat_id = int(getattr(entity, "id", None) or 0)
        if not chat_id:
            raise RuntimeError("无法解析 chat_id")

        source_id = db.add_download_source(
            source_type="channel",
            chat_id=_to_telegram_chat_id(chat_id),
            display_title=str(title),
        )
        print(f"已添加频道: id={source_id} title={title}")
    finally:
        await client.disconnect()


async def _fetch_forum_topics(client: TelegramClient, peer) -> List:
    result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=peer,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q="",
        )
    )
    topics = getattr(result, "topics", None)
    if topics is None:
        topics = getattr(result, "forum_topics", None)
    return list(topics or [])


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


async def cmd_add_group_topics(db: Database, ref: str, select: Optional[str]) -> None:
    client = await ensure_user_client()
    try:
        entity = await _resolve_entity(client, _parse_entity_ref(ref))
        group_title = getattr(entity, "title", None) or str(ref)
        chat_id = int(getattr(entity, "id", None) or 0)
        if not chat_id:
            raise RuntimeError("无法解析 chat_id")

        chat_id = _to_telegram_chat_id(chat_id)
        try:
            db.upsert_forum_group(chat_id, str(group_title))
        except Exception:
            pass

        topics = await _fetch_forum_topics(client, entity)
        if not topics:
            print("未获取到任何话题，请确认该群已启用 Forum Topics")
            return

        print(f"话题列表：{group_title} (chat_id={chat_id})")
        for idx, t in enumerate(topics, 1):
            title = getattr(t, "title", "")
            tid = getattr(t, "id", None)
            top_message = getattr(t, "top_message", None)
            print(f"{idx:>3}. id={tid} root={top_message} title={title}")

        if not select:
            select = input("选择要加入工作列表的话题序号（例如 1,3,5 或 1-10 或 all）: ").strip()

        picked = _parse_selection(select, len(topics))
        if not picked:
            print("未选择任何话题")
            return

        for idx in picked:
            topic = topics[idx - 1]
            topic_title = getattr(topic, "title", "")
            topic_id = getattr(topic, "id", None)
            topic_root = getattr(topic, "top_message", None)
            display_title = f"{group_title}__{topic_title}" if topic_title else str(group_title)
            source_id = db.add_download_source(
                source_type="forum_topic",
                chat_id=chat_id,
                display_title=display_title,
                topic_id=int(topic_id) if topic_id is not None else None,
                topic_root_message_id=int(topic_id) if topic_id is not None else None,
            )
            print(f"已添加话题: id={source_id} title={display_title}")

    finally:
        await client.disconnect()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="下载来源管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出来源")
    p_list.add_argument("--active", action="store_true", help="仅显示启用的")

    p_add_ch = sub.add_parser("add-channel", help="添加频道")
    p_add_ch.add_argument("ref", help="频道用户名/链接/ID")

    p_add_group = sub.add_parser("add-group", help="添加超级群话题")
    p_add_group.add_argument("ref", help="群组用户名/链接/ID")
    p_add_group.add_argument("--select", help="直接指定选择序号，如 1,3,5 或 1-10 或 all")

    p_enable = sub.add_parser("enable", help="启用来源")
    p_enable.add_argument("id", type=int)

    p_disable = sub.add_parser("disable", help="停用来源")
    p_disable.add_argument("id", type=int)

    return parser


async def main_async() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    db = Database()

    if args.cmd == "list":
        await cmd_list(db, active_only=bool(args.active))
        return

    if args.cmd == "add-channel":
        await cmd_add_channel(db, args.ref)
        return

    if args.cmd == "add-group":
        await cmd_add_group_topics(db, args.ref, args.select)
        return

    if args.cmd == "enable":
        await cmd_enable(db, int(args.id), True)
        return

    if args.cmd == "disable":
        await cmd_enable(db, int(args.id), False)
        return


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
