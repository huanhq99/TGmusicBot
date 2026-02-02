#!/usr/bin/env python3
"""
下载管理器 - 队列管理、并发控制、重试机制、统计监控
"""

import asyncio
import logging
import time
import json
import sqlite3
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timedelta
from collections import deque
import threading

logger = logging.getLogger(__name__)


class DownloadStatus(Enum):
    """下载状态"""
    PENDING = "pending"          # 等待中
    DOWNLOADING = "downloading"  # 下载中
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"            # 失败
    RETRYING = "retrying"        # 重试中
    CANCELLED = "cancelled"      # 已取消


@dataclass
class DownloadTask:
    """下载任务"""
    id: str                      # 任务 ID
    song_id: str                 # 歌曲 ID
    title: str                   # 歌曲标题
    artist: str                  # 歌手
    platform: str                # 平台 (NCM/QQ)
    quality: str                 # 音质
    output_dir: str              # 输出目录
    
    status: DownloadStatus = DownloadStatus.PENDING
    progress: int = 0            # 进度 0-100
    retry_count: int = 0         # 重试次数
    max_retries: int = 3         # 最大重试次数
    error_message: str = ""      # 错误信息
    file_path: str = ""          # 下载后的文件路径
    file_size: int = 0           # 文件大小
    
    created_at: float = field(default_factory=time.time)
    started_at: float = 0
    completed_at: float = 0
    
    # 回调
    user_id: str = ""            # 请求用户
    chat_id: str = ""            # 聊天 ID
    message_id: int = 0          # 消息 ID（用于更新进度）
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> 'DownloadTask':
        d['status'] = DownloadStatus(d['status'])
        return cls(**d)
    
    @property
    def display_name(self) -> str:
        return f"{self.title} - {self.artist}"
    
    @property
    def duration(self) -> float:
        """下载耗时（秒）"""
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        elif self.started_at:
            return time.time() - self.started_at
        return 0


class DownloadQueue:
    """下载队列 - 线程安全"""
    
    def __init__(self, max_size: int = 1000):
        self._queue: deque[DownloadTask] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._task_map: Dict[str, DownloadTask] = {}  # id -> task
    
    def add(self, task: DownloadTask) -> bool:
        """添加任务到队列"""
        with self._lock:
            if task.id in self._task_map:
                return False  # 已存在
            self._queue.append(task)
            self._task_map[task.id] = task
            return True
    
    def get_next(self) -> Optional[DownloadTask]:
        """获取下一个待处理任务"""
        with self._lock:
            for task in self._queue:
                if task.status == DownloadStatus.PENDING:
                    return task
            return None
    
    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """根据 ID 获取任务"""
        return self._task_map.get(task_id)
    
    def remove(self, task_id: str) -> bool:
        """移除任务"""
        with self._lock:
            if task_id in self._task_map:
                task = self._task_map.pop(task_id)
                self._queue.remove(task)
                return True
            return False
    
    def clear_completed(self) -> int:
        """清理已完成的任务"""
        with self._lock:
            to_remove = [t for t in self._queue 
                        if t.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED)]
            for task in to_remove:
                self._queue.remove(task)
                self._task_map.pop(task.id, None)
            return len(to_remove)
    
    def get_all(self) -> List[DownloadTask]:
        """获取所有任务"""
        with self._lock:
            return list(self._queue)
    
    def get_pending_count(self) -> int:
        """待处理任务数"""
        with self._lock:
            return sum(1 for t in self._queue if t.status == DownloadStatus.PENDING)
    
    def get_active_count(self) -> int:
        """正在下载的任务数"""
        with self._lock:
            return sum(1 for t in self._queue 
                      if t.status in (DownloadStatus.DOWNLOADING, DownloadStatus.RETRYING))
    
    def __len__(self) -> int:
        return len(self._queue)


