#!/usr/bin/env python3
"""
数据库操作封装
"""

import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Database:
    """SQLite 数据库封装"""
    
    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
    
    def connect(self) -> sqlite3.Connection:
        """获取数据库连接"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn
    
    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None
    
    @contextmanager
    def cursor(self):
        """获取游标的上下文管理器"""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
    
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """执行 SQL"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return cursor
    
    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """查询单条记录"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.fetchone()
    
    def fetch_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        """查询所有记录"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.fetchall()
    
    def get_setting(self, key: str, default: str = '') -> str:
        """获取设置项"""
        row = self.fetch_one(
            'SELECT value FROM bot_settings WHERE key = ?', (key,)
        )
        if row:
            return row['value'] if row['value'] else default
        return default
    
    def set_setting(self, key: str, value: str):
        """设置设置项"""
        self.execute('''
            INSERT OR REPLACE INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
    
    def init_tables(self):
        """初始化数据库表"""
        with self.cursor() as cursor:
            # bot_settings 表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # users 表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id TEXT PRIMARY KEY,
                    emby_user_id TEXT,
                    emby_token TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # subscriptions 表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id TEXT NOT NULL,
                    playlist_url TEXT NOT NULL,
                    playlist_name TEXT,
                    platform TEXT DEFAULT 'ncm',
                    interval_minutes INTEGER DEFAULT 360,
                    last_sync TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, playlist_url)
                )
            ''')
            
            # download_history 表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    song_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    platform TEXT,
                    quality TEXT,
                    file_path TEXT,
                    file_size INTEGER,
                    status TEXT DEFAULT 'completed',
                    error_message TEXT,
                    user_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # download_queue 表（持久化队列）
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
            
            # song_requests 表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS song_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id TEXT NOT NULL,
                    song_name TEXT NOT NULL,
                    artist TEXT,
                    album TEXT,
                    source_url TEXT,
                    status TEXT DEFAULT 'pending',
                    admin_note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP
                )
            ''')
            
            logger.info("数据库表初始化完成")


# 全局数据库实例
_db_instance: Optional[Database] = None


def get_database() -> Database:
    """获取数据库实例"""
    global _db_instance
    if _db_instance is None:
        from bot.config import DATABASE_FILE
        _db_instance = Database(DATABASE_FILE)
        _db_instance.init_tables()
    return _db_instance


def init_database(db_path: Union[str, Path]) -> Database:
    """初始化数据库"""
    global _db_instance
    _db_instance = Database(db_path)
    _db_instance.init_tables()
    return _db_instance
