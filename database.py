#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SQLite 数据库（仅用于“来源管理 + 媒体组下载进度”）

本项目已移除旧的“推送/中转/标题润色”等逻辑，这里只保留：
- download_sources：下载来源（频道 / 超级群 Forum 话题）
- download_counters：同一来源同一日期的序号（001/002/...）
- download_groups：媒体组下载/上传状态（去重、进度、失败原因）
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/aggregator.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        try:
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA temp_store=MEMORY;")
            self.conn.execute("PRAGMA cache_size=-64000;")
            self.conn.execute("PRAGMA busy_timeout=30000;")
        except Exception as e:
            logger.warning(f"设置数据库优化参数失败: {e}")

        self._init_tables()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_tables(self) -> None:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS forum_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS download_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER,
                topic_root_message_id INTEGER NOT NULL DEFAULT 0,
                display_title TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_message_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, topic_root_message_id)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS download_counters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                last_seq INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id, date),
                FOREIGN KEY (source_id) REFERENCES download_sources(id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS download_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                group_uid TEXT NOT NULL,
                date TEXT NOT NULL,
                seq INTEGER NOT NULL,
                last_message_id INTEGER NOT NULL,
                local_dir TEXT,
                remote_dir TEXT,
                status TEXT DEFAULT 'downloaded',
                error_message TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                uploaded_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                local_deleted BOOLEAN DEFAULT 0,
                UNIQUE(source_id, group_uid),
                FOREIGN KEY (source_id) REFERENCES download_sources(id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_download_sources_active ON download_sources(is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_download_groups_source_id ON download_groups(source_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_download_groups_status ON download_groups(status)")

        # 兜底：将历史 download_sources.topic_root_message_id 的 NULL 归一化为 0
        try:
            cursor.execute("UPDATE download_sources SET topic_root_message_id=0 WHERE topic_root_message_id IS NULL")
        except Exception:
            pass

        # 迁移：Forum 话题的 thread_id 应为 topic_id（t.me/c/.../<thread_id>/<msg_id>），
        # 历史上可能误把 “top_message(最新消息)” 写进 topic_root_message_id，导致 reply_to 失败。
        try:
            cursor.execute(
                """
                UPDATE download_sources
                SET topic_root_message_id=topic_id, updated_at=CURRENT_TIMESTAMP
                WHERE source_type='forum_topic'
                  AND topic_id IS NOT NULL
                  AND CAST(topic_id AS INTEGER) > 0
                  AND CAST(topic_root_message_id AS INTEGER) != CAST(topic_id AS INTEGER)
                  AND NOT EXISTS (
                    SELECT 1 FROM download_sources d2
                    WHERE d2.chat_id = download_sources.chat_id
                      AND CAST(d2.topic_root_message_id AS INTEGER) = CAST(download_sources.topic_id AS INTEGER)
                      AND d2.id != download_sources.id
                  )
                """
            )
        except Exception:
            pass

        self.conn.commit()

    # -------------------------
    # Forum groups
    # -------------------------

    def upsert_forum_group(self, chat_id: int, title: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO forum_groups (chat_id, title) VALUES (?, ?)",
            (int(chat_id), str(title)),
        )
        cursor.execute(
            "UPDATE forum_groups SET title=?, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?",
            (str(title), int(chat_id)),
        )
        self.conn.commit()

        cursor.execute("SELECT id FROM forum_groups WHERE chat_id=?", (int(chat_id),))
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_forum_group(self, group_id: int) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, chat_id, title FROM forum_groups WHERE id=?", (int(group_id),))
        r = cursor.fetchone()
        if not r:
            return None
        return {"id": int(r[0]), "chat_id": int(r[1]), "title": r[2]}

    def list_forum_groups(self) -> list:
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, chat_id, title FROM forum_groups ORDER BY updated_at DESC, id DESC")
        rows = cursor.fetchall()
        return [{"id": int(r[0]), "chat_id": int(r[1]), "title": r[2]} for r in rows]

    def delete_forum_group(self, group_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM forum_groups WHERE id=?", (int(group_id),))
        self.conn.commit()
        return cursor.rowcount > 0

    # -------------------------
    # Sources
    # -------------------------

    def add_download_source(
        self,
        source_type: str,
        chat_id: int,
        display_title: str,
        topic_id: Optional[int] = None,
        topic_root_message_id: Optional[int] = None,
        is_active: bool = False,
    ) -> int:
        normalized_root_id = int(topic_root_message_id or 0)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO download_sources
                (source_type, chat_id, topic_id, topic_root_message_id, display_title, is_active)
            VALUES
                (?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_type),
                int(chat_id),
                int(topic_id) if topic_id is not None else None,
                int(normalized_root_id),
                str(display_title),
                1 if is_active else 0,
            ),
        )
        self.conn.commit()

        cursor.execute(
            "SELECT id FROM download_sources WHERE chat_id=? AND topic_root_message_id=?",
            (int(chat_id), int(normalized_root_id)),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_download_source(self, source_id: int) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, source_type, chat_id, topic_id, topic_root_message_id, display_title, is_active, last_message_id
            FROM download_sources
            WHERE id=?
            """,
            (int(source_id),),
        )
        r = cursor.fetchone()
        if not r:
            return None
        return {
            "id": int(r[0]),
            "source_type": str(r[1]),
            "chat_id": int(r[2]),
            "topic_id": r[3],
            "topic_root_message_id": int(r[4] or 0),
            "display_title": r[5],
            "is_active": bool(r[6]),
            "last_message_id": int(r[7] or 0),
        }

    def list_download_sources(self, active_only: bool = False) -> list:
        cursor = self.conn.cursor()
        if active_only:
            cursor.execute(
                """
                SELECT id, source_type, chat_id, topic_id, topic_root_message_id, display_title, is_active, last_message_id
                FROM download_sources
                WHERE is_active=1
                ORDER BY id ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, source_type, chat_id, topic_id, topic_root_message_id, display_title, is_active, last_message_id
                FROM download_sources
                ORDER BY id ASC
                """
            )
        rows = cursor.fetchall()
        return [
            {
                "id": int(r[0]),
                "source_type": str(r[1]),
                "chat_id": int(r[2]),
                "topic_id": r[3],
                "topic_root_message_id": int(r[4] or 0),
                "display_title": r[5],
                "is_active": bool(r[6]),
                "last_message_id": int(r[7] or 0),
            }
            for r in rows
        ]

    def set_download_source_active(self, source_id: int, is_active: bool) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE download_sources
            SET is_active=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (1 if is_active else 0, int(source_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_download_source_last_message(self, source_id: int, last_message_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE download_sources
            SET last_message_id=CASE WHEN last_message_id < ? THEN ? ELSE last_message_id END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(last_message_id), int(last_message_id), int(source_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_download_source_last_message(self, source_id: int, last_message_id: int) -> bool:
        """强制设置 last_message_id（允许回退，用于从较小 ID 重新克隆）。"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE download_sources
            SET last_message_id=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(last_message_id), int(source_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def delete_download_source(self, source_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM download_sources WHERE id=?", (int(source_id),))
        self.conn.commit()
        return cursor.rowcount > 0

    # -------------------------
    # Counters / groups
    # -------------------------

    def get_next_download_seq(self, source_id: int, date: str) -> int:
        """获取并递增某来源在某日期下的序号（从 1 开始）。"""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT last_seq FROM download_counters WHERE source_id=? AND date=?",
                (int(source_id), str(date)),
            )
            row = cursor.fetchone()
            if row is None:
                last_seq = 0
                cursor.execute(
                    "INSERT INTO download_counters (source_id, date, last_seq) VALUES (?, ?, 0)",
                    (int(source_id), str(date)),
                )
            else:
                last_seq = int(row[0] or 0)

            next_seq = last_seq + 1
            cursor.execute(
                """
                UPDATE download_counters
                SET last_seq=?, updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND date=?
                """,
                (int(next_seq), int(source_id), str(date)),
            )
        return int(next_seq)

    def download_group_exists(self, source_id: int, group_uid: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            # failed 允许重试，因此不算“已存在”
            "SELECT 1 FROM download_groups WHERE source_id=? AND group_uid=? AND status!='failed' LIMIT 1",
            (int(source_id), str(group_uid)),
        )
        return cursor.fetchone() is not None

    def add_download_group(
        self,
        source_id: int,
        group_uid: str,
        date: str,
        seq: int,
        last_message_id: int,
        local_dir: str,
        remote_dir: Optional[str],
        status: str = "downloaded",
        error_message: Optional[str] = None,
    ) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO download_groups
                (source_id, group_uid, date, seq, last_message_id, local_dir, remote_dir, status, error_message)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, group_uid) DO UPDATE SET
                date=excluded.date,
                seq=excluded.seq,
                last_message_id=excluded.last_message_id,
                local_dir=excluded.local_dir,
                remote_dir=excluded.remote_dir,
                status=excluded.status,
                error_message=excluded.error_message,
                updated_at=CURRENT_TIMESTAMP,
                downloaded_at=CASE
                    WHEN excluded.status='downloaded' THEN CURRENT_TIMESTAMP
                    ELSE download_groups.downloaded_at
                END
            """,
            (
                int(source_id),
                str(group_uid),
                str(date),
                int(seq),
                int(last_message_id),
                str(local_dir),
                remote_dir,
                str(status),
                str(error_message) if error_message is not None else None,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_download_group_uploaded(
        self, source_id: int, group_uid: str, uploaded: bool, remote_dir: str, local_deleted: bool
    ) -> bool:
        cursor = self.conn.cursor()
        if uploaded:
            cursor.execute(
                """
                UPDATE download_groups
                SET status='uploaded', remote_dir=?, uploaded_at=CURRENT_TIMESTAMP, local_deleted=?, updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND group_uid=?
                """,
                (str(remote_dir), 1 if local_deleted else 0, int(source_id), str(group_uid)),
            )
        else:
            cursor.execute(
                """
                UPDATE download_groups
                SET status='downloaded', remote_dir=?, local_deleted=?, updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND group_uid=?
                """,
                (str(remote_dir), 1 if local_deleted else 0, int(source_id), str(group_uid)),
            )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_download_group_failed(self, source_id: int, group_uid: str, error_message: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE download_groups
            SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP
            WHERE source_id=? AND group_uid=?
            """,
            (str(error_message), int(source_id), str(group_uid)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def count_download_groups_by_status(self, source_id: int) -> dict:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT status, COUNT(1) AS cnt
            FROM download_groups
            WHERE source_id=?
            GROUP BY status
            """,
            (int(source_id),),
        )
        rows = cursor.fetchall()
        counts = {str(r[0]): int(r[1]) for r in rows}
        counts["total"] = int(sum(counts.values()))
        return counts

    def get_download_group(self, source_id: int, group_uid: str) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT source_id, group_uid, date, seq, last_message_id, local_dir, remote_dir, status, error_message, local_deleted
            FROM download_groups
            WHERE source_id=? AND group_uid=?
            LIMIT 1
            """,
            (int(source_id), str(group_uid)),
        )
        r = cursor.fetchone()
        if not r:
            return None
        return {
            "source_id": int(r[0]),
            "group_uid": str(r[1]),
            "date": str(r[2]),
            "seq": int(r[3]),
            "last_message_id": int(r[4] or 0),
            "local_dir": r[5],
            "remote_dir": r[6],
            "status": str(r[7]),
            "error_message": r[8],
            "local_deleted": bool(r[9]),
        }