class DownloadStats:
    """下载统计"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """初始化统计数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                platform TEXT NOT NULL,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                total_size INTEGER DEFAULT 0,
                total_duration REAL DEFAULT 0,
                UNIQUE(date, platform)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                song_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                platform TEXT,
                quality TEXT,
                status TEXT,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                duration REAL DEFAULT 0,
                error_message TEXT,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def record_download(self, task: DownloadTask):
        """记录下载结果"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 记录历史
        cursor.execute('''
            INSERT INTO download_history 
            (task_id, song_id, title, artist, platform, quality, status, 
             file_path, file_size, duration, error_message, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.id, task.song_id, task.title, task.artist, task.platform,
            task.quality, task.status.value, task.file_path, task.file_size,
            task.duration, task.error_message, task.user_id
        ))
        
        # 更新统计
        is_success = task.status == DownloadStatus.COMPLETED
        cursor.execute('''
            INSERT INTO download_stats (date, platform, success_count, fail_count, total_size, total_duration)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, platform) DO UPDATE SET
                success_count = success_count + ?,
                fail_count = fail_count + ?,
                total_size = total_size + ?,
                total_duration = total_duration + ?
        ''', (
            today, task.platform,
            1 if is_success else 0,
            0 if is_success else 1,
            task.file_size if is_success else 0,
            task.duration,
            1 if is_success else 0,
            0 if is_success else 1,
            task.file_size if is_success else 0,
            task.duration
        ))
        
        conn.commit()
        conn.close()
    
    def get_today_stats(self) -> dict:
        """获取今日统计"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT platform, success_count, fail_count, total_size, total_duration
            FROM download_stats WHERE date = ?
        ''', (today,))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = {
            'total_success': 0,
            'total_fail': 0,
            'total_size': 0,
            'by_platform': {}
        }
        
        for row in rows:
            result['total_success'] += row['success_count']
            result['total_fail'] += row['fail_count']
            result['total_size'] += row['total_size']
            result['by_platform'][row['platform']] = {
                'success': row['success_count'],
                'fail': row['fail_count'],
                'size': row['total_size']
            }
        
        return result
    
    def get_weekly_stats(self) -> List[dict]:
        """获取最近7天统计"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT date, SUM(success_count) as success, SUM(fail_count) as fail, 
                   SUM(total_size) as size
            FROM download_stats 
            WHERE date >= ?
            GROUP BY date
            ORDER BY date
        ''', (week_ago,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def get_recent_history(self, limit: int = 50) -> List[dict]:
        """获取最近下载历史"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM download_history 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]


class CookieManager:
    """Cookie 管理器 - 过期检测、自动提醒"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """初始化 Cookie 表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cookie_status (
                platform TEXT PRIMARY KEY,
                cookie TEXT,
                is_valid INTEGER DEFAULT 1,
                last_check TIMESTAMP,
                last_valid TIMESTAMP,
                expires_warning_sent INTEGER DEFAULT 0,
                nickname TEXT,
                is_vip INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def update_status(self, platform: str, is_valid: bool, info: dict = None):
        """更新 Cookie 状态"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        
        if is_valid:
            cursor.execute('''
                INSERT INTO cookie_status (platform, is_valid, last_check, last_valid, nickname, is_vip)
                VALUES (?, 1, ?, ?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    is_valid = 1,
                    last_check = ?,
                    last_valid = ?,
                    nickname = COALESCE(?, nickname),
                    is_vip = COALESCE(?, is_vip),
                    expires_warning_sent = 0
            ''', (
                platform, now, now, 
                info.get('nickname') if info else None,
                info.get('is_vip', 0) if info else 0,
                now, now,
                info.get('nickname') if info else None,
                info.get('is_vip', 0) if info else 0
            ))
        else:
            cursor.execute('''
                INSERT INTO cookie_status (platform, is_valid, last_check)
                VALUES (?, 0, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    is_valid = 0,
                    last_check = ?
            ''', (platform, now, now))
        
        conn.commit()
        conn.close()
    
    def get_status(self, platform: str) -> Optional[dict]:
        """获取 Cookie 状态"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM cookie_status WHERE platform = ?', (platform,))
        row = cursor.fetchone()
        conn.close()
        
        return dict(row) if row else None
    
    def get_all_status(self) -> List[dict]:
        """获取所有平台状态"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM cookie_status')
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def should_warn_expiry(self, platform: str, hours_before: int = 24) -> bool:
        """是否应该发送过期预警"""
        status = self.get_status(platform)
        if not status:
            return False
        
        if status['expires_warning_sent']:
            return False
        
        if not status['is_valid']:
            return True
        
        # 如果最后有效时间超过一定时间，可能快过期了
        if status['last_valid']:
            last_valid = datetime.fromisoformat(status['last_valid'])
            # 简单逻辑：如果超过5天没验证过，提醒用户检查
            if datetime.now() - last_valid > timedelta(days=5):
                return True
        
        return False
    
    def mark_warning_sent(self, platform: str):
        """标记已发送警告"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE cookie_status SET expires_warning_sent = 1 WHERE platform = ?
        ''', (platform,))
        conn.commit()
        conn.close()


class DownloadManager:
    """
    下载管理器 - 核心类
    
    功能：
    - 队列管理
    - 并发控制
    - 自动重试
    - 进度回调
    - 统计记录
    """
    
    def __init__(self, db_path: str, max_concurrent: int = 3, 
                 max_retries: int = 3, retry_delay: float = 2.0):
        self.db_path = db_path
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        self.queue = DownloadQueue()
        self.stats = DownloadStats(db_path)
        self.cookie_manager = CookieManager(db_path)
        
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._worker_task: Optional[asyncio.Task] = None
        
        # 下载器实例（延迟初始化）
        self._ncm_api = None
        self._qq_api = None
        
        # 国内代理配置
        self._proxy_url = ""
        self._proxy_key = ""
        self._ncm_cookie = ""
        self._qq_cookie = ""
        
        # 回调
        self.on_task_start: Optional[Callable] = None
        self.on_task_progress: Optional[Callable] = None
        self.on_task_complete: Optional[Callable] = None
        self.on_task_failed: Optional[Callable] = None
        
        logger.info(f"DownloadManager 初始化: 并发={max_concurrent}, 重试={max_retries}")
    
    def set_ncm_api(self, api):
        """设置网易云 API 实例"""
        self._ncm_api = api
    
    def set_qq_api(self, api):
        """设置 QQ 音乐 API 实例"""
        self._qq_api = api
    
    def set_proxy(self, proxy_url: str, proxy_key: str, ncm_cookie: str = "", qq_cookie: str = ""):
        """设置国内代理配置"""
        self._proxy_url = proxy_url
        self._proxy_key = proxy_key
        self._ncm_cookie = ncm_cookie
        self._qq_cookie = qq_cookie
        if proxy_url:
            logger.info(f"已配置国内代理: {proxy_url}")
    
    def add_task(self, task: DownloadTask) -> bool:
        """添加下载任务"""
        task.max_retries = self.max_retries
        success = self.queue.add(task)
        if success:
            logger.info(f"添加下载任务: {task.display_name} [{task.platform}]")
        return success
    
    def add_tasks(self, tasks: List[DownloadTask]) -> int:
        """批量添加任务"""
        count = 0
        for task in tasks:
            if self.add_task(task):
                count += 1
        return count
    
    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        task = self.queue.get_task(task_id)
        if task and task.status in (DownloadStatus.PENDING, DownloadStatus.RETRYING):
            task.status = DownloadStatus.CANCELLED
            logger.info(f"取消任务: {task.display_name}")
            return True
        return False
    
    async def start(self):
        """启动下载管理器"""
        if self._running:
            return
        
        self._running = True
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("下载管理器已启动")
    
    async def stop(self):
        """停止下载管理器"""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("下载管理器已停止")
    
    async def _worker_loop(self):
        """工作循环"""
        while self._running:
            try:
                # 获取下一个待处理任务
                task = self.queue.get_next()
                
                if task:
                    # 使用信号量控制并发
                    async with self._semaphore:
                        await self._process_task(task)
                else:
                    # 没有任务，等待一下
                    await asyncio.sleep(0.5)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(1)
    
    async def _process_task(self, task: DownloadTask):
        """处理单个下载任务"""
        task.status = DownloadStatus.DOWNLOADING
        task.started_at = time.time()
        
        logger.info(f"开始下载: {task.display_name} (重试 {task.retry_count}/{task.max_retries})")
        
        # 回调
        if self.on_task_start:
            try:
                await self._safe_callback(self.on_task_start, task)
            except:
                pass
        
        try:
            # 根据平台选择 API
            if task.platform == 'NCM':
                result = await self._download_ncm(task)
            elif task.platform == 'QQ':
                result = await self._download_qq(task)
            else:
                raise ValueError(f"未知平台: {task.platform}")
            
            if result:
                task.status = DownloadStatus.COMPLETED
                task.file_path = result
                task.completed_at = time.time()
                
                # 获取文件大小
                try:
                    result_path = Path(result)
                    if result_path.exists():
                        task.file_size = result_path.stat().st_size
                        logger.debug(f"获取文件大小: {task.file_size} bytes, 路径: {result}")
                    else:
                        logger.warning(f"文件不存在，无法获取大小: {result}")
                except Exception as e:
                    logger.warning(f"获取文件大小失败: {e}, 路径: {result}")
                
                logger.info(f"下载完成: {task.display_name} ({task.duration:.1f}s, {task.file_size} bytes)")
                
                # 记录统计
                self.stats.record_download(task)
                
                # 回调
                if self.on_task_complete:
                    await self._safe_callback(self.on_task_complete, task)
            else:
                raise Exception("下载返回空结果")
                
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"下载失败: {task.display_name} - {error_msg}")
            
            task.error_message = error_msg
            task.retry_count += 1
            
            # 判断是否重试
            if task.retry_count < task.max_retries:
                task.status = DownloadStatus.RETRYING
                logger.info(f"准备重试 ({task.retry_count}/{task.max_retries}): {task.display_name}")
                
                # 延迟后重新加入队列
                await asyncio.sleep(self.retry_delay * task.retry_count)  # 指数退避
                task.status = DownloadStatus.PENDING
            else:
                task.status = DownloadStatus.FAILED
                task.completed_at = time.time()
                
                logger.error(f"下载最终失败: {task.display_name}")
                
                # 记录统计
                self.stats.record_download(task)
                
                # 回调
                if self.on_task_failed:
                    await self._safe_callback(self.on_task_failed, task)
    
    async def _download_ncm(self, task: DownloadTask) -> Optional[str]:
        """网易云下载"""
        if not self._ncm_api:
            raise Exception("网易云 API 未初始化")
        
        song_info = {
            'title': task.title,
            'artist': task.artist,
            'source_id': task.song_id
        }
        
        # 在线程池中执行同步下载
        result = await asyncio.to_thread(
            self._ncm_api.download_song,
            task.song_id,
            song_info,
            task.output_dir,
            task.quality
        )
        
        # 如果本地下载失败，尝试通过国内代理
        if not result and self._proxy_url:
            logger.info(f"网易云本地下载失败，尝试国内代理: {task.title}")
            result = await asyncio.to_thread(
                self._download_via_proxy,
                'ncm',
                task.song_id,
                song_info,
                task.output_dir,
                task.quality
            )
        
        return result
    
    async def _download_qq(self, task: DownloadTask) -> Optional[str]:
        """QQ 音乐下载"""
        if not self._qq_api:
            raise Exception("QQ 音乐 API 未初始化")
        
        song_info = {
            'title': task.title,
            'artist': task.artist,
            'source_id': task.song_id
        }
        
        # 先尝试本地下载
        result = await asyncio.to_thread(
            self._qq_api.download_song,
            task.song_id,
            song_info,
            task.output_dir,
            task.quality
        )
        
        # 如果本地下载失败，尝试通过国内代理
        if not result and self._proxy_url:
            logger.info(f"QQ 音乐本地下载失败，尝试国内代理: {task.title}")
            result = await asyncio.to_thread(
                self._download_via_proxy,
                'qq',
                task.song_id,
                song_info,
                task.output_dir,
                task.quality
            )
        
        return result
    
    # 音质降级顺序（从高到低）
    QUALITY_FALLBACK_ORDER = ['hires', 'lossless', 'exhigh', 'higher', 'standard']
    
    def _download_via_proxy(self, platform: str, song_id: str, song_info: dict,
                            output_dir: str, quality: str) -> Optional[str]:
        """通过国内代理服务下载歌曲，支持自动音质降级重试"""
        import requests
        from pathlib import Path
        
        if not self._proxy_url or not self._proxy_key:
            return None
        
        # 构建尝试的音质列表（从请求的音质开始，逐级降级）
        try:
            start_idx = self.QUALITY_FALLBACK_ORDER.index(quality)
        except ValueError:
            start_idx = 0
        qualities_to_try = self.QUALITY_FALLBACK_ORDER[start_idx:]
        
        song_title = song_info.get('title', song_id)
        
        for try_quality in qualities_to_try:
            result = self._download_via_proxy_single(platform, song_id, song_info, 
                                                      output_dir, try_quality)
            if result:
                if try_quality != quality:
                    logger.info(f"代理下载成功（降级到 {try_quality}）: {song_title}")
                return result
            else:
                if try_quality != qualities_to_try[-1]:
                    logger.info(f"代理下载 {try_quality} 失败，尝试降级到更低音质...")
        
        logger.warning(f"代理下载失败（所有音质都不可用）: {song_title}")
        return None
    
    def _download_via_proxy_single(self, platform: str, song_id: str, song_info: dict,
                                    output_dir: str, quality: str) -> Optional[str]:
        """通过国内代理服务下载歌曲（单次尝试，不重试）"""
        import requests
        from pathlib import Path
        
        if not self._proxy_url or not self._proxy_key:
            return None
        
        try:
            # 构建代理请求 URL
            if platform == 'qq':
                url = f"{self._proxy_url}/qq/download/{song_id}"
                cookie_header = {'X-QQ-Cookie': self._qq_cookie or ''}
            else:
                url = f"{self._proxy_url}/ncm/download/{song_id}"
                cookie_header = {'X-NCM-Cookie': self._ncm_cookie or ''}
            
            headers = {
                'X-API-Key': self._proxy_key,
                **cookie_header
            }
            params = {'quality': quality}
            
            logger.info(f"通过国内代理下载 ({quality}): {song_info.get('title', song_id)}")
            
            resp = requests.get(url, headers=headers, params=params, timeout=120, stream=True)
            
            if resp.status_code != 200:
                # 尝试读取错误信息
                try:
                    error_body = resp.text[:500] if resp.text else ''
                except:
                    error_body = ''
                logger.warning(f"代理下载失败: HTTP {resp.status_code}, 音质={quality}, 响应={error_body}")
                return None
            
            # 从响应头获取文件类型
            file_type = resp.headers.get('X-File-Type', 'mp3')
            actual_quality = resp.headers.get('X-Quality', quality)
            
            # 生成文件名
            title = song_info.get('title', song_id)
            artist = song_info.get('artist', '')
            if artist:
                filename = f"{artist} - {title}.{file_type}"
            else:
                filename = f"{title}.{file_type}"
            
            # 清理非法字符
            filename = "".join(c for c in filename if c not in r'<>:"/\\|?*')
            output_path = Path(output_dir) / filename
            
            # 保存文件
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # 验证文件大小
            if output_path.stat().st_size < 10000:
                logger.warning(f"代理下载的文件过小（{output_path.stat().st_size} bytes），可能是错误响应")
                output_path.unlink()
                return None
            
            logger.info(f"代理下载成功: {filename} (音质: {actual_quality})")
            return str(output_path)
            
        except Exception as e:
            logger.error(f"代理下载异常: {e}")
            return None
    
    async def _safe_callback(self, callback: Callable, *args):
        """安全执行回调"""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)
        except Exception as e:
            logger.error(f"回调执行失败: {e}")
    
    def get_queue_status(self) -> dict:
        """获取队列状态"""
        tasks = self.queue.get_all()
        
        status_counts = {s.value: 0 for s in DownloadStatus}
        for task in tasks:
            status_counts[task.status.value] += 1
        
        return {
            'total': len(tasks),
            'pending': status_counts['pending'],
            'downloading': status_counts['downloading'],
            'completed': status_counts['completed'],
            'failed': status_counts['failed'],
            'retrying': status_counts['retrying'],
            'tasks': [task.to_dict() for task in tasks[-20:]]  # 最近20个
        }
    
    def get_stats(self) -> dict:
        """获取统计数据"""
        return {
            'today': self.stats.get_today_stats(),
            'weekly': self.stats.get_weekly_stats(),
            'queue': self.get_queue_status()
        }


# 全局下载管理器实例
_download_manager: Optional[DownloadManager] = None


def get_download_manager() -> Optional[DownloadManager]:
    """获取全局下载管理器"""
    return _download_manager


def init_download_manager(db_path: str, **kwargs) -> DownloadManager:
    """初始化全局下载管理器"""
    global _download_manager
    _download_manager = DownloadManager(db_path, **kwargs)
    return _download_manager
