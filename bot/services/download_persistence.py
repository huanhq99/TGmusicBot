#!/usr/bin/env python3
"""
下载队列持久化模块
提供下载任务的数据库持久化功能
"""

import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


def init_download_queue_table(db_path: str):
    """初始化 download_queue 表"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_queue (
                id TEXT PRIMARY KEY,
                song_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                platform TEXT,
                quality TEXT,
                output_dir TEXT,
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                user_id TEXT,
                chat_id TEXT,
                message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        logger.info("download_queue 表已初始化")
    except Exception as e:
        logger.error(f"初始化 download_queue 表失败: {e}")


def persist_task(db_path: str, task) -> bool:
    """持久化下载任务到数据库"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO download_queue 
            (id, song_id, title, artist, platform, quality, output_dir,
             status, progress, retry_count, error_message, user_id, chat_id, message_id,
             created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.id, task.song_id, task.title, task.artist, task.platform,
            task.quality, task.output_dir, task.status.value, task.progress,
            task.retry_count, task.error_message, task.user_id, task.chat_id,
            task.message_id,
            datetime.fromtimestamp(task.created_at).isoformat() if task.created_at else None,
            datetime.fromtimestamp(task.started_at).isoformat() if task.started_at else None,
            datetime.fromtimestamp(task.completed_at).isoformat() if task.completed_at else None
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"持久化任务失败: {e}")
        return False


def remove_persisted_task(db_path: str, task_id: str) -> bool:
    """从数据库移除已完成任务"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM download_queue WHERE id = ?', (task_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"移除持久化任务失败: {e}")
        return False


def get_pending_tasks(db_path: str) -> List[dict]:
    """获取所有未完成任务"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM download_queue 
            WHERE status IN ('pending', 'downloading', 'retrying')
            ORDER BY created_at ASC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"获取未完成任务失败: {e}")
        return []


def update_task_status(db_path: str, task_id: str, status: str, 
                       error_message: str = None) -> bool:
    """更新任务状态"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        if status == 'completed':
            cursor.execute('''
                UPDATE download_queue 
                SET status = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (status, task_id))
        elif status == 'downloading':
            cursor.execute('''
                UPDATE download_queue 
                SET status = ?, started_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (status, task_id))
        else:
            if error_message:
                cursor.execute('''
                    UPDATE download_queue 
                    SET status = ?, error_message = ?
                    WHERE id = ?
                ''', (status, error_message, task_id))
            else:
                cursor.execute('''
                    UPDATE download_queue SET status = ? WHERE id = ?
                ''', (status, task_id))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"更新任务状态失败: {e}")
        return False


def clear_completed_tasks(db_path: str, days: int = 7) -> int:
    """清理已完成任务（保留最近 N 天）"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM download_queue 
            WHERE status IN ('completed', 'cancelled', 'failed')
            AND completed_at < datetime('now', '-' || ? || ' days')
        ''', (days,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"清理了 {deleted} 个旧任务")
        return deleted
    except Exception as e:
        logger.error(f"清理任务失败: {e}")
        return 0
