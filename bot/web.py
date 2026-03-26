"""
FastAPI Web 管理界面
"""

import os
import json
import sqlite3
import secrets
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Request, HTTPException, Form, Query, Depends, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

# 版本号（统一从 config.py 读取）
try:
    from bot.config import APP_VERSION
except ImportError:
    APP_VERSION = "1.12.12"

# 路径配置
SCRIPT_DIR = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
MUSIC_TARGET_DIR = Path(os.environ.get('MUSIC_TARGET_DIR', SCRIPT_DIR / 'uploads'))
DATABASE_FILE = (DATA_DIR / 'bot.db').resolve()
LIBRARY_CACHE_FILE = DATA_DIR / 'library_cache.json'
TEMPLATES_DIR = Path(__file__).parent / 'templates'
STATIC_DIR = Path(__file__).parent / 'static'

# 确保目录存在
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Emby 配置
EMBY_URL = os.environ.get('EMBY_URL', '')

# Web 管理员配置
WEB_USERNAME = os.environ.get('WEB_USERNAME', 'admin')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '')  # 必须设置

# Session 存储 (使用 SQLite 持久化)
# get_db 定义
def get_db():
    try:
        conn = sqlite3.connect(str(DATABASE_FILE), check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        # 启用 WAL 模式提高并发性能
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception as e:
        print(f"[Web] [CRITICAL] Failed to connect to DB at {DATABASE_FILE}: {e}")
        raise

print(f"[Web] Using Database at: {DATABASE_FILE.absolute()}")


def get_ncm_cookie():
    """获取网易云 Cookie（优先从数据库读取）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_cookie',))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['value']
    except:
        pass
    return os.environ.get('NCM_COOKIE', '')


def get_qq_cookie():
    """获取 QQ音乐 Cookie（优先从数据库读取）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('qq_cookie',))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['value']
    except:
        pass
    return os.environ.get('QQ_COOKIE', '')


def init_session_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS web_sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        ''')
        conn.commit()

# 初始化数据库
try:
    init_session_db()
except Exception as e:
    print(f"初始化 Session 数据库失败: {e}")

def save_session(session_id, username, role, max_age=None):
    expires_at = None
    if max_age:
        # max_age 是秒数
        import time
        expires_at = datetime.fromtimestamp(time.time() + max_age)
    
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO web_sessions (session_id, username, role, expires_at) VALUES (?, ?, ?, ?)',
                    (session_id, username, role, expires_at))
        conn.commit()

def get_session(session_id):
    if not session_id: return None
    with get_db() as conn:
        cursor = conn.execute('SELECT * FROM web_sessions WHERE session_id = ?', (session_id,))
        row = cursor.fetchone()
        
        if not row: return None
        
        # 检查过期
        if row['expires_at']:
            try:
                expires = datetime.fromisoformat(str(row['expires_at'])) if isinstance(row['expires_at'], str) else row['expires_at']
                if datetime.now() > expires:
                    delete_session(session_id)
                    return None
            except:
                pass
                
        return {"username": row['username'], "role": row['role']}

def delete_session(session_id):
    with get_db() as conn:
        conn.execute('DELETE FROM web_sessions WHERE session_id = ?', (session_id,))
        conn.commit()

# 兼容旧代码引用 (虽然我们会替换掉使用它的地方)
sessions = {}

# Webhook 通知队列（用于实时推送到 Telegram）
_webhook_notifications = []
_webhook_bot_instance = None  # Bot 实例引用

def set_webhook_bot(bot):
    """设置 Bot 实例用于发送通知"""
    global _webhook_bot_instance
    _webhook_bot_instance = bot

def get_webhook_notifications():
    """获取并清空通知队列（供后台任务发送用）"""
    global _webhook_notifications
    notifications = _webhook_notifications.copy()
    _webhook_notifications = []
    return notifications

def peek_webhook_notifications():
    """查看通知队列但不清空（供 Web 页面显示用）"""
    global _webhook_notifications
    return _webhook_notifications.copy()

def add_webhook_notification(notification: dict):
    """添加通知到队列"""
    global _webhook_notifications
    _webhook_notifications.append(notification)
    # 限制队列大小，避免内存泄漏
    if len(_webhook_notifications) > 100:
        _webhook_notifications = _webhook_notifications[-50:]


# ============================================================
# WebSocket 实时进度支持
# ============================================================

from fastapi import WebSocket, WebSocketDisconnect

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # 移除已关闭的连接
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        for conn in disconnected:
            if conn in self.active_connections:
                self.active_connections.remove(conn)

manager = ConnectionManager()

async def broadcast_progress(data: dict):
    """
    广播进度更新
    data 格式: {'type': 'progress', 'song_id': '...', 'percent': 50, ...}
    """
    try:
        await manager.broadcast(data)
    except Exception as e:
        print(f"广播进度失败: {e}")




async def send_telegram_notification(item_type: str, title: str, artist: str, album: str, 
                                       audio_format: str = '', bitrate: str = ''):
    """直接发送 Telegram 入库通知"""
    import os
    import logging
    import httpx
    logger = logging.getLogger(__name__)
    
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_TOKEN')
    admin_id = os.environ.get('ADMIN_USER_ID')
    
    if not bot_token:
        print("[Webhook] 未配置 TELEGRAM_BOT_TOKEN")
        return False
    
    if not admin_id:
        print("[Webhook] 未配置 ADMIN_USER_ID")
        return False
    
    # 检查是否启用通知
    webhook_notify = os.environ.get('EMBY_WEBHOOK_NOTIFY', 'true').lower() == 'true'
    if not webhook_notify:
        print("[Webhook] Webhook 通知已禁用")
        return False
    
    try:
        item_type_lower = item_type.lower()
        if item_type_lower == 'audio':
            emoji = "🎵"
            type_name = "歌曲"
        elif item_type_lower == 'musicalbum':
            emoji = "💿"
            type_name = "专辑"
        elif item_type_lower == 'musicartist':
            emoji = "🎤"
            type_name = "艺术家"
        else:
            emoji = "📀"
            type_name = "媒体"
        
        msg = f"{emoji} *Emby 新{type_name}入库*\n\n"
        msg += f"🎵 名称: {title}\n"
        if artist:
            msg += f"🎤 艺术家: {artist}\n"
        if album:
            msg += f"💿 专辑: {album}\n"
        
        # 显示音频格式和码率
        if audio_format:
            # 判断是否无损
            lossless_formats = ['flac', 'ape', 'wav', 'aiff', 'alac', 'dsd', 'dsf', 'dff']
            format_lower = audio_format.lower()
            if format_lower in lossless_formats:
                format_emoji = "💎"  # 无损
                quality_label = "无损"
            elif format_lower == 'mp3':
                format_emoji = "🎧"
                quality_label = "有损"
            elif format_lower in ['m4a', 'aac', 'ogg']:
                format_emoji = "🎧"
                quality_label = "有损"
            else:
                format_emoji = "📁"
                quality_label = ""
            
            format_str = f"{format_emoji} 格式: {audio_format.upper()}"
            if quality_label:
                format_str += f" ({quality_label})"
            if bitrate:
                format_str += f" · {bitrate}"
            msg += format_str
        
        # 使用 HTTP API 直接发送消息
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={
                "chat_id": admin_id,
                "text": msg,
                "parse_mode": "Markdown"
            })
            
            if resp.status_code == 200:
                print(f"[Webhook] ✓ 已发送通知: {title} - {artist} [{audio_format}]")
                return True
            else:
                print(f"[Webhook] ✗ 发送失败: {resp.text}")
                return False
        
    except Exception as e:
        print(f"[Webhook] ✗ 发送异常: {e}")
        return False


def hash_password(password: str) -> str:
    """哈希密码"""
    return hashlib.sha256(password.encode()).hexdigest()


# get_db 已移动到文件顶部



def init_web_tables():
    """初始化 Web 相关的数据库表"""
    conn = get_db()
    cursor = conn.cursor()
    
    # 歌曲补全申请表
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
    
    # 用户权限表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_permissions (
            telegram_id TEXT PRIMARY KEY,
            role TEXT DEFAULT 'user',
            can_upload INTEGER DEFAULT 1,
            can_request INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 机器人设置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 自动创建管理员账户（如果不存在）
    if WEB_USERNAME and WEB_PASSWORD:
        import hashlib
        password_hash = hashlib.sha256(WEB_PASSWORD.encode()).hexdigest()
        
        # 检查管理员是否已存在
        cursor.execute('SELECT id FROM web_users WHERE username = ?', (WEB_USERNAME,))
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO web_users (username, password_hash, email, role, is_active)
                VALUES (?, ?, ?, 'admin', 1)
            ''', (WEB_USERNAME, password_hash, f'{WEB_USERNAME}@localhost'))
            print(f"[Init] 创建管理员账户: {WEB_USERNAME}")
    
    conn.commit()
    conn.close()


async def get_current_user(session_id: Optional[str] = Cookie(None)):
    """验证登录状态"""
    if not WEB_PASSWORD:
        # 未设置密码，跳过验证（开发模式）
        return {"username": "admin", "role": "admin"}
    
    if not session_id:
        return None
    
    # 从数据库获取 Session
    return get_session(session_id)


async def require_login(request: Request, session_id: Optional[str] = Cookie(None)):
    """要求登录的依赖"""
    user = await get_current_user(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    init_web_tables()
    
    # 启动时恢复文件整理器状态
    try:
        from bot.file_organizer import start_watcher, stop_watcher
        
        conn = get_db()
        cursor = conn.cursor()
        
        # 获取整理器配置
        cursor.execute("SELECT value FROM bot_settings WHERE key='organize_enabled'")
        row = cursor.fetchone()
        organize_enabled = row['value'] == 'true' if row else False
        
        if organize_enabled:
            cursor.execute("SELECT value FROM bot_settings WHERE key='organize_source_dir'")
            row = cursor.fetchone()
            source_dir = row['value'] if row else None
            
            cursor.execute("SELECT value FROM bot_settings WHERE key='organize_target_dir'")
            row = cursor.fetchone()
            target_dir = row['value'] if row else None
            
            cursor.execute("SELECT value FROM bot_settings WHERE key='organize_template'")
            row = cursor.fetchone()
            template = row['value'] if row else "{album_artist}/{album}"
            
            cursor.execute("SELECT value FROM bot_settings WHERE key='organize_on_conflict'")
            row = cursor.fetchone()
            on_conflict = row['value'] if row else "skip"
            
            if source_dir and target_dir:
                print(f"[Lifespan] 正在恢复文件整理器: {source_dir} -> {target_dir}")
                start_watcher(source_dir, target_dir, template, on_conflict)
        
        conn.close()
            
    except Exception as e:
        print(f"[Lifespan] 恢复文件整理器失败: {e}")
    
    yield
    
    # 关闭时停止整理器
    try:
        from bot.file_organizer import stop_watcher
        stop_watcher()
    except:
        pass


app = FastAPI(
    title="TGmusicbot 管理界面",
    description="Telegram 音乐机器人管理",
    version=APP_VERSION,
    lifespan=lifespan
)


# 挂载静态文件
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# 模板引擎
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ============================================================
# 数据模型
# ============================================================

class StatsResponse(BaseModel):
    library_songs: int = 0
    users: int = 0
    playlists: int = 0
    songs_synced: int = 0
    uploads: int = 0
    upload_size_mb: float = 0
    pending_requests: int = 0


class PlaylistRecord(BaseModel):
    id: int
    telegram_id: str
    playlist_name: str
    platform: str
    total_songs: int
    matched_songs: int
    match_rate: float
    created_at: str


class UploadRecord(BaseModel):
    id: int
    telegram_id: str
    original_name: str
    saved_name: str
    file_size_mb: float
    created_at: str


class SongRequest(BaseModel):
    id: int
    telegram_id: str
    song_name: str
    artist: Optional[str]
    album: Optional[str]
    source_url: Optional[str]
    status: str
    admin_note: Optional[str]
    created_at: str
    processed_at: Optional[str]


class UserBinding(BaseModel):
    telegram_id: str
    emby_username: str
    created_at: str



# 启动时间（用于计算 uptime）
import time as _time_module
_app_start_time = _time_module.time()


@app.get("/health")
async def health_check():
    """健康检查接口 - 用于监控服务状态"""
    import time
    
    status = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - _app_start_time),
    }
    
    # 检查 Emby 连接
    try:
        if EMBY_URL:
            status["emby"] = {"url": EMBY_URL, "connected": True}
        else:
            status["emby"] = {"connected": False, "reason": "URL not configured"}
    except:
        status["emby"] = {"connected": False}
    
    # 检查数据库
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]
        status["database"] = {"connected": True, "users": user_count}
    except Exception as e:
        status["database"] = {"connected": False, "error": str(e)}
    
    # 下载队列状态
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM download_queue WHERE status = 'pending'")
        row = cursor.fetchone()
        pending = row[0] if row else 0
        cursor.execute("SELECT COUNT(*) FROM download_queue WHERE status = 'downloading'")
        row = cursor.fetchone()
        downloading = row[0] if row else 0
        status["download_queue"] = {"pending": pending, "downloading": downloading}
    except:
        status["download_queue"] = {"pending": 0, "downloading": 0}
    
    return JSONResponse(status)


# ============================================================
# API 路由
# ============================================================


@app.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # 保持连接活跃，也可以接收客户端命令（暂时不需要）
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats():
    """获取统计数据"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 用户数
        cursor.execute('SELECT COUNT(*) as cnt FROM user_bindings')
        users = cursor.fetchone()['cnt']
        
        # 歌单同步
        cursor.execute('SELECT COUNT(*) as cnt, COALESCE(SUM(matched_songs), 0) as matched FROM playlist_records')
        row = cursor.fetchone()
        playlists = row['cnt']
        songs_synced = row['matched']
        
        # 上传记录
        cursor.execute('SELECT COUNT(*) as cnt, COALESCE(SUM(file_size), 0) as size FROM upload_records')
        row = cursor.fetchone()
        uploads = row['cnt']
        upload_size = row['size'] / (1024 * 1024) if row['size'] else 0
        
        # 媒体库
        library_songs = 0
        if LIBRARY_CACHE_FILE.exists():
            with open(LIBRARY_CACHE_FILE, 'r') as f:
                library_songs = len(json.load(f))
        # 待审核申请
        pending_requests = 0
        try:
            cursor.execute('SELECT COUNT(*) as cnt FROM playlist_requests WHERE status = ?', ('pending',))
            pending_requests = cursor.fetchone()['cnt']
        except:
            pass
        
        conn.close()
        
        return StatsResponse(
            library_songs=library_songs,
            users=users,
            playlists=playlists,
            songs_synced=songs_synced,
            uploads=uploads,
            upload_size_mb=round(upload_size, 2),
            pending_requests=pending_requests
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/playlists", response_model=List[PlaylistRecord])
async def get_playlists(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    platform: Optional[str] = None
):
    """获取歌单同步记录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        offset = (page - 1) * per_page
        
        if platform:
            cursor.execute('''
                SELECT * FROM playlist_records 
                WHERE platform = ? 
                ORDER BY created_at DESC 
                LIMIT ? OFFSET ?
            ''', (platform, per_page, offset))
        else:
            cursor.execute('''
                SELECT * FROM playlist_records 
                ORDER BY created_at DESC 
                LIMIT ? OFFSET ?
            ''', (per_page, offset))
        
        rows = cursor.fetchall()
        conn.close()
        
        records = []
        for row in rows:
            match_rate = (row['matched_songs'] / row['total_songs'] * 100) if row['total_songs'] > 0 else 0
            records.append(PlaylistRecord(
                id=row['id'],
                telegram_id=row['telegram_id'],
                playlist_name=row['playlist_name'],
                platform=row['platform'],
                total_songs=row['total_songs'],
                matched_songs=row['matched_songs'],
                match_rate=round(match_rate, 1),
                created_at=row['created_at']
            ))
        
        return records
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/playlists/{record_id}")
async def delete_playlist_record(record_id: int):
    """删除歌单同步记录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM playlist_records WHERE id = ?', (record_id,))
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="记录不存在")
            
        conn.commit()
        conn.close()
        return {"status": "success", "message": "记录已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/uploads", response_model=List[UploadRecord])
async def get_uploads(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100)
):
    """获取上传记录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        offset = (page - 1) * per_page
        
        cursor.execute('''
            SELECT * FROM upload_records 
            ORDER BY created_at DESC 
            LIMIT ? OFFSET ?
        ''', (per_page, offset))
        
        rows = cursor.fetchall()
        conn.close()
        
        records = []
        for row in rows:
            size_mb = row['file_size'] / (1024 * 1024) if row['file_size'] else 0
            records.append(UploadRecord(
                id=row['id'],
                telegram_id=row['telegram_id'],
                original_name=row['original_name'],
                saved_name=row['saved_name'],
                file_size_mb=round(size_mb, 2),
                created_at=row['created_at']
            ))
        
        return records
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/users")
async def get_users(user: dict = Depends(require_login)):
    """获取所有用户列表（管理员）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 查询 web_users 表的完整信息
        cursor.execute('''
            SELECT id, username, email, role, emby_user_id, emby_username, 
                   telegram_id, points, expire_at, is_active, created_at
            FROM web_users 
            ORDER BY created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        
        users = []
        for row in rows:
            users.append({
                "id": row['id'],
                "username": row['username'],
                "email": row['email'],
                "role": row['role'],
                "emby_user_id": row['emby_user_id'],
                "emby_username": row['emby_username'],
                "telegram_id": row['telegram_id'],
                "points": row['points'] or 0,
                "expire_at": row['expire_at'],
                "is_active": bool(row['is_active']),
                "created_at": row['created_at']
            })
        
        return users
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(require_login)):
    """删除用户"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM web_users WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        return {"status": "ok", "message": f"用户已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/users/{user_id}/toggle_status")
async def toggle_user_status(user_id: int, user: dict = Depends(require_login)):
    """切换用户状态"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT is_active FROM web_users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        new_status = 0 if row['is_active'] else 1
        cursor.execute('UPDATE web_users SET is_active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        conn.close()
        return {"status": "ok", "is_active": bool(new_status)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 下载统计 API
# ============================================================

@app.get("/api/download-stats")
async def get_download_stats():
    """获取下载统计数据"""
    from datetime import datetime, timedelta
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 确保表存在（schema 与 main.py 一致）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                song_id TEXT,
                title TEXT,
                artist TEXT,
                platform TEXT,
                quality TEXT,
                status TEXT DEFAULT 'completed',
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                duration REAL DEFAULT 0,
                error_message TEXT,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 兼容旧表：添加可能缺失的列
        for col, col_def in [
            ('task_id', 'TEXT'), ('file_path', 'TEXT'), 
            ('duration', 'REAL DEFAULT 0'), ('user_id', 'TEXT')
        ]:
            try:
                cursor.execute(f'ALTER TABLE download_history ADD COLUMN {col} {col_def}')
            except:
                pass  # 列已存在
        conn.commit()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 今日统计
        cursor.execute('''
            SELECT 
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'completed' THEN file_size ELSE 0 END) as size
            FROM download_history 
            WHERE DATE(created_at) = DATE('now', 'localtime')
        ''')
        row = cursor.fetchone()
        today_stats = {
            'success': row['success'] or 0,
            'failed': row['failed'] or 0,
            'size': row['size'] or 0
        }
        
        # 平台分布（最近30天）
        cursor.execute('''
            SELECT platform, COUNT(*) as cnt 
            FROM download_history 
            WHERE status = 'completed' AND created_at > datetime('now', '-30 days')
            GROUP BY platform
        ''')
        platforms = {row['platform']: row['cnt'] for row in cursor.fetchall()}
        
        # 7天趋势
        weekly = []
        for i in range(6, -1, -1):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            cursor.execute('''
                SELECT 
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
                FROM download_history 
                WHERE DATE(created_at) = ?
            ''', (date,))
            row = cursor.fetchone()
            weekly.append({
                'date': date[-5:],  # MM-DD
                'success': row['success'] or 0,
                'failed': row['failed'] or 0
            })
        
        conn.close()
        
        return {
            'today': today_stats,
            'platforms': platforms,
            'weekly': weekly
        }
    except Exception as e:
        return {
            'today': {'success': 0, 'failed': 0, 'size': 0},
            'platforms': {},
            'weekly': []
        }


@app.get("/api/download-history")
async def get_download_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    platform: Optional[str] = None,
    status: Optional[str] = None
):
    """获取下载历史"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 构建查询
        where_clauses = []
        params = []
        
        if platform:
            where_clauses.append("platform = ?")
            params.append(platform)
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        # 总数
        cursor.execute(f'SELECT COUNT(*) as cnt FROM download_history WHERE {where_sql}', params)
        total = cursor.fetchone()['cnt']
        
        # 分页查询
        offset = (page - 1) * per_page
        cursor.execute(f'''
            SELECT * FROM download_history 
            WHERE {where_sql}
            ORDER BY created_at DESC 
            LIMIT ? OFFSET ?
        ''', params + [per_page, offset])
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return {'total': total, 'items': items}
    except Exception as e:
        return {'total': 0, 'items': []}


@app.get("/api/download-history/failed")
async def get_failed_downloads(
    days: int = Query(7, ge=1, le=30),
    user: dict = Depends(require_login)
):
    """获取最近 N 天失败的下载记录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, song_id, title, artist, platform, quality, error_message, created_at
            FROM download_history 
            WHERE status = 'failed' 
            AND created_at > datetime('now', ?)
            ORDER BY created_at DESC
        ''', (f'-{days} days',))
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return {'total': len(items), 'items': items}
    except Exception as e:
        return {'total': 0, 'items': [], 'error': str(e)}


@app.post("/api/download-history/retry")
async def retry_failed_downloads(
    request: Request,
    user: dict = Depends(require_login)
):
    """批量重试失败的下载"""
    try:
        data = await request.json()
        song_ids = data.get('song_ids', [])
        
        if not song_ids:
            return {'success': False, 'error': '未选择任何歌曲'}
        
        # 获取失败的下载记录
        conn = get_db()
        cursor = conn.cursor()
        
        placeholders = ','.join(['?' for _ in song_ids])
        cursor.execute(f'''
            SELECT DISTINCT song_id, title, artist, platform, quality
            FROM download_history 
            WHERE id IN ({placeholders}) AND status = 'failed'
        ''', song_ids)
        
        records = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        if not records:
            return {'success': False, 'error': '未找到失败的下载记录'}
        
        # 添加到重试队列（通过 Telegram Bot 的下载队列处理）
        # 这里保存到临时表，由 bot 定期检查并处理
        retry_count = 0
        conn = get_db()
        cursor = conn.cursor()
        
        # 确保重试表存在
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                platform TEXT,
                quality TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        for record in records:
            cursor.execute('''
                INSERT INTO download_retry_queue (song_id, title, artist, platform, quality)
                VALUES (?, ?, ?, ?, ?)
            ''', (record['song_id'], record['title'], record['artist'], 
                  record['platform'], record['quality']))
            retry_count += 1
        
        conn.commit()
        conn.close()
        
        return {
            'success': True, 
            'message': f'已添加 {retry_count} 首歌曲到重试队列',
            'count': retry_count
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.delete("/api/download-history/failed")
async def clear_failed_downloads(
    days: int = Query(7, ge=1, le=30),
    user: dict = Depends(require_login)
):
    """清空指定天数内的失败记录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM download_history 
            WHERE status = 'failed' 
            AND created_at > datetime('now', ?)
        ''', (f'-{days} days',))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        return {'success': True, 'deleted': deleted}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.get("/api/cookie-status")
async def get_cookie_status():
    """获取 Cookie 状态"""
    ncm_cookie = os.environ.get('NCM_COOKIE', '')
    qq_cookie = os.environ.get('QQ_COOKIE', '')
    
    # 从数据库获取（优先）
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_cookie',))
        row = cursor.fetchone()
        if row and row['value']:
            ncm_cookie = row['value']
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('qq_cookie',))
        row = cursor.fetchone()
        if row and row['value']:
            qq_cookie = row['value']
        conn.close()
    except:
        pass
    
    result = {'ncm': None, 'qq': None}
    
    # 检查网易云
    if ncm_cookie:
        try:
            from bot.ncm_downloader import NeteaseMusicAPI
            api = NeteaseMusicAPI(ncm_cookie)
            logged_in, info = api.check_login()
            result['ncm'] = {
                'is_valid': logged_in,
                'nickname': info.get('nickname', '') if logged_in else '',
                'is_vip': info.get('is_vip', False) if logged_in else False
            }
        except:
            result['ncm'] = {'is_valid': False}
    
    # 检查 QQ
    if qq_cookie:
        try:
            from bot.ncm_downloader import QQMusicAPI
            api = QQMusicAPI(qq_cookie)
            logged_in, info = api.check_login()
            result['qq'] = {
                'is_valid': logged_in,
                'nickname': info.get('nickname', '') if logged_in else '',
                'is_vip': info.get('is_vip', False) if logged_in else False
            }
        except:
            result['qq'] = {'is_valid': False}
    
    return result





@app.get("/api/scan-covers/dirs")
async def get_scan_dirs(path: str = Query(default="")):
    """目录浏览器 - 可点击进入子目录"""
    from pathlib import Path
    
    # 获取配置的根目录
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
    row = cursor.fetchone()
    download_dir = row['value'] if row and row['value'] else str(MUSIC_TARGET_DIR)    # 使用用户指定目录或默认目录
    if dir and dir.strip():
        music_dir = Path(dir.strip())
    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        music_dir = Path(row['value']) if row and row['value'] else MUSIC_TARGET_DIR
    
    if not music_dir.exists():
        return JSONResponse({"success": False, "message": "音乐目录不存在"})
    
    # 支持的音频格式
    audio_extensions = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape'}
    
    scanned = 0
    filled = 0
    errors = []
    
    # 遍历所有子目录
    for root, dirs, files in os.walk(str(music_dir)):
        root_path = Path(root)
        cover_path = root_path / "cover.jpg"
        
        # 如果已有封面，跳过
        if cover_path.exists():
            continue
        
        # 查找音频文件
        audio_files = [f for f in files if Path(f).suffix.lower() in audio_extensions]
        if not audio_files:
            continue
        
        scanned += 1
        
        # 尝试从音频文件提取封面
        for audio_file in audio_files:
            audio_path = root_path / audio_file
            try:
                # 先尝试提取内嵌封面，如果没有则在线搜索
                result = extract_or_search_cover(str(audio_path), str(root_path))
                if result:
                    filled += 1
                    break
            except Exception as e:
                errors.append(f"{audio_file}: {str(e)[:50]}")
    
    # 提供详细反馈
    msg = f"扫描 {scanned} 个文件夹，补全 {filled} 个封面"
    if errors:
        msg += f"（{len(errors)} 个错误）"
    
    return JSONResponse({
        "success": True,
        "message": msg,
        "scanned": scanned,
        "filled": filled,
        "errors": errors[:10]
    })


def safe_int(v, default=0):
    try:
        if v is None or v == '': return default
        # 处理可能的布尔字符串
        if str(v).lower() in ('true', 'on', 'yes'): return 1
        if str(v).lower() in ('false', 'off', 'no'): return 0
        return int(float(v))
    except:
        return default

@app.get("/api/config")
async def get_config():
    """获取配置信息 (恢复 v1.12.9 完整字段 + safe_int 改进)"""
    ncm_cookie = os.environ.get('NCM_COOKIE', '')
    qq_cookie = os.environ.get('QQ_COOKIE', '')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 确保表存在
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 获取数据库中的 NCM Cookie（优先）
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_cookie',))
        row = cursor.fetchone()
        if row and row['value']:
            ncm_cookie = row['value']
        
        # 获取数据库中的 QQ Cookie（优先）
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('qq_cookie',))
        row = cursor.fetchone()
        if row and row['value']:
            qq_cookie = row['value']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_quality',))
        row = cursor.fetchone()
        ncm_quality = row['value'] if row else os.environ.get('NCM_QUALITY', 'exhigh')
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('qq_quality',))
        row = cursor.fetchone()
        qq_quality = row['value'] if row else '320'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_download',))
        row = cursor.fetchone()
        auto_download = row['value'] == 'true' if row else os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_organize',))
        row = cursor.fetchone()
        auto_organize = row['value'] == 'true' if row else False
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        download_dir = row['value'] if row else str(MUSIC_TARGET_DIR)
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_dir',))
        row = cursor.fetchone()
        organize_dir = row['value'] if row else ''
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_target_dir',))
        row = cursor.fetchone()
        organize_target_dir = row['value'] if row else organize_dir
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_template',))
        row = cursor.fetchone()
        organize_template = row['value'] if row else '{album_artist}/{album}'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_on_conflict',))
        row = cursor.fetchone()
        organize_on_conflict = row['value'] if row else 'skip'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
        row = cursor.fetchone()
        emby_scan_interval = safe_int(row['value'] if row else '0', 0)
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('playlist_sync_interval',))
        row = cursor.fetchone()
        playlist_sync_interval = safe_int(row['value'] if row else '60', 60)
        
        conn.close()
    except Exception as e:
        print(f"[Web] get_config error: {e}")
        import traceback
        traceback.print_exc()
        ncm_quality = os.environ.get('NCM_QUALITY', 'exhigh')
        qq_quality = '320'
        auto_download = os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'
        auto_organize = False
        download_dir = str(MUSIC_TARGET_DIR)
        organize_dir = ''
        organize_target_dir = ''
        organize_template = '{album_artist}/{album}'
        organize_on_conflict = 'skip'
        emby_scan_interval = 0
        playlist_sync_interval = 60
        qq_cookie = ''
    
    # 网易云状态
    ncm_status = {
        'configured': bool(ncm_cookie),
        'logged_in': False,
        'nickname': '',
        'is_vip': False
    }
    if ncm_cookie:
        ncm_status['configured'] = True
    
    # QQ音乐状态
    qq_status = {
        'configured': bool(qq_cookie),
        'logged_in': False,
        'nickname': '',
        'is_vip': False
    }
    
    response = JSONResponse({
        "emby_url": EMBY_URL,
        "data_dir": str(DATA_DIR),
        "database": str(DATABASE_FILE),
        "cache_exists": LIBRARY_CACHE_FILE.exists(),
        "ncm_status": ncm_status,
        "qq_status": qq_status,
        "ncm_cookie": ncm_cookie,
        "qq_cookie": qq_cookie,
        "ncm_quality": ncm_quality,
        "qq_quality": qq_quality,
        "auto_download": auto_download,
        "auto_organize": auto_organize,
        "organize_monitor_enabled": auto_organize,
        "download_dir": download_dir,
        "organize_dir": organize_dir,
        "organize_target_dir": organize_target_dir,
        "organize_template": organize_template,
        "organize_on_conflict": organize_on_conflict,
        "emby_scan_interval": emby_scan_interval,
        "playlist_sync_interval": playlist_sync_interval
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/api/settings/save")
@app.post("/api/settings/ncm")
async def save_ncm_settings(
    ncm_quality: str = Form(...),
    qq_quality: str = Form('320'),
    auto_download: bool = Form(False),
    download_dir: str = Form(''),
    auto_organize: bool = Form(False),
    organize_dir: str = Form(''),
    organize_target_dir: str = Form(''),
    organize_template: str = Form('{album_artist}/{album}'),
    organize_on_conflict: str = Form('skip'),
    emby_scan_interval: Optional[str] = Form(None),
    playlist_sync_interval: Optional[str] = Form(None)
):
    """保存下载设置到数据库 (恢复 v1.12.9 稳定逻辑)"""
    try:
        # Debug logging
        print(f"[Web] Saving settings: ncm_quality={ncm_quality}, qq_quality={qq_quality}, auto_download={auto_download}")
        print(f"[Web] Auto Organize: {auto_organize}")
        print(f"[Web] organize_dir='{organize_dir}'")
        print(f"[Web] download_dir='{download_dir}'")
        print(f"[Web] organize_template='{organize_template}'")
        print(f"[Web] Scan Interval: {emby_scan_interval}, Sync: {playlist_sync_interval}")

        # Convert strings to int securely
        scan_interval_int = 0
        if emby_scan_interval and str(emby_scan_interval).strip().isdigit():
            scan_interval_int = int(emby_scan_interval)
            
        sync_interval_int = 60
        if playlist_sync_interval and str(playlist_sync_interval).strip().isdigit():
            sync_interval_int = int(playlist_sync_interval)

        conn = get_db()
        cursor = conn.cursor()
        
        # 创建设置表（如果不存在）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 保存设置
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('ncm_quality', ncm_quality))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('qq_quality', qq_quality))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('auto_download', 'true' if auto_download else 'false'))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('auto_organize', 'true' if auto_organize else 'false'))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('download_dir', download_dir))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_dir', organize_target_dir or organize_dir or '/music'))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_target_dir', organize_target_dir or organize_dir or '/music'))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_template', organize_template))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_on_conflict', organize_on_conflict))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('emby_scan_interval', str(scan_interval_int)))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('playlist_sync_interval', str(max(1, sync_interval_int))))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "设置已保存"}
    except Exception as e:
        print(f"[Web] [CRITICAL] Save failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug/db")
async def debug_db():
    """Debug endpoint to check database contents"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bot_settings")
        rows = cursor.fetchall()
        data = {row['key']: row['value'] for row in rows}
        conn.close()
        return {
            "status": "ok",
            "db_path": str(DATABASE_FILE.absolute()),
            "db_exists": DATABASE_FILE.exists(),
            "settings_count": len(data),
            "settings": data
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/email/config")
async def get_email_config():
    """获取邮件配置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'smtp_%'")
        rows = cursor.fetchall()
        conn.close()
        return {row['key']: row['value'] for row in rows}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/email/config")
async def save_email_config(request: Request):
    """保存邮件配置"""
    try:
        data = await request.json()
        conn = get_db()
        cursor = conn.cursor()
        fields = ['smtp_server', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from']
        for field in fields:
            if field in data:
                cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', (field, str(data[field])))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 网易云音乐 API
# ============================================================

@app.get("/api/ncm/status")
async def get_ncm_status():
    """获取网易云登录状态"""
    try:
        from bot.main import get_ncm_cookie
        cookie = get_ncm_cookie()
        if not cookie:
            return {"logged_in": False, "configured": False}
        
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(cookie)
        logged_in, info = api.check_login()
        return {
            "logged_in": logged_in,
            "configured": True,
            "nickname": info.get('nickname', ''),
            "is_vip": info.get('is_vip', False)
        }
    except Exception as e:
        return {"logged_in": False, "configured": True, "error": str(e)}


@app.post("/api/ncm/check")
async def check_ncm_cookie(cookie: str = Form(...)):
    """检查网易云 Cookie"""
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(cookie)
        logged_in, info = api.check_login()
        if logged_in:
            return {"status": "ok", "logged_in": True, "nickname": info.get('nickname', '')}
        return {"status": "error", "message": "Cookie 无效"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/ncm/cookie/save")
async def save_ncm_cookie(cookie: str = Form(...)):
    """保存网易云 Cookie"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ('ncm_cookie', cookie))
        conn.commit()
        conn.close()
        os.environ['NCM_COOKIE'] = cookie
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/ncm/logout")
async def ncm_logout():
    """退出网易云登录（清除 Cookie）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 删除保存的 Cookie
        cursor.execute('DELETE FROM bot_settings WHERE key = ?', ('ncm_cookie',))
        conn.commit()
        conn.close()
        
        # 清除环境变量
        if 'NCM_COOKIE' in os.environ:
            del os.environ['NCM_COOKIE']
        
        return {"status": "ok", "message": "已退出登录"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/ncm/refresh")
async def ncm_refresh():
    """刷新网易云 Cookie"""
    try:
        from bot.main import get_ncm_cookie
        cookie = get_ncm_cookie()
        if not cookie: return {"status": "error", "message": "未配置 Cookie"}
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(cookie)
        success, data = api.refresh_login()
        if success:
            new_cookie = data.get('cookie', '')
            if new_cookie:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ('ncm_cookie', new_cookie))
                conn.commit()
                conn.close()
                os.environ['NCM_COOKIE'] = new_cookie
            return {"status": "ok", "nickname": data.get('nickname', '')}
        return {"status": "error", "message": data.get('message', '刷新失败')}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# QQ音乐 API
# ============================================================

@app.get("/api/qq/status")
async def get_qq_status():
    """获取 QQ 音乐登录状态"""
    try:
        from bot.main import get_qq_cookie
        cookie = get_qq_cookie()
        if not cookie:
            return {"logged_in": False, "configured": False}
        
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(cookie)
        logged_in, info = api.check_login()
        return {
            "logged_in": logged_in,
            "configured": True,
            "nickname": info.get('nickname', ''),
            "is_vip": info.get('is_vip', False),
            "vip_name": info.get('vip_name', '') or ("VIP会员" if info.get('is_vip') else "普通用户"),
            "vip_uncertain": info.get('vip_uncertain', False)
        }
    except Exception as e:
        return {"logged_in": False, "configured": True, "error": str(e)}


@app.post("/api/qq/check")
async def check_qq_cookie(cookie: str = Form(...)):
    """检查 QQ 音乐 Cookie"""
    try:
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(cookie)
        logged_in, info = api.check_login()
        if logged_in:
            return {"status": "ok", "logged_in": True, "nickname": info.get('nickname', '')}
        return {"status": "error", "message": "Cookie 无效"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/qq/save")
async def save_qq_cookie(cookie: str = Form('')):
    """保存 QQ 音乐 Cookie"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ('qq_cookie', cookie))
        conn.commit()
        conn.close()
        os.environ['QQ_COOKIE'] = cookie
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/qq/refresh")
async def qq_refresh_cookie():
    """刷新 QQ 音乐 Cookie"""
    try:
        from bot.main import get_qq_cookie
        cookie = get_qq_cookie()
        if not cookie: return {"status": "error", "message": "未配置 Cookie"}
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(cookie)
        success, data = api.refresh_cookie()
        if success:
            new_musickey = data.get('musickey', '')
            if new_musickey:
                import re
                new_cookie = cookie
                if 'qqmusic_key=' in new_cookie:
                    new_cookie = re.sub(r'qqmusic_key=[^;]*', f'qqmusic_key={new_musickey}', new_cookie)
                if 'qm_keyst=' in new_cookie:
                    new_cookie = re.sub(r'qm_keyst=[^;]*', f'qm_keyst={new_musickey}', new_cookie)
                
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ('qq_cookie', new_cookie))
                conn.commit()
                conn.close()
                os.environ['QQ_COOKIE'] = new_cookie
            return {"status": "ok", "message": "刷新成功"}
        return {"status": "error", "message": "刷新失败"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# 登录认证 API
# ============================================================

@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...), remember_me: bool = Form(False)):
    """登录"""
    if not WEB_PASSWORD:
        return {"status": "error", "message": "未配置 WEB_PASSWORD，请在环境变量中设置"}
    
    if username == WEB_USERNAME and password == WEB_PASSWORD:
        session_id = secrets.token_hex(32)
        
        # 记住我：30天；否则：会话结束即过期 (max_age=None)
        max_age = 86400 * 30 if remember_me else None
        
        # 保存 Session 到数据库
        save_session(session_id, username, "admin", max_age)
        
        response.set_cookie(key="session_id", value=session_id, httponly=True, max_age=max_age)
        return {"status": "ok", "message": "登录成功"}
    else:
        raise HTTPException(status_code=401, detail="用户名或密码错误")


@app.post("/api/logout")
async def logout(response: Response, session_id: Optional[str] = Cookie(None), user_session_id: Optional[str] = Cookie(None)):
    """登出（同时清除管理员和用户 session）"""
    if session_id:
        delete_session(session_id)
    if user_session_id:
        delete_session(user_session_id)
    response.delete_cookie("session_id")
    response.delete_cookie("user_session_id")
    return {"status": "ok", "message": "已登出"}

@app.get("/api/auth/status")
async def auth_status(session_id: Optional[str] = Cookie(None)):
    """检查登录状态"""
    user = await get_current_user(session_id)
    if user:
        return {"logged_in": True, "username": user["username"], "role": user["role"]}
    return {"logged_in": False, "need_password": bool(WEB_PASSWORD)}


# ============================================================
# 歌单申请管理 API
# ============================================================

@app.get("/api/requests")
async def get_playlist_requests(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_login)
):
    """获取歌单同步申请列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 创建表（如果不存在）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS playlist_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT NOT NULL,
                playlist_url TEXT NOT NULL,
                playlist_name TEXT,
                platform TEXT,
                song_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                admin_note TEXT,
                download_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        ''')
        conn.commit()
        
        offset = (page - 1) * per_page
        
        if status:
            cursor.execute('''
                SELECT * FROM playlist_requests 
                WHERE status = ? 
                ORDER BY created_at DESC 
                LIMIT ? OFFSET ?
            ''', (status, per_page, offset))
        else:
            cursor.execute('''
                SELECT * FROM playlist_requests 
                ORDER BY created_at DESC 
                LIMIT ? OFFSET ?
            ''', (per_page, offset))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/requests/{request_id}/approve")
async def approve_request(
    request_id: int,
    note: str = Form(""),
    user: dict = Depends(require_login)
):
    """批准歌单申请（Web 端仅更新状态，实际下载通过 Telegram Bot 完成）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE playlist_requests 
            SET status = ?, admin_note = ?, processed_at = ? 
            WHERE id = ?
        ''', ('approved', note, datetime.now().isoformat(), request_id))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "申请已批准，请在 Telegram 中处理下载"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/requests/{request_id}/reject")
async def reject_request(
    request_id: int,
    note: str = Form(""),
    user: dict = Depends(require_login)
):
    """拒绝歌单申请"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE playlist_requests 
            SET status = ?, admin_note = ?, processed_at = ? 
            WHERE id = ?
        ''', ('rejected', note, datetime.now().isoformat(), request_id))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "申请已拒绝"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/requests/{request_id}")
async def delete_request(request_id: int, user: dict = Depends(require_login)):
    """删除歌单申请"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM playlist_requests WHERE id = ?', (request_id,))
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "申请已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 用户权限管理 API
# ============================================================

@app.get("/api/permissions")
async def get_user_permissions(user: dict = Depends(require_login)):
    """获取用户权限列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_permissions ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/permissions/{telegram_id}")
async def update_user_permission(
    telegram_id: str,
    can_upload: int = Form(1),
    can_request: int = Form(1),
    user: dict = Depends(require_login)
):
    """更新用户权限"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO user_permissions (telegram_id, can_upload, can_request, created_at)
            VALUES (?, ?, ?, ?)
        ''', (telegram_id, can_upload, can_request, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "权限已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# HTML 页面
# ============================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """注册页"""
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session_id: Optional[str] = Cookie(None)):
    """首页仪表盘"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request, "is_admin": True})


@app.get("/playlists", response_class=HTMLResponse)
async def playlists_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """歌单记录页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("playlists.html", {"request": request, "is_admin": True})


@app.get("/uploads", response_class=HTMLResponse)
async def uploads_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """上传记录页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("uploads.html", {"request": request, "is_admin": True})


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """用户管理页 - 重定向到统一的 /members 页面"""
    return RedirectResponse(url="/members", status_code=302)


@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """会员管理页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("members.html", {"request": request, "is_admin": True})


@app.get("/cards", response_class=HTMLResponse)
async def cards_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """卡密管理页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("cards.html", {"request": request, "is_admin": True})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """设置页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request, "is_admin": True, "version": APP_VERSION})



@app.get("/metadata", response_class=HTMLResponse)
async def metadata_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """音乐元数据管理器页面"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("metadata.html", {"request": request, "is_admin": True})


@app.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """歌曲申请管理页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("requests.html", {"request": request, "is_admin": True})


@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """下载统计页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("downloads.html", {"request": request, "is_admin": True})


# ============================================================
# 文件整理 API
# ============================================================

@app.get("/api/organizer/templates")
async def get_organize_templates():
    """获取可用的目录模板"""
    from bot.file_organizer import PRESET_TEMPLATES, TEMPLATE_VARIABLES
    return {
        "presets": PRESET_TEMPLATES,
        "variables": TEMPLATE_VARIABLES
    }


@app.get("/api/organizer/settings")
async def get_organizer_settings():
    """获取整理器设置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        settings = {}
        key_map = {
            'organize_source_dir': 'source_dir',
            'organize_target_dir': 'target_dir', 
            'organize_template': 'template',
            'organize_on_conflict': 'on_conflict',
            'organize_enabled': 'enabled'
        }
        
        for db_key, api_key in key_map.items():
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (db_key,))
            row = cursor.fetchone()
            settings[api_key] = row['value'] if row else ''
        
        conn.close()
        
        # 默认值
        if not settings.get('template'):
            settings['template'] = '{album_artist}/{album}'
        if not settings.get('on_conflict'):
            settings['on_conflict'] = 'skip'
        if not settings.get('enabled'):
            settings['enabled'] = 'false'
        
        return settings
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/organizer/settings")
async def save_organizer_settings(
    source_dir: str = Form(''),
    target_dir: str = Form(''),
    template: str = Form('{album_artist}/{album}'),
    on_conflict: str = Form('skip'),
    enabled: str = Form('false')
):
    """保存整理器设置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_source_dir', source_dir))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_target_dir', target_dir))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_template', template))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_on_conflict', on_conflict))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('organize_enabled', enabled))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "整理设置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/organizer/status")
async def get_organizer_status():
    """获取整理器状态"""
    try:
        # 获取保存的设置
        settings = await get_organizer_settings()
        enabled = settings.get('enabled', 'false') == 'true'
        
        try:
            from bot.file_organizer import get_watcher
            watcher = get_watcher()
            
            if watcher and watcher._running:
                return {
                    "running": True,
                    "enabled": True,
                    "source_dir": settings.get('source_dir', ''),
                    "target_dir": settings.get('target_dir', ''),
                    "template": settings.get('template', '{album_artist}/{album}'),
                    "on_conflict": settings.get('on_conflict', 'skip'),
                    "stats": watcher.get_stats() if hasattr(watcher, 'get_stats') else None
                }
        except ImportError:
            pass
        
        return {
            "running": False,
            "enabled": enabled,
            "source_dir": settings.get('source_dir', ''),
            "target_dir": settings.get('target_dir', ''),
            "template": settings.get('template', '{album_artist}/{album}'),
            "on_conflict": settings.get('on_conflict', 'skip'),
            "stats": None
        }
    except Exception as e:
        return {"running": False, "enabled": False, "error": str(e)}


@app.post("/api/organizer/start")
async def start_organizer(
    source_dir: str = Form(None),
    target_dir: str = Form(None),
    template: str = Form(None)
):
    """启动整理监控"""
    try:
        # 如果传入了参数，先保存
        if source_dir and target_dir:
            await save_organizer_settings(
                source_dir=source_dir,
                target_dir=target_dir,
                template=template or '{album_artist}/{album}'
            )
        
        # 获取设置
        settings = await get_organizer_settings()
        
        if not settings.get('source_dir') or not settings.get('target_dir'):
            return {"status": "error", "message": "请先配置监控目录和目标目录"}
        
        from bot.file_organizer import start_watcher
        watcher = start_watcher(
            settings['source_dir'],
            settings['target_dir'],
            settings.get('template', '{album_artist}/{album}'),
            settings.get('on_conflict', 'skip')
        )
        
        return {"status": "ok", "message": "监控已启动"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/organizer/stop")
async def stop_organizer():
    """停止整理监控"""
    try:
        from bot.file_organizer import stop_watcher
        stop_watcher()
        return {"status": "ok", "message": "监控已停止"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# Emby Webhook 实时联动
# ============================================================


@app.post("/webhook/emby")
async def emby_webhook(request: Request):
    """
    接收 Emby Webhooks 插件的事件
    
    Emby 设置方法：
    1. 安装 Webhooks 插件（设置 → 插件 → 目录 → Webhooks）
    2. 配置 Webhook URL: http://your-bot-server:8080/webhook/emby
    3. 选择事件类型: library.new, item.added 等
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        # 尝试解析 JSON
        try:
            data = await request.json()
        except:
            # 可能是 form data
            form = await request.form()
            data = dict(form)
        
        event_type = data.get('Event') or data.get('event') or data.get('NotificationType', '')
        
        # 详细调试日志
        print(f"[Webhook] 收到事件: {event_type}")
        print(f"[Webhook] 完整数据: {data}")
        
        # 处理不同事件类型
        # Emby Webhooks 插件的"已添加新媒体"事件可能是多种格式
        if event_type.lower() in ['library.new', 'item.added', 'itemadded', 'media.new', 'library.itemadded']:
            await handle_library_new_item(data)
        elif event_type.lower() in ['library.deleted', 'item.removed', 'itemremoved']:
            await handle_library_item_removed(data)
        elif event_type.lower() in ['playback.start', 'playbackstart']:
            # 播放开始事件
            pass
        elif event_type.lower() in ['playback.stop', 'playbackstop']:
            # 播放完成事件 - 记录到统计
            await handle_playback_stop(data)
        
        return {"status": "ok"}
    except Exception as e:
        print(f"[Webhook] 处理失败: {e}")
        logger.error(f"处理 Emby Webhook 失败: {e}")
        return {"status": "error", "message": str(e)}




async def handle_playback_stop(data: dict):
    """处理播放完成事件 - 记录到统计数据库"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        from bot.services.playback_stats import get_playback_stats
        
        # 提取信息
        item = data.get('Item') or data.get('item') or {}
        user_info = data.get('User') or data.get('user') or {}
        
        item_type = (item.get('Type') or item.get('type') or '').lower()
        
        # 只记录音频播放
        if item_type != 'audio':
            return
        
        item_id = str(item.get('Id') or item.get('id') or '')
        title = item.get('Name') or item.get('name') or ''
        
        # 艺术家
        artist = ''
        if item.get('Artists'):
            artist = item['Artists'][0] if isinstance(item['Artists'], list) else item['Artists']
        elif item.get('AlbumArtist'):
            artist = item['AlbumArtist']
        elif item.get('ArtistItems'):
            artist = item['ArtistItems'][0].get('Name', '') if item['ArtistItems'] else ''
        
        album = item.get('Album') or ''
        album_id = str(item.get('AlbumId') or item.get('ParentId') or '')
        
        # 封面 URL
        cover_url = ''
        if album_id:
            cover_url = f"/Items/{album_id}/Images/Primary"
        
        # 用户信息
        user_id = str(user_info.get('Id') or user_info.get('id') or '')
        user_name = user_info.get('Name') or user_info.get('name') or ''
        
        # 查找关联的 Telegram ID
        telegram_id = ''
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT telegram_id FROM users WHERE emby_user_id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                telegram_id = row['telegram_id'] if isinstance(row, dict) else row[0]
        except:
            pass
        
        # 记录播放
        stats = get_playback_stats(str(DATABASE_FILE))
        stats.record_playback(
            user_id=user_id,
            telegram_id=telegram_id,
            item_id=item_id,
            title=title,
            artist=artist,
            album=album,
            album_id=album_id,
            cover_url=cover_url
        )
        
        logger.info(f"记录播放: {title} - {artist} (用户: {user_name})")
        
    except Exception as e:
        logger.error(f"处理播放完成事件失败: {e}")


async def handle_library_new_item(data: dict):
    """处理新媒体入库事件"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        # 提取媒体信息
        item = data.get('Item') or data.get('item') or {}
        item_type = item.get('Type') or item.get('type') or ''
        
        print(f"[Webhook] 处理入库事件, item_type={item_type}")
        
        # 只处理音频相关类型
        accepted_types = ['audio', 'musicalbum', 'musicartist', 'song', 'music', 'episode']
        if item_type.lower() not in accepted_types:
            print(f"[Webhook] 跳过非音频类型: {item_type}")
            return
        
        item_name = item.get('Name') or item.get('name') or '未知'
        artist = ''
        if item.get('Artists'):
            artist = item['Artists'][0] if isinstance(item['Artists'], list) else item['Artists']
        elif item.get('AlbumArtist'):
            artist = item['AlbumArtist']
        album = item.get('Album') or ''
        
        # 提取音频格式信息
        audio_format = ''
        bitrate = ''
        
        # 尝试从 MediaSources 获取格式信息
        media_sources = item.get('MediaSources') or []
        if media_sources and len(media_sources) > 0:
            source = media_sources[0]
            container = source.get('Container', '')
            if container:
                audio_format = container
            
            # 获取码率
            source_bitrate = source.get('Bitrate', 0)
            if source_bitrate:
                # 转换为 kbps
                kbps = source_bitrate // 1000
                if kbps >= 1000:
                    bitrate = f"{kbps // 1000}.{(kbps % 1000) // 100}Mbps"
                else:
                    bitrate = f"{kbps}kbps"
            
            # 从音频流获取更详细信息
            media_streams = source.get('MediaStreams') or []
            for stream in media_streams:
                if stream.get('Type') == 'Audio':
                    codec = stream.get('Codec', '')
                    if codec and not audio_format:
                        audio_format = codec
                    stream_bitrate = stream.get('BitRate', 0)
                    if stream_bitrate and not bitrate:
                        kbps = stream_bitrate // 1000
                        bitrate = f"{kbps}kbps"
                    # 获取采样率和位深
                    sample_rate = stream.get('SampleRate', 0)
                    bit_depth = stream.get('BitDepth', 0)
                    if sample_rate and bit_depth:
                        bitrate = f"{sample_rate//1000}kHz/{bit_depth}bit"
                    break
        
        # 从 Path 中提取格式（备选）
        if not audio_format:
            path = item.get('Path') or ''
            if path:
                import os
                ext = os.path.splitext(path)[1].lower().lstrip('.')
                if ext:
                    audio_format = ext
        
        print(f"[Webhook] 新音乐: {item_name} - {artist} ({album}) [{audio_format} {bitrate}]")
        
        # 更新媒体库缓存
        await update_library_cache_item(item)
        
        # 检查是否匹配待同步歌单
        await check_pending_playlist_match(item_name, artist)
        
        # 添加通知到队列（供 Web 页面显示）
        add_webhook_notification({
            'type': 'library_new',
            'item_type': item_type,
            'title': item_name,
            'artist': artist,
            'album': album,
            'audio_format': audio_format,
            'bitrate': bitrate,
            'time': datetime.now().isoformat()
        })
        
        # 直接发送 Telegram 通知
        print(f"[Webhook] 准备发送 Telegram 通知...")
        await send_telegram_notification(item_type, item_name, artist, album, audio_format, bitrate)
        print(f"[Webhook] Telegram 通知已发送")
        
    except Exception as e:
        print(f"[Webhook] 处理入库失败: {e}")
        logger.error(f"处理新媒体事件失败: {e}")


async def handle_library_item_removed(data: dict):
    """处理媒体删除事件"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        item = data.get('Item') or data.get('item') or {}
        item_id = item.get('Id') or item.get('id')
        
        if item_id:
            # 从缓存中移除
            await remove_from_library_cache(item_id)
            logger.info(f"媒体已从缓存移除: {item_id}")
            
    except Exception as e:
        logger.error(f"处理媒体删除事件失败: {e}")


async def update_library_cache_item(item: dict):
    """更新媒体库缓存中的单个项目"""
    try:
        if not LIBRARY_CACHE_FILE.exists():
            return
        
        with open(LIBRARY_CACHE_FILE, 'r') as f:
            cache = json.load(f)
        
        # 检查是否已存在
        item_id = item.get('Id') or item.get('id')
        existing_ids = {s.get('Id') for s in cache}
        
        if item_id not in existing_ids:
            # 添加新项目
            cache.append({
                'Id': item_id,
                'Name': item.get('Name', ''),
                'Artists': item.get('Artists', []),
                'Album': item.get('Album', ''),
                'AlbumArtist': item.get('AlbumArtist', ''),
                'Type': item.get('Type', 'Audio')
            })
            
            with open(LIBRARY_CACHE_FILE, 'w') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
                
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"更新缓存失败: {e}")


async def remove_from_library_cache(item_id: str):
    """从媒体库缓存中移除项目"""
    try:
        if not LIBRARY_CACHE_FILE.exists():
            return
        
        with open(LIBRARY_CACHE_FILE, 'r') as f:
            cache = json.load(f)
        
        cache = [s for s in cache if s.get('Id') != item_id]
        
        with open(LIBRARY_CACHE_FILE, 'w') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
            
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"从缓存移除失败: {e}")


async def check_pending_playlist_match(song_name: str, artist: str):
    """检查新入库歌曲是否匹配待同步歌单"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 获取所有活跃的定时同步歌单
        cursor.execute('''
            SELECT id, playlist_name, last_song_ids, telegram_id 
            FROM scheduled_playlists 
            WHERE is_active = 1
        ''')
        playlists = cursor.fetchall()
        
        if not playlists:
            conn.close()
            return
        
        # 这里可以做更复杂的匹配逻辑
        # 目前只记录日志，实际通知通过 Telegram Bot 完成
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"检查歌曲 '{song_name}' 是否匹配 {len(playlists)} 个订阅歌单")
        
        conn.close()
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"检查歌单匹配失败: {e}")


@app.get("/api/webhook/notifications")
async def api_get_webhook_notifications(user: dict = Depends(require_login)):
    """获取最近的 Webhook 通知（供前端显示，不清空队列）"""
    notifications = peek_webhook_notifications()
    return {"notifications": notifications}


@app.get("/api/webhook/status")
async def get_webhook_status(user: dict = Depends(require_login)):
    """获取 Webhook 配置状态"""
    return {
        "webhook_url": "/webhook/emby",
        "enabled": True,
        "events_supported": [
            "library.new / ItemAdded - 新媒体入库",
            "library.deleted / ItemRemoved - 媒体删除",
            "playback.start / PlaybackStart - 播放开始"
        ],
        "setup_instructions": [
            "1. 在 Emby 中安装 Webhooks 插件",
            "2. 设置 → 插件 → Webhooks",
            "3. 添加 Webhook URL: http://your-server:8080/webhook/emby",
            "4. 选择需要的事件类型"
        ]
    }


@app.post("/api/webhook/test")
async def test_webhook_notification(user: dict = Depends(require_login)):
    """测试 Webhook 通知（模拟一条入库消息）"""
    try:
        # 添加一条测试通知到队列（供 Web 页面显示）
        add_webhook_notification({
            'type': 'library_new',
            'item_type': 'audio',
            'title': '测试歌曲',
            'artist': '测试艺术家',
            'album': '测试专辑',
            'time': datetime.now().isoformat()
        })
        
        # 直接发送 Telegram 通知
        print("[Webhook] 测试: 准备发送 Telegram 通知...")
        success = await send_telegram_notification('audio', '测试歌曲', '测试艺术家', '测试专辑')
        
        if success:
            return {
                "success": True,
                "message": "测试通知已发送到 Telegram"
            }
        else:
            return {
                "success": False,
                "message": "发送失败，请检查日志"
            }
    except Exception as e:
        print(f"[Webhook] 测试失败: {e}")
        return {"success": False, "message": str(e)}


@app.post("/api/ranking/test/daily")
async def test_daily_ranking_push(user: dict = Depends(require_login)):
    """测试日榜推送"""
    try:
        from bot.services.playback_stats import get_playback_stats
        from bot.utils.ranking_image import generate_daily_ranking_image
        import sqlite3
        
        # 获取推送目标 - 优先从数据库读取，其次从环境变量
        target_chat_id = None
        try:
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'ranking_target_chat'")
                row = cursor.fetchone()
                if row and row[0]:
                    target_chat_id = row[0].strip()
        except:
            pass
        
        if not target_chat_id:
            target_chat_id = os.environ.get('LOG_CHANNEL_ID', '')
        
        if not target_chat_id:
            return {"success": False, "message": "未配置推送目标。请在设置页面配置 ranking_target_chat 或设置环境变量 LOG_CHANNEL_ID"}
        
        # 获取统计数据
        stats_svc = get_playback_stats()
        data = stats_svc.get_global_daily_stats()
        
        print(f"[TestDailyPush] Data: leaderboard={len(data.get('leaderboard', []))}, top_songs={len(data.get('top_songs', []))}")
        
        if not data or not data.get('leaderboard'):
            return {"success": False, "message": f"没有播放数据。请检查 Emby Playback Reporting 插件是否正常工作。"}
        
        # 生成图片
        img_bytes = generate_daily_ranking_image(data, emby_url=stats_svc.emby_url, emby_token=stats_svc.emby_token)
        
        if not img_bytes:
            return {"success": False, "message": "生成图片失败"}
        
        # 获取标题
        ranking_subtitle = "每日音乐热曲榜"
        try:
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'ranking_daily_subtitle'")
                row = cursor.fetchone()
                if row and row[0]:
                    ranking_subtitle = row[0]
        except:
            pass
        
        # 构建 caption
        caption_lines = [f"【{ranking_subtitle} 播放日榜】\n", "▎热门歌曲：\n"]
        top_songs = data.get('top_songs', [])[:10]
        for i, song in enumerate(top_songs):
            title = song.get('title', 'Unknown')
            artist = song.get('artist', 'Unknown')
            count = song.get('count', 0)
            caption_lines.append(f"{i+1}. {title}")
            if artist and artist != 'Unknown':
                caption_lines.append(f"歌手: {artist}")
            caption_lines.append(f"播放次数: {count}\n")
        caption_lines.append(f"\n#DayRanks  {data.get('date', '')}")
        caption = "\n".join(caption_lines)
        
        # 发送到 Telegram
        from bot.config import TELEGRAM_TOKEN as TELEGRAM_BOT_TOKEN
        import httpx
        
        async with httpx.AsyncClient() as client:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('daily_ranking.png', img_bytes, 'image/png')}
            form_data = {'chat_id': target_chat_id, 'caption': caption[:1024]}
            resp = await client.post(url, files=files, data=form_data, timeout=30)
            result = resp.json()
            
            if result.get('ok'):
                return {"success": True, "message": f"日榜已推送到 {target_chat_id}"}
            else:
                return {"success": False, "message": f"Telegram API 错误: {result.get('description', 'Unknown error')}"}
                
    except Exception as e:
        print(f"[TestDailyPush] 失败: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


@app.post("/api/ranking/test/weekly")
async def test_weekly_ranking_push(user: dict = Depends(require_login)):
    """测试周榜推送"""
    try:
        from bot.services.playback_stats import get_playback_stats
        from bot.utils.ranking_image import generate_daily_ranking_image
        import sqlite3
        
        # 获取推送目标 - 优先从数据库读取，其次从环境变量
        target_chat_id = None
        try:
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'ranking_target_chat'")
                row = cursor.fetchone()
                if row and row[0]:
                    target_chat_id = row[0].strip()
        except:
            pass
        
        if not target_chat_id:
            target_chat_id = os.environ.get('LOG_CHANNEL_ID', '')
        
        if not target_chat_id:
            return {"success": False, "message": "未配置推送目标"}
        
        # 获取统计数据
        stats_svc = get_playback_stats()
        data = stats_svc.get_global_weekly_stats()
        
        if not data or not data.get('leaderboard'):
            return {"success": False, "message": "没有播放数据"}
        
        # 生成图片 - 复用日榜图片生成器，传入周榜标题
        img_bytes = generate_daily_ranking_image(data, emby_url=stats_svc.emby_url, emby_token=stats_svc.emby_token, title="Weekly Music Charts")
        
        if not img_bytes:
            return {"success": False, "message": "生成图片失败"}
        
        # 获取标题
        ranking_title = "本周音乐热曲榜"
        try:
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'ranking_weekly_title'")
                row = cursor.fetchone()
                if row and row[0]:
                    ranking_title = row[0]
        except:
            pass
        
        # 构建 caption
        caption_lines = [f"【{ranking_title} 播放周榜】\n", "▎热门歌曲：\n"]
        top_songs = data.get('top_songs', [])[:10]
        for i, song in enumerate(top_songs):
            title = song.get('title', 'Unknown')
            artist = song.get('artist', 'Unknown')
            count = song.get('count', 0)
            caption_lines.append(f"{i+1}. {title}")
            if artist and artist != 'Unknown':
                caption_lines.append(f"歌手: {artist}")
            caption_lines.append(f"播放次数: {count}\n")
        caption_lines.append(f"\n#WeekRanks  {data.get('week_range', '')}")
        caption = "\n".join(caption_lines)
        
        # 发送到 Telegram
        from bot.config import TELEGRAM_TOKEN as TELEGRAM_BOT_TOKEN
        import httpx
        
        async with httpx.AsyncClient() as client:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('weekly_ranking.png', img_bytes, 'image/png')}
            form_data = {'chat_id': target_chat_id, 'caption': caption[:1024]}
            resp = await client.post(url, files=files, data=form_data, timeout=30)
            result = resp.json()
            
            if result.get('ok'):
                return {"success": True, "message": f"周榜已推送到 {target_chat_id}"}
            else:
                return {"success": False, "message": f"Telegram API 错误: {result.get('description', 'Unknown error')}"}
                
    except Exception as e:
        print(f"[TestWeeklyPush] 失败: {e}")
        return {"success": False, "message": str(e)}


# ============================================================
# 歌单订阅管理 API
# ============================================================

@app.get("/api/subscriptions")
async def get_subscriptions(user: dict = Depends(require_login)):
    """获取所有歌单订阅"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, telegram_id, playlist_url, playlist_name, platform, 
                   last_song_ids, last_sync_at, is_active, created_at, is_public
            FROM scheduled_playlists 
            ORDER BY created_at DESC
        ''')
        rows = cursor.fetchall()
        
        # 获取同步间隔设置（分钟）
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('playlist_sync_interval',))
        interval_row = cursor.fetchone()
        sync_interval = int(interval_row['value']) if interval_row else 60
        
        conn.close()
        
        subscriptions = []
        for row in rows:
            last_song_ids = row[5] or '[]'
            try:
                song_count = len(json.loads(last_song_ids))
            except:
                song_count = 0
            
            # 兼容旧数据库（如果 fetchall 返回的 row 长度不够，说明迁移可能未生效 - 正常不会发生，但防御一下）
            is_public_val = True
            if len(row) > 9:
                is_public_val = bool(row[9])
                
            subscriptions.append({
                'id': row[0],
                'telegram_id': row[1],
                'playlist_url': row[2],
                'playlist_name': row[3],
                'platform': row[4],
                'song_count': song_count,
                'last_sync_at': row[6],
                'is_active': bool(row[7]) if row[7] is not None else True,
                'created_at': row[8],
                'is_public': is_public_val
            })
        
        return {"subscriptions": subscriptions, "sync_interval": sync_interval}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: int, user: dict = Depends(require_login)):
    """删除歌单订阅"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM scheduled_playlists WHERE id = ?', (subscription_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        if deleted:
            return {"status": "ok", "message": "订阅已删除"}
        else:
            raise HTTPException(status_code=404, detail="订阅不存在")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subscriptions/{subscription_id}/toggle")
async def toggle_subscription(subscription_id: int, user: dict = Depends(require_login)):
    """切换订阅的启用/禁用状态"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 先获取当前状态
        cursor.execute('SELECT is_active FROM scheduled_playlists WHERE id = ?', (subscription_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="订阅不存在")
        
        current_active = row[0] if row[0] is not None else 1
        new_active = 0 if current_active else 1
        
        cursor.execute('UPDATE scheduled_playlists SET is_active = ? WHERE id = ?', (new_active, subscription_id))
        conn.commit()
        conn.close()
        
        return {"status": "ok", "is_active": bool(new_active)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subscriptions/{subscription_id}/toggle_visibility")
async def toggle_subscription_visibility(subscription_id: int, user: dict = Depends(require_login)):
    """切换订阅的可见性 (公开/私有)"""
    try:
        from bot.services import emby
        
        conn = get_db()
        cursor = conn.cursor()
        
        # 先获取当前状态
        cursor.execute('SELECT playlist_name, is_public FROM scheduled_playlists WHERE id = ?', (subscription_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="订阅不存在")
        
        playlist_name = row[0]
        current_public = row[1] if row[1] is not None else 1
        new_public = 0 if current_public else 1
        
        cursor.execute('UPDATE scheduled_playlists SET is_public = ? WHERE id = ?', (new_public, subscription_id))
        conn.commit()
        conn.close()
        
        # 立即在 Emby 中应用
        # 需要查找该订阅对应的 Emby 歌单 ID
        # 查找逻辑：尝试通过名称查找
        auth = emby.get_auth()
        playlist_id = emby.find_playlist_by_name(playlist_name, auth)
        if playlist_id:
            emby.set_playlist_visibility(playlist_id, bool(new_public), auth)
        
        return {"status": "ok", "is_public": bool(new_public)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request, user: dict = Depends(require_login)):
    """歌单订阅管理页面"""
    return templates.TemplateResponse("subscriptions.html", {"request": request, "is_admin": True})





# ============================================================
# 启动服务
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


# 排行榜配置 API
@app.get("/api/ranking/config")
async def get_ranking_config():
    """获取排行榜配置"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'ranking_%'")
    settings = {row['key'].replace('ranking_', ''): row['value'] for row in cursor.fetchall()}
    
    return JSONResponse({
        "target_chat": settings.get('target_chat', ''),
        "daily_time": settings.get('daily_time', '08:00'),
        "weekly_time": settings.get('weekly_time', '10:00'),
        "weekly_day": settings.get('weekly_day', '6'),
        "monthly_time": settings.get('monthly_time', '09:00'),
        "daily_title": settings.get('daily_title', ''),
        "daily_subtitle": settings.get('daily_subtitle', ''),
        "weekly_title": settings.get('weekly_title', '')
    })


@app.post("/api/ranking/config")
async def save_ranking_config(request: Request):
    """保存排行榜配置"""
    try:
        data = await request.json()
        
        conn = get_db()
        cursor = conn.cursor()
        
        fields = ['target_chat', 'daily_time', 'weekly_time', 'weekly_day', 'monthly_time', 'daily_title', 'daily_subtitle', 'weekly_title']
        for field in fields:
            if field in data:
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_settings (key, value)
                    VALUES (?, ?)
                ''', (f'ranking_{field}', data[field]))
        
        conn.commit()
        
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


# 私人雷达配置 API
@app.get("/api/radar/config")
async def get_radar_config():
    """获取私人雷达配置"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'radar_%'")
    settings = {row['key'].replace('radar_', ''): row['value'] for row in cursor.fetchall()}
    
    return JSONResponse({
        "push_enabled": settings.get('push_enabled', '0'),
        "push_time": settings.get('push_time', '09:00')
    })


@app.post("/api/radar/config")
async def save_radar_config(request: Request):
    """保存私人雷达配置"""
    try:
        data = await request.json()
        
        conn = get_db()
        cursor = conn.cursor()
        
        fields = ['push_enabled', 'push_time']
        for field in fields:
            if field in data:
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_settings (key, value)
                    VALUES (?, ?)
                ''', (f'radar_{field}', data[field]))
        
        conn.commit()
        
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


# ============================================================
# 重复歌曲检测 API
# ============================================================

# 缓存扫描结果
_duplicates_cache = None
_duplicates_scan_time = None

@app.post("/api/duplicates/scan")
async def api_duplicates_scan():
    """扫描重复歌曲（通过 Emby API）"""
    global _duplicates_cache, _duplicates_scan_time
    
    try:
        from bot.services.duplicates import scan_duplicates_emby
        import asyncio
        from datetime import datetime
        
        # 通过 Emby API 扫描
        duplicates = await asyncio.to_thread(scan_duplicates_emby)
        
        # 缓存结果
        _duplicates_cache = duplicates
        _duplicates_scan_time = datetime.now().isoformat()
        
        return JSONResponse({
            "success": True,
            "count": len(duplicates),
            "total_files": sum(d['count'] for d in duplicates),
            "scan_time": _duplicates_scan_time
        })
    except Exception as e:
        logger.error(f"扫描重复歌曲失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/duplicates/progress")
async def api_duplicates_progress():
    """获取扫描进度"""
    try:
        from bot.services.duplicates import get_scan_progress
        progress = get_scan_progress()
        return JSONResponse({"success": True, **progress})
    except Exception as e:
        return JSONResponse({"success": False, "status": "error", "message": str(e)})


@app.get("/api/duplicates/list")
async def api_duplicates_list(offset: int = 0, limit: int = 50):
    """获取重复歌曲列表"""
    global _duplicates_cache, _duplicates_scan_time
    
    if _duplicates_cache is None:
        return JSONResponse({
            "success": True,
            "count": 0,
            "items": [],
            "message": "请先执行扫描"
        })
    
    items = _duplicates_cache[offset:offset + limit]
    
    return JSONResponse({
        "success": True,
        "count": len(_duplicates_cache),
        "total_files": sum(len(g.get('files', [])) for g in _duplicates_cache),
        "offset": offset,
        "limit": limit,
        "items": items,
        "scan_time": _duplicates_scan_time
    })


class DeleteDuplicateRequest(BaseModel):
    item_id: str


@app.post("/api/duplicates/delete")
async def api_duplicates_delete(req: DeleteDuplicateRequest):
    """删除重复文件（通过 Emby API）"""
    global _duplicates_cache
    
    try:
        from bot.services.duplicates import delete_emby_item
        import asyncio
        
        success, message = await asyncio.to_thread(delete_emby_item, req.item_id)
        
        if success and _duplicates_cache:
            # 从缓存中移除（确保 ID 类型一致）
            item_id_str = str(req.item_id)
            before_count = sum(len(g['files']) for g in _duplicates_cache)
            
            # 调试：打印要查找的 ID 和缓存中的 ID
            if _duplicates_cache and _duplicates_cache[0]['files']:
                sample_ids = [str(f.get('id', '')) for f in _duplicates_cache[0]['files'][:3]]
                print(f"[Duplicates] 查找 ID: '{item_id_str}', 缓存样本 IDs: {sample_ids}")
            
            for group in _duplicates_cache:
                group['files'] = [f for f in group['files'] if str(f.get('id', '')) != item_id_str]
                group['count'] = len(group['files'])
            # 移除空组或只剩一个文件的组
            _duplicates_cache = [g for g in _duplicates_cache if g['count'] > 1]
            
            after_count = sum(len(g['files']) for g in _duplicates_cache)
            print(f"[Duplicates] 缓存更新: {before_count} -> {after_count} 个文件")
        
        return JSONResponse({"success": success, "message": message})
    except Exception as e:
        print(f"删除文件失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


# 目录缓存（减少云盘重复访问）
_dir_cache = {}
_dir_cache_time = {}
DIR_CACHE_TTL = 300  # 缓存5分钟

# ============================================================
# 音乐元数据管理器 API
# ============================================================

@app.get("/api/metadata/browse")
async def metadata_browse(path: str = Query(default=""), force: bool = Query(default=False)):
    """浏览目录结构（带缓存）"""
    from pathlib import Path
    import time
    
    # 检查缓存
    cache_key = path.strip() if path else "__root__"
    now = time.time()
    if not force and cache_key in _dir_cache:
        if now - _dir_cache_time.get(cache_key, 0) < DIR_CACHE_TTL:
            return JSONResponse(_dir_cache[cache_key])
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 获取配置的目录
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
    row = cursor.fetchone()
    download_dir = row['value'] if row and row['value'] else str(MUSIC_TARGET_DIR)
    
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_target_dir',))
    row = cursor.fetchone()
    organize_dir = row['value'] if row and row['value'] else None
    
    items = []
    current_path = path.strip() if path else ""
    
    if not current_path:
        # 初始显示配置的几个根目录和重要挂载点
        mount_points = [
            {"path": "/watch", "name": "📁 监控来源目录 /watch"},
            {"path": "/music", "name": "📁 整理目标目录 /music"},
            {"path": "/app/uploads", "name": "📁 下载目录 /app/uploads"},
             # 兼容旧配置
            {"path": str(MUSIC_TARGET_DIR), "name": "📁 系统默认目录"}
        ]
        
        # 去重
        seen_paths = set()
        for mp in mount_points:
            p = mp['path']
            if p and p not in seen_paths and Path(p).exists():
                items.append({"path": p, "name": mp['name'], "is_dir": True})
                seen_paths.add(p)
                
        # 添加用户配置的额外目录
        if download_dir and download_dir not in seen_paths and Path(download_dir).exists():
            items.append({"path": download_dir, "name": "📁 当前下载目录", "is_dir": True})
            seen_paths.add(download_dir)
            
        if organize_dir and organize_dir not in seen_paths and Path(organize_dir).exists():
            items.append({"path": organize_dir, "name": "📁 当前整理目录", "is_dir": True})
            seen_paths.add(organize_dir)
            
        return JSONResponse({"items": items, "current": "", "parent": ""})
    
    base = Path(current_path)
    if not base.exists() or not base.is_dir():
        return JSONResponse({"items": [], "current": current_path, "parent": "", "error": "目录不存在"})
    
    parent = str(base.parent) if str(base.parent) != current_path else ""
    
    # 列出子目录和音频文件
    audio_exts = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma'}
    
    try:
        # 先收集文件夹，再收集音频文件（限制数量加速）
        dirs = []
        files = []
        count = 0
        max_items = 5000  # 限制最大数量
        
        for item in base.iterdir():
            if item.name.startswith('.'):
                continue
            if item.is_dir():
                dirs.append(item)
            elif item.suffix.lower() in audio_exts:
                files.append(item)
            count += 1
            if count > max_items:
                break
        
        # 排序并添加到结果
        for d in sorted(dirs, key=lambda x: x.name.lower()):
            items.append({"path": str(d), "name": d.name, "is_dir": True, "type": "folder"})
        for f in sorted(files, key=lambda x: x.name.lower()):
            items.append({"path": str(f), "name": f.name, "is_dir": False, "type": "audio"})
            
    except Exception as e:
        return JSONResponse({"items": [], "current": current_path, "parent": parent, "error": str(e)})
    
    result = {
        "items": items,
        "current": current_path,
        "parent": parent,
        "folder_name": base.name
    }
    # 保存到缓存
    _dir_cache[cache_key] = result
    _dir_cache_time[cache_key] = time.time()
    return JSONResponse(result)


@app.get("/api/metadata/search_files")
async def metadata_search_files(query: str = Query(...), base_dir: str = Query(default="")):
    """递归搜索音频文件"""
    from pathlib import Path
    import time
    
    if not query or len(query.strip()) < 1:
        return JSONResponse({"results": [], "message": "请输入至少1个字符"})
    
    query_lower = query.strip().lower()
    
    # 获取基础目录
    if not base_dir:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        base_dir = row['value'] if row and row['value'] else str(MUSIC_TARGET_DIR)
    
    base_path = Path(base_dir)
    if not base_path.exists() or not base_path.is_dir():
        return JSONResponse({"results": [], "error": "目录不存在"})
    
    audio_exts = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma'}
    results = []
    max_results = 100
    start_time = time.time()
    timeout = 30.0  # 30秒超时
    
    print(f"[Search] 开始搜索: query='{query}', base_dir='{base_dir}'", flush=True)
    
    scanned_count = 0
    matched_count = 0
    
    try:
        # 递归搜索文件夹和音频文件
        for item_path in base_path.rglob('*'):
            # 超时检查
            if time.time() - start_time > timeout:
                break
            
            # 跳过隐藏文件和目录
            if any(part.startswith('.') for part in item_path.parts):
                continue
            
            scanned_count += 1
            
            # 名称匹配检查
            if query_lower not in item_path.name.lower():
                continue
            
            matched_count += 1
            
            # 处理文件夹
            if item_path.is_dir():
                try:
                    relative_path = item_path.relative_to(base_path)
                    results.append({
                        "path": str(item_path),
                        "name": item_path.name,
                        "relative_path": str(relative_path),
                        "parent_dir": str(item_path.parent.relative_to(base_path)) if item_path.parent != base_path else ".",
                        "is_dir": True,
                        "type": "folder"
                    })
                    
                    if len(results) >= max_results:
                        break
                except Exception:
                    continue
            
            # 处理音频文件
            elif item_path.is_file() and item_path.suffix.lower() in audio_exts:
                try:
                    relative_path = item_path.relative_to(base_path)
                    results.append({
                        "path": str(item_path),
                        "name": item_path.name,
                        "relative_path": str(relative_path),
                        "parent_dir": str(item_path.parent.relative_to(base_path)) if item_path.parent != base_path else ".",
                        "is_dir": False,
                        "type": "audio",
                        "size": item_path.stat().st_size
                    })
                    
                    if len(results) >= max_results:
                        break
                except Exception:
                    continue
        
        elapsed = time.time() - start_time
        print(f"[Search] 完成: scanned={scanned_count}, matched={matched_count}, results={len(results)}, time={elapsed:.2f}s", flush=True)
        
        return JSONResponse({
            "results": results,
            "count": len(results),
            "truncated": len(results) >= max_results,
            "query": query
        })
        
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})


@app.get("/api/metadata/detail")
async def metadata_detail(path: str = Query(...)):
    """获取单个音频文件的元数据详情"""
    from pathlib import Path
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    import base64
    
    file_path = Path(path)
    if not file_path.exists():
        return JSONResponse({"error": "文件不存在"})
    
    try:
        audio = File(str(file_path))
        if audio is None:
            return JSONResponse({"error": "无法读取音频文件"})
        
        # 提取元数据
        metadata = {
            "path": str(file_path),
            "filename": file_path.name,
            "title": "",
            "artist": "",
            "album": "",
            "album_artist": "",
            "year": "",
            "track": "",
            "genre": "",
            "has_cover": False,
            "cover_data": None,
            "format": file_path.suffix.lower(),
            "duration": int(audio.info.length) if hasattr(audio, 'info') and hasattr(audio.info, 'length') else 0,
            "bitrate": getattr(audio.info, 'bitrate', 0) if hasattr(audio, 'info') else 0
        }
        
        # FLAC
        if isinstance(audio, FLAC):
            metadata["title"] = audio.get("title", [""])[0] if audio.get("title") else ""
            metadata["artist"] = audio.get("artist", [""])[0] if audio.get("artist") else ""
            metadata["album"] = audio.get("album", [""])[0] if audio.get("album") else ""
            metadata["album_artist"] = audio.get("albumartist", [""])[0] if audio.get("albumartist") else ""
            metadata["year"] = audio.get("date", [""])[0] if audio.get("date") else ""
            metadata["track"] = audio.get("tracknumber", [""])[0] if audio.get("tracknumber") else ""
            metadata["genre"] = audio.get("genre", [""])[0] if audio.get("genre") else ""
            metadata["lyrics"] = audio.get("lyrics", [""])[0] if audio.get("lyrics") else ""
            if audio.pictures:
                metadata["has_cover"] = True
                metadata["cover_data"] = base64.b64encode(audio.pictures[0].data).decode()
        
        # MP3
        elif hasattr(audio, 'tags') and audio.tags:
            tags = audio.tags
            # ID3 tags
            if hasattr(tags, 'get'):
                metadata["title"] = str(tags.get("TIT2", "")) if tags.get("TIT2") else ""
                metadata["artist"] = str(tags.get("TPE1", "")) if tags.get("TPE1") else ""
                metadata["album"] = str(tags.get("TALB", "")) if tags.get("TALB") else ""
                metadata["album_artist"] = str(tags.get("TPE2", "")) if tags.get("TPE2") else ""
                metadata["year"] = str(tags.get("TDRC", "")) if tags.get("TDRC") else ""
                metadata["track"] = str(tags.get("TRCK", "")) if tags.get("TRCK") else ""
                metadata["genre"] = str(tags.get("TCON", "")) if tags.get("TCON") else ""
                for key in tags.keys():
                    if key.startswith('APIC'):
                        metadata["has_cover"] = True
                        metadata["cover_data"] = base64.b64encode(tags[key].data).decode()
                        break
                # 读取内嵌歌词 (USLT)
                for key in tags.keys():
                    if key.startswith('USLT'):
                        metadata["lyrics"] = str(tags[key])
                        break
                else:
                    metadata["lyrics"] = ""
        
        # MP4/M4A
        elif isinstance(audio, MP4):
            metadata["title"] = audio.tags.get("\xa9nam", [""])[0] if audio.tags and audio.tags.get("\xa9nam") else ""
            metadata["artist"] = audio.tags.get("\xa9ART", [""])[0] if audio.tags and audio.tags.get("\xa9ART") else ""
            metadata["album"] = audio.tags.get("\xa9alb", [""])[0] if audio.tags and audio.tags.get("\xa9alb") else ""
            metadata["album_artist"] = audio.tags.get("aART", [""])[0] if audio.tags and audio.tags.get("aART") else ""
            metadata["year"] = audio.tags.get("\xa9day", [""])[0] if audio.tags and audio.tags.get("\xa9day") else ""
            metadata["genre"] = audio.tags.get("\xa9gen", [""])[0] if audio.tags and audio.tags.get("\xa9gen") else ""
            if audio.tags and "covr" in audio.tags and audio.tags["covr"]:
                metadata["has_cover"] = True
                metadata["cover_data"] = base64.b64encode(bytes(audio.tags["covr"][0])).decode()
        
        # 计算完整度
        required_fields = ["title", "artist", "album"]
        optional_fields = ["album_artist", "year", "track", "genre"]
        filled_required = sum(1 for f in required_fields if metadata.get(f))
        filled_optional = sum(1 for f in optional_fields if metadata.get(f))
        metadata["completeness"] = {
            "required": f"{filled_required}/{len(required_fields)}",
            "optional": f"{filled_optional}/{len(optional_fields)}",
            "has_cover": metadata["has_cover"],
            "score": round((filled_required * 2 + filled_optional + (1 if metadata["has_cover"] else 0)) / (len(required_fields) * 2 + len(optional_fields) + 1) * 100)
        }
        
        return JSONResponse(metadata)
        
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.post("/api/metadata/search")
async def metadata_search(request: Request):
    """在线搜索元数据（返回多个候选结果）"""
    import requests
    import urllib.parse
    
    data = await request.json()
    query = data.get("query", "")
    source = data.get("source", "auto")  # auto / netease / qq
    search_type = data.get("type", "song")  # song / album
    
    if not query:
        return JSONResponse({"results": [], "error": "请输入搜索关键词"})
    
    results = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    # 网易云音乐搜索
    if source in ["auto", "netease"]:
        try:
            # Get Cookie
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'ncm_cookie'")
            row = cursor.fetchone()
            ncm_cookie = row['value'] if row else ''
            
            from bot.ncm_downloader import NeteaseMusicAPI
            api = NeteaseMusicAPI(cookie=ncm_cookie)
            
            if search_type == "song":
                songs = api.search_song(query, limit=10)
                # Debug: 打印第一条结果的 cover_url
                if songs:
                    print(f"[MetadataSearch Debug] First song cover_url from API: '{songs[0].get('cover_url', 'EMPTY')}'")
                for s in songs:
                    pub_year = ""
                    if s.get('publish_time'):
                        try:
                            # NCM timestamps are milliseconds
                            pub_year = str(datetime.fromtimestamp(s['publish_time']/1000).year)
                        except: pass
                    
                    # 构建代理 URL
                    raw_cover = s.get('cover_url', '')
                    proxy_cover = f"/api/proxy/cover?url={urllib.parse.quote(raw_cover + '?param=300y300')}" if raw_cover else ""
                    
                    # Debug
                    if not raw_cover:
                        print(f"[MetadataSearch Debug] Song '{s.get('title')}' has NO cover_url!")
                        
                    results.append({
                        "source": "netease",
                        "id": s['source_id'],
                        "title": s['title'],
                        "artist": s['artist'],
                        "album": s['album'],
                        "album_id": s['album_id'],
                        "year": pub_year,
                        "cover_url": proxy_cover,
                        "cover_hd_url": raw_cover
                    })
            elif search_type == "album":
                albums = api.search_album(query, limit=10)
                for a in albums:
                    pub_year = ""
                    if a.get('publish_time'):
                        try:
                            pub_year = str(datetime.fromtimestamp(a['publish_time']/1000).year)
                        except:
                            pass
                            
                    results.append({
                        "source": "netease",
                        "id": a['album_id'],
                        "title": "",
                        "artist": a['artist'],
                        "album": a['name'],
                        "album_id": a['album_id'],
                        "year": pub_year,
                        "cover_url": f"/api/proxy/cover?url={urllib.parse.quote(a.get('pic_url', '') + '?param=300y300')}" if a.get('pic_url') else "",
                        "cover_hd_url": a.get('pic_url', '')
                    })
        except Exception as e:
            print(f"[MetadataSearch] 网易云搜索失败: {e}")
    
    # QQ 音乐搜索
    if source in ["auto", "qq"]:
        try:
            qq_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                'w': query,
                'format': 'json',
                'p': 1,
                'n': 10,
                't': 0 if search_type == "song" else 8
            }
            resp = requests.get(qq_url, params=params, headers={**headers, 'Referer': 'https://y.qq.com'}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if search_type == "song":
                    songs = data.get('data', {}).get('song', {}).get('list', [])
                    for song in songs[:10]:
                        singers = song.get('singer', [])
                        album_mid = song.get('albummid', '')
                        
                        pub_year = ""
                        if song.get('pubtime'):
                            try:
                                # QQ timestamps are seconds
                                pub_year = str(datetime.fromtimestamp(song['pubtime']).year)
                            except: 
                                pub_year = str(song.get('pubtime', ''))[:4]

                        results.append({
                            "source": "qq",
                            "id": song.get('songmid'),
                            "title": song.get('songname', ''),
                            "artist": ', '.join([s.get('name', '') for s in singers]),
                            "album": song.get('albumname', ''),
                            "album_id": album_mid,
                            "year": pub_year,
                            "cover_url": f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else "",
                            "cover_hd_url": f"https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg" if album_mid else ""
                        })
                else:
                    albums = data.get('data', {}).get('album', {}).get('list', [])
                    for album in albums[:10]:
                        album_mid = album.get('albumMID', '')
                        results.append({
                            "source": "qq",
                            "id": album_mid,
                            "title": "",
                            "artist": album.get('singerName', ''),
                            "album": album.get('albumName', ''),
                            "album_id": album_mid,
                            "year": str(album.get('publicTime', ''))[:4] if album.get('publicTime') else "",
                            "cover_url": f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else "",
                            "cover_hd_url": f"https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg" if album_mid else ""
                        })
        except Exception as e:
            print(f"[MetadataSearch] QQ音乐搜索失败: {e}")
    
    return JSONResponse({"results": results})


@app.post("/api/metadata/update")
async def metadata_update(request: Request):
    """更新音频文件元数据"""
    from pathlib import Path
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TCON, APIC
    import requests
    import base64
    
    data = await request.json()
    file_path = data.get("path")
    updates = data.get("metadata", {})
    cover_url = data.get("cover_url")
    save_cover_file = data.get("save_cover_file", False)
    
    if not file_path or not Path(file_path).exists():
        return JSONResponse({"success": False, "error": "文件不存在"})
    
    try:
        audio = File(file_path)
        if audio is None:
            return JSONResponse({"success": False, "error": "无法读取音频文件"})
        
        # 下载封面
        cover_data = None
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    cover_data = resp.content
            except:
                pass
        
        # 更新 FLAC
        if isinstance(audio, FLAC):
            if updates.get("title"): audio["title"] = updates["title"]
            if updates.get("artist"): audio["artist"] = updates["artist"]
            if updates.get("album"): audio["album"] = updates["album"]
            if updates.get("album_artist"): audio["albumartist"] = updates["album_artist"]
            if updates.get("year"): audio["date"] = updates["year"]
            if updates.get("track"): audio["tracknumber"] = updates["track"]
            if updates.get("genre"): audio["genre"] = updates["genre"]
            
            # 自动补全 Album Artist
            if not audio.get("albumartist") and audio.get("artist"):
                audio["albumartist"] = audio["artist"]
            
            if cover_data:
                from mutagen.flac import Picture
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = cover_data
                audio.clear_pictures()
                audio.add_picture(pic)
            
            audio.save()
        
        # 更新 MP3
        elif file_path.lower().endswith('.mp3'):
            try:
                tags = ID3(file_path)
            except:
                from mutagen.id3 import ID3NoHeaderError
                tags = ID3()
            
            if updates.get("title"): tags["TIT2"] = TIT2(encoding=3, text=updates["title"])
            if updates.get("artist"): tags["TPE1"] = TPE1(encoding=3, text=updates["artist"])
            if updates.get("album"): tags["TALB"] = TALB(encoding=3, text=updates["album"])
            if updates.get("album_artist"): tags["TPE2"] = TPE2(encoding=3, text=updates["album_artist"])
            if updates.get("year"): tags["TDRC"] = TDRC(encoding=3, text=updates["year"])
            if updates.get("track"): tags["TRCK"] = TRCK(encoding=3, text=updates["track"])
            if updates.get("genre"): tags["TCON"] = TCON(encoding=3, text=updates["genre"])
            
            # 自动补全 Album Artist
            if not tags.get("TPE2") and tags.get("TPE1"):
                tags["TPE2"] = TPE2(encoding=3, text=tags["TPE1"].text[0])
            
            if cover_data:
                tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data)
            
            tags.save(file_path)
        
        # 更新 MP4/M4A
        elif isinstance(audio, MP4):
            if updates.get("title"): audio.tags["\xa9nam"] = [updates["title"]]
            if updates.get("artist"): audio.tags["\xa9ART"] = [updates["artist"]]
            if updates.get("album"): audio.tags["\xa9alb"] = [updates["album"]]
            if updates.get("album_artist"): audio.tags["aART"] = [updates["album_artist"]]
            if updates.get("year"): audio.tags["\xa9day"] = [updates["year"]]
            if updates.get("genre"): audio.tags["\xa9gen"] = [updates["genre"]]
            
            # 自动补全 Album Artist
            if not audio.tags.get("aART") and audio.tags.get("\xa9ART"):
                audio.tags["aART"] = audio.tags.get("\xa9ART")

            if cover_data:
                from mutagen.mp4 import MP4Cover
                audio.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            
            audio.save()
        
        # 保存封面文件
        if save_cover_file and cover_data:
            cover_path = Path(file_path).parent / "cover.jpg"
            with open(cover_path, 'wb') as f:
                f.write(cover_data)
        
        return JSONResponse({"success": True, "message": "元数据已更新"})
        
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})



@app.post("/api/metadata/organize")
async def metadata_organize(request: Request):
    """手动整理指定目录的音乐文件到目标目录"""
    from pathlib import Path
    from bot.file_organizer import organize_file, read_audio_metadata
    
    data = await request.json()
    source_dir = data.get("source_dir", "")
    
    if not source_dir:
        return JSONResponse({"success": False, "error": "未指定源目录"})
    
    source_path = Path(source_dir)
    if not source_path.exists() or not source_path.is_dir():
        return JSONResponse({"success": False, "error": "目录不存在"})
    
    # 获取目标目录配置
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_target_dir',))
    row = cursor.fetchone()
    target_dir = row['value'] if row and row['value'] else None
    
    if not target_dir:
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        target_dir = row['value'] if row and row['value'] else str(MUSIC_TARGET_DIR)
    
    # 获取整理模板
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_template',))
    row = cursor.fetchone()
    template = row['value'] if row and row['value'] else '{album_artist}/{album}'
    
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_on_conflict',))
    row = cursor.fetchone()
    on_conflict = row['value'] if row and row['value'] else 'skip'
    
    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_mode',))
    row = cursor.fetchone()
    mode = row['value'] if row and row['value'] else 'move'
    
    # 支持的音频格式
    audio_exts = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma'}
    
    organized = 0
    skipped = 0
    failed = 0
    
    # 递归遍历目录下的所有音频文件（包括子目录）
    import os
    for root, dirs, files in os.walk(str(source_path)):
        for filename in files:
            if Path(filename).suffix.lower() in audio_exts:
                file_path = os.path.join(root, filename)
                try:
                    result = organize_file(
                        file_path,
                        target_dir,
                        template=template,
                        on_conflict=on_conflict,
                        move=mode
                    )
                    if result:
                        organized += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f"[Organize] 整理失败 {filename}: {e}")
                    failed += 1
    
    # 清除目录缓存
    if source_dir in _dir_cache:
        del _dir_cache[source_dir]
    
    return JSONResponse({
        "success": True,
        "organized": organized,
        "skipped": skipped,
        "failed": failed
    })



@app.post("/api/metadata/batch-scrape")
async def batch_scrape_metadata(request: Request):
    """批量刮削：补全缺失的封面和艺术家头像"""
    from pathlib import Path
    from bot.file_organizer import (
        search_cover_online, search_artist_photo, read_audio_metadata, ensure_artist_photo
    )
    
    data = await request.json()
    target_dir = data.get("target_dir", "")
    
    if not target_dir:
        return JSONResponse({"success": False, "error": "未指定目录"})
    
    target_path = Path(target_dir)
    if not target_path.exists():
        return JSONResponse({"success": False, "error": "目录不存在"})
    
    covers_added = 0
    artists_added = 0
    errors = 0
    processed_artists = set()
    
    audio_exts = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma'}
    
    # 遍历所有音频文件
    import os
    for root, dirs, files in os.walk(str(target_path)):
        root_path = Path(root)
        
        # 检查该目录是否缺少封面
        has_cover = any((root_path / c).exists() for c in ['cover.jpg', 'folder.jpg', 'album.jpg'])
        
        for filename in files:
            if Path(filename).suffix.lower() not in audio_exts:
                continue
            
            file_path = root_path / filename
            
            try:
                # 读取元数据
                metadata = read_audio_metadata(str(file_path))
                if not metadata:
                    continue
                
                artist = metadata.get('album_artist') or metadata.get('artist', '')
                album = metadata.get('album', '')
                title = metadata.get('title', '')
                
                # 补全专辑封面
                if not has_cover and album:
                    cover_path = root_path / "cover.jpg"
                    if search_cover_online(artist, album, title, str(cover_path)):
                        covers_added += 1
                        has_cover = True
                        print(f"[BatchScrape] 封面已补全: {root_path.name}")
                
                # 补全艺术家头像（只处理一次）
                if artist and artist not in processed_artists:
                    processed_artists.add(artist)
                    artist_dir = root_path.parent
                    # 确保是艺术家目录（包含专辑目录）
                    if artist_dir != target_path and artist_dir.is_dir():
                        artist_photo = artist_dir / "folder.jpg"
                        if not artist_photo.exists():
                            if search_artist_photo(artist, str(artist_photo)):
                                artists_added += 1
                                print(f"[BatchScrape] 艺术家头像已补全: {artist}")
                
            except Exception as e:
                errors += 1
                print(f"[BatchScrape] 处理失败 {filename}: {e}")
    
    return JSONResponse({
        "success": True,
        "covers_added": covers_added,
        "artists_added": artists_added,
        "errors": errors,
        "artists_processed": len(processed_artists)
    })


# --- 工具 API ---

class FixMetadataRequest(BaseModel):
    file_path: str
    song_id: str
    source: str = 'ncm'

@app.post("/api/tools/search_ncm")
async def api_search_ncm(keyword: str = Query(..., min_length=1)):
    """搜索网易云音乐 (用于手动修复元数据)"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'ncm_cookie'")
        row = cursor.fetchone()
        ncm_cookie = row['value'] if row else ''
        
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(cookie=ncm_cookie)
        results = api.search_song(keyword, limit=20)
        
        return {"code": 200, "data": results}
    except Exception as e:
        return {"code": 500, "message": str(e)}

@app.post("/api/tools/apply_metadata")
async def api_apply_metadata(req: FixMetadataRequest):
    """应用元数据到本地文件"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        settings = {}
        for key in ['ncm_cookie', 'qq_cookie', 'download_dir', 'music_proxy_url', 'music_proxy_key']:
            cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            settings[key] = row['value'] if row else ''
            
        from bot.ncm_downloader import MusicAutoDownloader
        downloader = MusicAutoDownloader(
            ncm_cookie=settings['ncm_cookie'],
            qq_cookie=settings['qq_cookie'],
            download_dir=settings['download_dir'] or str(MUSIC_TARGET_DIR),
            proxy_url=settings['music_proxy_url'], 
            proxy_key=settings['music_proxy_key']
        )
        
        success, msg = await asyncio.to_thread(downloader.apply_metadata_to_file, req.file_path, req.song_id, source=req.source)
        
        if success:
             return {"code": 200, "message": "Success"}
        else:
             return {"code": 500, "message": msg}
    except Exception as e:
        return {"code": 500, "message": str(e)}

@app.get("/api/proxy/cover")
async def api_proxy_cover(url: str = Query(...)):
    """代理获取封面图片，解决 Referer 和 Mixed Content 问题"""
    import requests
    from fastapi import Response
    
    if not url:
        return Response(status_code=404)
        
    try:
        # 移除严格的域名检查，避免漏网之鱼，改为日志警告
        # 构造请求头
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        
        # 网易云特殊处理
        if 'music.126.net' in url or 'music.163.com' in url:
            headers['Referer'] = 'https://music.163.com/'
        else:
            # 其他来源设为空 Referer
            headers['Referer'] = ''
        
        # 使用 verify=False 忽略 SSL 问题 (部分 CDN 证书可能有问题)
        # run in threadpool to avoid blocking async loop
        def fetch():
            return requests.get(url, headers=headers, timeout=15, verify=False)
            
        r = await asyncio.to_thread(fetch)
        
        if r.status_code == 200:
            content_type = r.headers.get('Content-Type', 'image/jpeg')
            return Response(content=r.content, media_type=content_type)
        else:
            print(f"[Proxy] Failed to fetch {url}: {r.status_code}")
            return Response(status_code=404)
            
    except Exception as e:
        print(f"[Proxy] Error fetching {url}: {e}")
        return Response(status_code=500)

class OrganizeRequest(BaseModel):
    source_dir: str

@app.post("/api/tools/organize_preview")
async def api_organize_preview(req: OrganizeRequest):
    """统计目录下的音频文件数量"""
    try:
        from bot.file_organizer import AUDIO_EXTENSIONS
        from pathlib import Path
        
        source_path = Path(req.source_dir)
        if not source_path.exists():
            return {"code": 400, "message": "目录不存在", "count": 0}
        
        audio_files = list(source_path.rglob('*'))
        audio_count = sum(1 for f in audio_files if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
        
        return {"code": 200, "count": audio_count}
    except Exception as e:
        return {"code": 500, "message": str(e), "count": 0}

@app.post("/api/tools/organize_current_dir")
async def api_organize_current_dir(req: OrganizeRequest):
    """批量整理指定目录下的音频文件"""
    try:
        from bot.file_organizer import organize_file, AUDIO_EXTENSIONS
        from pathlib import Path
        import logging
        logger = logging.getLogger(__name__)
        
        source_path = Path(req.source_dir)
        if not source_path.exists():
            return {"code": 400, "message": "目录不存在"}
        
        # 读取整理配置
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'organize_target_dir'")
        row = cursor.fetchone()
        target_dir = row['value'] if row and row['value'] else ''
        
        # 如果 organize_target_dir 为空，尝试 organize_dir 回退
        if not target_dir:
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'organize_dir'")
            row = cursor.fetchone()
            target_dir = row['value'] if row and row['value'] else ''
        
        # 最终回退到 /music
        if not target_dir:
            target_dir = '/music'
        
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'organize_template'")
        row = cursor.fetchone()
        template = row['value'] if row and row['value'] else '{album_artist}/{album}'
        
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'organize_on_conflict'")
        row = cursor.fetchone()
        on_conflict = row['value'] if row and row['value'] else 'skip'
        
        conn.close()
        
        # 收集所有音频文件（递归扫描所有子目录）
        audio_files = [f for f in source_path.rglob('*') 
                       if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
        
        if not audio_files:
            return {"code": 200, "message": "没有找到音频文件", "processed": 0, "total": 0}
        
        total_files = len(audio_files)
        logger.info(f"开始整理 {total_files} 个音频文件...")
        
        # 批量处理
        success_count = 0
        failed_files = []
        moved_files = []  # 记录移动的文件及其新路径
        
        for idx, file_path in enumerate(audio_files, 1):
            if idx % 10 == 0 or idx == total_files:
                logger.info(f"整理进度: {idx}/{total_files} ({idx*100//total_files}%)")
            
            result = await asyncio.to_thread(
                organize_file, 
                str(file_path), 
                target_dir, 
                template, 
                True,  # move
                on_conflict
            )
            if result:
                success_count += 1
                # result 是新文件路径
                moved_files.append({
                    "original": str(file_path),
                    "new": result,
                    "name": file_path.name
                })
                print(f"[FileOrganizer] 已移动: {file_path.name} -> {result}", flush=True)
            else:
                failed_files.append(file_path.name)
        
        logger.info(f"整理完成: {success_count}/{total_files}")
        
        # 清理空文件夹
        if success_count > 0:
            try:
                deleted_dirs = 0
                # 从最深的子目录开始删除空目录
                for dirpath in sorted(source_path.rglob('*'), key=lambda p: len(str(p)), reverse=True):
                    if dirpath.is_dir():
                        try:
                            # 检查目录是否为空（忽略隐藏文件）
                            contents = [f for f in dirpath.iterdir() if not f.name.startswith('.')]
                            if not contents:
                                dirpath.rmdir()
                                deleted_dirs += 1
                        except OSError:
                            pass  # 目录不为空或无权限
                if deleted_dirs > 0:
                    logger.info(f"已清理 {deleted_dirs} 个空文件夹")
            except Exception as cleanup_err:
                logger.warning(f"清理空文件夹失败: {cleanup_err}")
        
        # 触发 Emby 扫库
        if success_count > 0:
            try:
                from bot.main import trigger_emby_library_scan
                await asyncio.to_thread(trigger_emby_library_scan)
                logger.info(f"整理完成后触发 Emby 扫库")
            except Exception as scan_err:
                logger.warning(f"触发 Emby 扫库失败: {scan_err}")
        
        return {
            "code": 200, 
            "message": f"整理完成: {success_count}/{total_files}", 
            "processed": success_count, 
            "total": total_files,
            "failed": failed_files[:10],  # 只返回前10个失败文件
            "moved": moved_files  # 返回移动的文件列表
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"code": 500, "message": str(e)}


# ============================================================
# 用户会员系统 API
# ============================================================

from werkzeug.security import generate_password_hash, check_password_hash
import random
import string
import base64

# ----- 密码加密/解密工具 (用于同步 Emby 密码) -----

def get_encryption_key() -> str:
    """获取或生成加密密钥"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'member_encryption_key'")
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]
    
    # 生成新密钥
    import secrets
    new_key = secrets.token_hex(32)
    cursor.execute("""
        INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('member_encryption_key', ?)
    """, (new_key,))
    conn.commit()
    conn.close()
    return new_key


def encrypt_password(password: str) -> str:
    """加密密码（可逆）"""
    key = get_encryption_key()
    # XOR 加密 + base64 编码
    key_bytes = key.encode('utf-8')
    pwd_bytes = password.encode('utf-8')
    encrypted = bytes([pwd_bytes[i] ^ key_bytes[i % len(key_bytes)] for i in range(len(pwd_bytes))])
    return base64.b64encode(encrypted).decode('utf-8')


def decrypt_password(encrypted: str) -> str:
    """解密密码"""
    if not encrypted:
        return ''
    try:
        key = get_encryption_key()
        key_bytes = key.encode('utf-8')
        encrypted_bytes = base64.b64decode(encrypted.encode('utf-8'))
        decrypted = bytes([encrypted_bytes[i] ^ key_bytes[i % len(key_bytes)] for i in range(len(encrypted_bytes))])
        return decrypted.decode('utf-8')
    except:
        return ''


def get_system_config(key: str, default: str = '') -> str:
    """获取系统配置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM system_config WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    except:
        return default


def set_system_config(key: str, value: str):
    """设置系统配置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO system_config (key, value, updated_at) 
        VALUES (?, ?, ?)
    ''', (key, value, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def generate_card_key() -> str:
    """生成卡密"""
    chars = string.ascii_uppercase + string.digits
    parts = [''.join(random.choices(chars, k=4)) for _ in range(4)]
    return '-'.join(parts)


def add_points_log(user_id: int, amount: int, reason: str):
    """记录积分变动"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO points_log (user_id, change_amount, reason) VALUES (?, ?, ?)
    ''', (user_id, amount, reason))
    conn.commit()
    conn.close()


def add_membership_log(user_id: int, days: int, source: str, detail: str = ''):
    """记录会员变动"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO membership_log (user_id, duration_days, source, source_detail) VALUES (?, ?, ?, ?)
    ''', (user_id, days, source, detail))
    conn.commit()
    conn.close()


# ----- 邮件服务 -----
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def get_setting_value(key: str, default: str = '') -> str:
    """从数据库获取设置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    except:
        return default

def send_email(to_email: str, subject: str, content: str):
    """发送邮件"""
    # 优先从数据库读取配置
    smtp_server = get_setting_value('smtp_server') or os.getenv('SMTP_SERVER')
    smtp_port_str = get_setting_value('smtp_port') or os.getenv('SMTP_PORT', '587')
    smtp_port = int(smtp_port_str) if smtp_port_str.isdigit() else 587
    
    smtp_user = get_setting_value('smtp_user') or os.getenv('SMTP_USER')
    smtp_password = get_setting_value('smtp_password') or os.getenv('SMTP_PASSWORD')
    smtp_from = get_setting_value('smtp_from') or os.getenv('SMTP_FROM_EMAIL', smtp_user)
    
    if not smtp_server or not smtp_user:
        print("[Email] SMTP Not configured")
        return False
    
    from_email = smtp_from or os.getenv('SMTP_FROM_EMAIL', smtp_user)
    
    try:
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_email
        msg['Subject'] = subject
        
        msg.attach(MIMEText(content, 'html', 'utf-8'))
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"[Email] Send failed: {e}")
        return False

# ----- 用户认证相关 -----

class UserRegisterRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class UserLoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/user/register")
async def user_register(req: UserRegisterRequest):
    """用户注册"""
    # 检查是否开放注册
    if get_system_config('enable_user_register', 'true') != 'true':
        return {"code": 403, "message": "注册功能已关闭"}
    
    if len(req.username) < 3 or len(req.username) > 20:
        return {"code": 400, "message": "用户名长度需在3-20个字符之间"}
    
    if len(req.password) < 6:
        return {"code": 400, "message": "密码长度至少6个字符"}
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 检查用户名是否存在
        cursor.execute('SELECT id FROM web_users WHERE username = ?', (req.username,))
        if cursor.fetchone():
            conn.close()
            return {"code": 400, "message": "用户名已存在"}
        
        # 检查邮箱是否存在（如果提供了邮箱）
        if req.email:
            cursor.execute('SELECT id FROM web_users WHERE email = ?', (req.email,))
            if cursor.fetchone():
                conn.close()
                return {"code": 400, "message": "邮箱已被使用"}
        
        # 创建用户
        password_hash = generate_password_hash(req.password)
        password_encrypted = encrypt_password(req.password)  # 存储加密版本以便创建 Emby 账号
        cursor.execute('''
            INSERT INTO web_users (username, password_hash, password_encrypted, email, role, points, is_active)
            VALUES (?, ?, ?, ?, 'user', 0, 1)
        ''', (req.username, password_hash, password_encrypted, req.email))
        
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        return {"code": 200, "message": "注册成功", "user_id": user_id}
        
    except Exception as e:
        return {"code": 500, "message": f"注册失败: {str(e)}"}


@app.post("/api/user/login")
async def user_login(req: UserLoginRequest, response: Response):
    """用户登录"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, username, password_hash, role, is_active FROM web_users WHERE username = ?
        ''', (req.username,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return {"code": 401, "message": "用户名或密码错误"}
        
        user_id, username, password_hash, role, is_active = row
        
        if not check_password_hash(password_hash, req.password):
            return {"code": 401, "message": "用户名或密码错误"}
        
        if not is_active:
            return {"code": 403, "message": "账号已被禁用"}
        
        # 创建 Session
        session_id = secrets.token_hex(32)
        save_session(session_id, username, role, max_age=86400 * 7)  # 7天有效
        
        # 设置 Cookie
        response.set_cookie(
            key="user_session_id",
            value=session_id,
            max_age=86400 * 7,
            httponly=True,
            samesite="lax"
        )
        
        return {"code": 200, "message": "登录成功", "username": username, "role": role}
        
    except Exception as e:
        return {"code": 500, "message": f"登录失败: {str(e)}"}


@app.post("/api/user/logout")
async def user_logout(response: Response, user_session_id: Optional[str] = Cookie(None)):
    """用户登出"""
    if user_session_id:
        delete_session(user_session_id)
    response.delete_cookie("user_session_id")
    return {"code": 200, "message": "已登出"}


async def get_current_member(user_session_id: Optional[str] = Cookie(None)):
    """获取当前登录的会员"""
    if not user_session_id:
        return None
    session = get_session(user_session_id)
    if not session:
        return None
    
    # 获取完整用户信息
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 尝试查询新列，如果失败回退到旧查询
        try:
            cursor.execute('''
                SELECT id, username, email, role, emby_user_id, emby_username, points, 
                       expire_at, is_active, last_checkin_at, created_at, password_encrypted 
                FROM web_users WHERE username = ?
            ''', (session['username'],))
            row = cursor.fetchone()
            has_password_encrypted = True
        except:
            cursor.execute('''
                SELECT id, username, email, role, emby_user_id, emby_username, points, 
                       expire_at, is_active, last_checkin_at, created_at 
                FROM web_users WHERE username = ?
            ''', (session['username'],))
            row = cursor.fetchone()
            has_password_encrypted = False
        conn.close()
        
        if row:
            result = {
                'id': row[0],
                'username': row[1],
                'email': row[2],
                'role': row[3],
                'emby_user_id': row[4],
                'emby_username': row[5],
                'points': row[6],
                'expire_at': row[7],
                'is_active': row[8],
                'last_checkin_at': row[9],
                'created_at': row[10],
                'password_encrypted': row[11] if has_password_encrypted and len(row) > 11 else None
            }
            return result
        return None
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user_session_id: Optional[str] = Cookie(None), session_id: Optional[str] = Cookie(None)):
    """用户个人中心"""
    user = await get_current_member(user_session_id)
    
    # 如果没有会员登录，检查是否有管理员登录
    if not user and session_id:
        admin = await get_current_user(session_id)
        if admin:
            # 管理员访问个人中心时，创建虚拟用户信息展示
            # 尝试从 bot 的 user_bindings 表获取已绑定的 Emby 账号
            emby_user_id = None
            emby_username = None
            try:
                from bot.config import ADMIN_USER_ID
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT emby_username, emby_user_id FROM user_bindings WHERE telegram_id = ?
                ''', (ADMIN_USER_ID,))
                row = cursor.fetchone()
                conn.close()
                if row:
                    emby_username = row[0]
                    emby_user_id = row[1]
            except Exception as e:
                import traceback
                traceback.print_exc()
            
            user = {
                'id': 0,
                'username': admin.get('username', 'admin'),
                'email': None,
                'role': 'admin',
                'emby_user_id': emby_user_id,
                'emby_username': emby_username,
                'points': 999999,
                'expire_at': None,
                'is_active': True,
                'last_checkin_at': None,
                'created_at': None
            }
    
    if not user:
        return RedirectResponse(url="/login?role=user", status_code=302)
        
    # 计算会员剩余天数
    is_member = False
    days_left = 0
    if user.get('expire_at'):
        try:
            expire_dt = datetime.fromisoformat(str(user['expire_at']))
            now = datetime.now()
            if expire_dt > now:
                is_member = True
                days_left = (expire_dt - now).days
        except:
             pass
    
    # 管理员永久会员
    if user.get('role') == 'admin':
        is_member = True
        days_left = 99999
             
    # 格式化日期显示
    if user.get('expire_at'):
        user['expire_at'] = str(user['expire_at'])

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "user": user,
        "is_member": is_member,
        "days_left": days_left,
        "is_admin": user.get('role') == 'admin'
    })


@app.get("/api/user/profile")
async def get_user_profile(user_session_id: Optional[str] = Cookie(None)):
    """获取用户信息"""
    user = await get_current_member(user_session_id)
    if not user:
        return {"code": 401, "message": "请先登录"}
    
    # 判断会员状态
    is_member = False
    days_left = 0
    if user['expire_at']:
        expire_dt = datetime.fromisoformat(user['expire_at']) if isinstance(user['expire_at'], str) else user['expire_at']
        now = datetime.now()
        if expire_dt > now:
            is_member = True
            days_left = (expire_dt - now).days
    
    return {
        "code": 200,
        "user": {
            "id": user['id'],
            "username": user['username'],
            "email": user['email'],
            "points": user['points'],
            "expire_at": user['expire_at'],
            "is_member": is_member,
            "days_left": days_left,
            "emby_username": user['emby_username'],
            "emby_url": EMBY_URL,
            "last_checkin_at": user['last_checkin_at'],
            "created_at": user['created_at']
        }
    }


# ----- 签到系统 -----

@app.post("/api/user/checkin")
async def user_checkin(user_session_id: Optional[str] = Cookie(None)):
    """每日签到"""
    user = await get_current_member(user_session_id)
    if not user:
        return {"code": 401, "message": "请先登录"}
    
    today = datetime.now().date().isoformat()
    
    # 检查今天是否已签到
    if user['last_checkin_at'] and str(user['last_checkin_at']) == today:
        return {"code": 400, "message": "今天已签到过了"}
    
    # 计算签到积分
    mode = get_system_config('checkin_points_mode', 'random')
    if mode == 'fixed':
        points = int(get_system_config('checkin_points_fixed', '10'))
    else:
        min_pts = int(get_system_config('checkin_points_min', '5'))
        max_pts = int(get_system_config('checkin_points_max', '20'))
        points = random.randint(min_pts, max_pts)
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 更新用户积分和签到时间
        cursor.execute('''
            UPDATE web_users SET points = points + ?, last_checkin_at = ? WHERE id = ?
        ''', (points, today, user['id']))
        
        conn.commit()
        conn.close()
        
        # 记录积分变动
        add_points_log(user['id'], points, 'checkin')
        
        return {
            "code": 200, 
            "message": f"签到成功，获得 {points} 积分",
            "points": points,
            "total_points": user['points'] + points
        }
        
    except Exception as e:
        return {"code": 500, "message": f"签到失败: {str(e)}"}


# ----- 兑换系统 -----

class RedeemCardRequest(BaseModel):
    card_key: str


@app.post("/api/user/redeem/card")
async def redeem_card(req: RedeemCardRequest, user_session_id: Optional[str] = Cookie(None)):
    """卡密兑换"""
    user = await get_current_member(user_session_id)
    if not user:
        return {"code": 401, "message": "请先登录"}
    
    card_key = req.card_key.strip().upper()
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 查找卡密
        cursor.execute('''
            SELECT id, duration_days, used_by FROM card_keys WHERE card_key = ?
        ''', (card_key,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return {"code": 404, "message": "卡密不存在"}
        
        card_id, duration_days, used_by = row
        
        if used_by:
            conn.close()
            return {"code": 400, "message": "卡密已被使用"}
        
        # 标记卡密已使用
        cursor.execute('''
            UPDATE card_keys SET used_by = ?, used_at = ? WHERE id = ?
        ''', (user['id'], datetime.now().isoformat(), card_id))
        
        # 计算新的到期时间
        now = datetime.now()
        if user['expire_at']:
            try:
                current_expire = datetime.fromisoformat(user['expire_at'])
                if current_expire > now:
                    new_expire = current_expire + timedelta(days=duration_days)
                else:
                    new_expire = now + timedelta(days=duration_days)
            except:
                new_expire = now + timedelta(days=duration_days)
        else:
            new_expire = now + timedelta(days=duration_days)
        
        # 更新用户到期时间
        cursor.execute('''
            UPDATE web_users SET expire_at = ?, is_active = 1 WHERE id = ?
        ''', (new_expire.isoformat(), user['id']))
        
        # 如果用户没有 Emby 账号，创建一个
        if not user['emby_user_id']:
            # 调用 Emby API 创建账号（使用和网站注册相同的用户名密码）
            from bot.services.emby import create_emby_user
            real_password = decrypt_password(user.get('password_encrypted', ''))
            if not real_password:
                real_password = req.card_key[:8]  # 回退方案：旧用户没有存密码
            emby_result = await asyncio.to_thread(create_emby_user, user['username'], real_password)
            if emby_result and emby_result.get('success'):
                cursor.execute('''
                    UPDATE web_users SET emby_user_id = ?, emby_username = ? WHERE id = ?
                ''', (emby_result.get('user_id'), user['username'], user['id']))
        
        conn.commit()
        conn.close()
        
        # 记录会员变动
        add_membership_log(user['id'], duration_days, 'card_key', card_key)
        
        return {
            "code": 200,
            "message": f"兑换成功，增加 {duration_days} 天会员",
            "duration_days": duration_days,
            "expire_at": new_expire.isoformat()
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"code": 500, "message": f"兑换失败: {str(e)}"}


class RedeemPointsRequest(BaseModel):
    days: int


@app.post("/api/user/redeem/points")
async def redeem_points(req: RedeemPointsRequest, user_session_id: Optional[str] = Cookie(None)):
    """积分兑换会员"""
    user = await get_current_member(user_session_id)
    if not user:
        return {"code": 401, "message": "请先登录"}
    
    if req.days < 1:
        return {"code": 400, "message": "兑换天数至少为1天"}
    
    points_per_day = int(get_system_config('points_per_day', '100'))
    required_points = req.days * points_per_day
    
    if user['points'] < required_points:
        return {"code": 400, "message": f"积分不足，需要 {required_points} 积分，当前 {user['points']} 积分"}
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 扣除积分
        cursor.execute('''
            UPDATE web_users SET points = points - ? WHERE id = ?
        ''', (required_points, user['id']))
        
        # 计算新的到期时间
        now = datetime.now()
        if user['expire_at']:
            try:
                current_expire = datetime.fromisoformat(user['expire_at'])
                if current_expire > now:
                    new_expire = current_expire + timedelta(days=req.days)
                else:
                    new_expire = now + timedelta(days=req.days)
            except:
                new_expire = now + timedelta(days=req.days)
        else:
            new_expire = now + timedelta(days=req.days)
        
        # 更新到期时间
        cursor.execute('''
            UPDATE web_users SET expire_at = ?, is_active = 1 WHERE id = ?
        ''', (new_expire.isoformat(), user['id']))
        
        # 如果用户没有 Emby 账号，创建一个
        if not user['emby_user_id']:
            from bot.services.emby import create_emby_user
            # 使用和网站注册相同的密码
            real_password = decrypt_password(user.get('password_encrypted', ''))
            if not real_password:
                real_password = secrets.token_urlsafe(8)  # 回退方案：旧用户没有存密码
            emby_result = await asyncio.to_thread(create_emby_user, user['username'], real_password)
            if emby_result and emby_result.get('success'):
                cursor.execute('''
                    UPDATE web_users SET emby_user_id = ?, emby_username = ? WHERE id = ?
                ''', (emby_result.get('user_id'), user['username'], user['id']))
        
        conn.commit()
        conn.close()
        
        # 记录
        add_points_log(user['id'], -required_points, 'points_exchange')
        add_membership_log(user['id'], req.days, 'points', f'{required_points} 积分')
        
        return {
            "code": 200,
            "message": f"兑换成功，消耗 {required_points} 积分，增加 {req.days} 天会员",
            "points_used": required_points,
            "duration_days": req.days,
            "expire_at": new_expire.isoformat()
        }
        
    except Exception as e:
        return {"code": 500, "message": f"兑换失败: {str(e)}"}


# ----- 管理员功能 -----

async def require_admin(session_id: Optional[str] = Cookie(None)):
    """要求管理员权限"""
    user = await get_current_user(session_id)
    if not user or user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@app.get("/api/admin/members")
async def get_members(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_login)
):
    """获取会员列表（管理员）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 获取总数
        cursor.execute('SELECT COUNT(*) FROM web_users')
        total = cursor.fetchone()[0]
        
        # 分页查询
        offset = (page - 1) * per_page
        cursor.execute('''
            SELECT id, username, email, role, emby_username, points, expire_at, 
                   is_active, last_checkin_at, created_at, telegram_id
            FROM web_users
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', (per_page, offset))
        
        rows = cursor.fetchall()
        
        # 获取管理员的 Emby 绑定信息
        admin_emby_username = None
        try:
            from bot.config import ADMIN_USER_ID
            cursor.execute('SELECT emby_username FROM user_bindings WHERE telegram_id = ?', (ADMIN_USER_ID,))
            admin_binding = cursor.fetchone()
            if admin_binding:
                admin_emby_username = admin_binding[0]
        except:
            pass
            
        conn.close()
        
        members = []
        now = datetime.now()
        for row in rows:
            expire_at = row[6]
            is_member = False
            days_left = 0
            if expire_at:
                try:
                    expire_dt = datetime.fromisoformat(expire_at) if isinstance(expire_at, str) else expire_at
                    if expire_dt > now:
                        is_member = True
                        days_left = (expire_dt - now).days
                except:
                    pass
            
            members.append({
                'id': row[0],
                'username': row[1],
                'email': row[2],
                'role': row[3],
                'emby_username': admin_emby_username if row[3] == 'admin' and not row[4] else row[4],
                'points': row[5],
                'expire_at': row[6],
                'is_active': row[7],
                'is_member': is_member,
                'days_left': days_left,
                'last_checkin_at': row[8],
                'created_at': row[9],
                'telegram_id': row[10]
            })
            if row[1] == 'zlh':
                print(f"[DEBUG] get_members zlh row: {row}")
                print(f"[DEBUG] get_members mapped: emby_username={row[4]}")
        
        return {
            "code": 200,
            "members": members,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


class GiftPointsRequest(BaseModel):
    points: int
    reason: Optional[str] = "管理员赠送"


@app.post("/api/admin/members/{user_id}/gift-points")
async def gift_points(
    user_id: int,
    req: GiftPointsRequest,
    user: dict = Depends(require_login)
):
    """赠送积分"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE web_users SET points = points + ? WHERE id = ?', (req.points, user_id))
        if cursor.rowcount == 0:
            conn.close()
            return {"code": 404, "message": "用户不存在"}
        
        conn.commit()
        conn.close()
        
        add_points_log(user_id, req.points, f'admin_gift:{req.reason}')
        
        return {"code": 200, "message": f"已赠送 {req.points} 积分"}
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


class GiftDaysRequest(BaseModel):
    days: int
    reason: Optional[str] = "管理员赠送"


@app.post("/api/admin/members/{user_id}/gift-days")
async def gift_days(
    user_id: int,
    req: GiftDaysRequest,
    user: dict = Depends(require_login)
):
    """赠送会员天数"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 获取当前用户
        cursor.execute('SELECT expire_at FROM web_users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {"code": 404, "message": "用户不存在"}
        
        # 计算新到期时间
        now = datetime.now()
        current_expire = row[0]
        if current_expire:
            try:
                expire_dt = datetime.fromisoformat(current_expire) if isinstance(current_expire, str) else current_expire
                if expire_dt > now:
                    new_expire = expire_dt + timedelta(days=req.days)
                else:
                    new_expire = now + timedelta(days=req.days)
            except:
                new_expire = now + timedelta(days=req.days)
        else:
            new_expire = now + timedelta(days=req.days)
        
        cursor.execute('UPDATE web_users SET expire_at = ?, is_active = 1 WHERE id = ?', 
                      (new_expire.isoformat(), user_id))
        conn.commit()
        conn.close()
        
        add_membership_log(user_id, req.days, 'admin_gift', req.reason)
        
        return {"code": 200, "message": f"已赠送 {req.days} 天会员", "expire_at": new_expire.isoformat()}
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


@app.post("/api/admin/members/{user_id}/toggle")
async def toggle_member(user_id: int, user: dict = Depends(require_login)):
    """启用/禁用用户"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('SELECT is_active FROM web_users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {"code": 404, "message": "用户不存在"}
        
        new_status = 0 if row[0] else 1
        cursor.execute('UPDATE web_users SET is_active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        conn.close()
        
        return {"code": 200, "message": "已启用" if new_status else "已禁用", "is_active": new_status}
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


# ----- 卡密管理 -----

@app.get("/api/admin/cards")
async def get_cards(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    user: dict = Depends(require_login)
):
    """获取卡密列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # 构建查询
        where_clause = ""
        params = []
        if status == 'used':
            where_clause = "WHERE used_by IS NOT NULL"
        elif status == 'unused':
            where_clause = "WHERE used_by IS NULL"
        
        # 获取总数
        cursor.execute(f'SELECT COUNT(*) FROM card_keys {where_clause}', params)
        total = cursor.fetchone()[0]
        
        # 分页查询
        offset = (page - 1) * per_page
        cursor.execute(f'''
            SELECT c.id, c.card_key, c.duration_days, c.used_by, c.used_at, c.created_by, c.created_at,
                   u.username as used_by_username
            FROM card_keys c
            LEFT JOIN web_users u ON c.used_by = u.id
            {where_clause}
            ORDER BY c.created_at DESC
            LIMIT ? OFFSET ?
        ''', params + [per_page, offset])
        
        rows = cursor.fetchall()
        conn.close()
        
        cards = []
        for row in rows:
            cards.append({
                'id': row[0],
                'card_key': row[1],
                'duration_days': row[2],
                'used_by': row[3],
                'used_at': row[4],
                'created_by': row[5],
                'created_at': row[6],
                'used_by_username': row[7],
                'status': 'used' if row[3] else 'unused'
            })
        
        return {
            "code": 200,
            "cards": cards,
            "total": total,
            "page": page,
            "per_page": per_page
        }
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


class GenerateCardsRequest(BaseModel):
    count: int = 1
    duration_days: int = 30


@app.post("/api/admin/cards/generate")
async def generate_cards(req: GenerateCardsRequest, user: dict = Depends(require_login)):
    """生成卡密"""
    if req.count < 1 or req.count > 100:
        return {"code": 400, "message": "生成数量需在1-100之间"}
    
    if req.duration_days < 1:
        return {"code": 400, "message": "天数至少为1天"}
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cards = []
        for _ in range(req.count):
            key = generate_card_key()
            # 确保唯一性
            while True:
                cursor.execute('SELECT id FROM card_keys WHERE card_key = ?', (key,))
                if not cursor.fetchone():
                    break
                key = generate_card_key()
            
            cursor.execute('''
                INSERT INTO card_keys (card_key, duration_days, created_by)
                VALUES (?, ?, ?)
            ''', (key, req.duration_days, user.get('username', 'admin')))
            cards.append(key)
        
        conn.commit()
        conn.close()
        
        return {
            "code": 200,
            "message": f"成功生成 {req.count} 张卡密",
            "cards": cards
        }
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


# ----- 系统配置 -----

@app.get("/api/admin/member-config")
async def get_member_config(user: dict = Depends(require_login)):
    """获取会员系统配置"""
    config = {
        'enable_user_register': get_system_config('enable_user_register', 'true'),
        'require_email_verify': get_system_config('require_email_verify', 'false'),
        'checkin_points_mode': get_system_config('checkin_points_mode', 'random'),
        'checkin_points_fixed': get_system_config('checkin_points_fixed', '10'),
        'checkin_points_min': get_system_config('checkin_points_min', '5'),
        'checkin_points_max': get_system_config('checkin_points_max', '20'),
        'points_per_day': get_system_config('points_per_day', '100'),
    }
    return {"code": 200, "config": config}


@app.post("/api/admin/member-config")
async def save_member_config(request: Request, user: dict = Depends(require_login)):
    """保存会员系统配置"""
    try:
        data = await request.json()
        
        valid_keys = [
            'enable_user_register', 'require_email_verify', 'checkin_points_mode',
            'checkin_points_fixed', 'checkin_points_min', 'checkin_points_max', 'points_per_day'
        ]
        
        for key, value in data.items():
            if key in valid_keys:
                set_system_config(key, str(value))
        
        return {"code": 200, "message": "配置已保存"}
        
    except Exception as e:
        return {"code": 500, "message": str(e)}


# ----- 用户绑定已有 Emby -----

class BindEmbyRequest(BaseModel):
    emby_username: Optional[str] = None
    emby_password: Optional[str] = None


@app.post("/api/user/bind-emby")
async def bind_emby(request: Request, user: dict = Depends(require_login)):
    """绑定或开通 Emby 账号"""
    
    if user.get('emby_user_id'):
        return {"code": 400, "message": "已绑定 Emby 账号，无法重复绑定"}
    
    # 解析可选请求体
    emby_username = None
    emby_password = None
    try:
        body = await request.json()
        emby_username = body.get('emby_username')
        emby_password = body.get('emby_password')
    except:
        pass  # 无请求体 = 开通模式
    
    is_admin = user.get('role') == 'admin'
    
    def _save_binding(emby_user_id, emby_name):
        """保存 Emby 绑定到数据库"""
        conn = get_db()
        cursor = conn.cursor()
        if is_admin:
            # 管理员: 写入 user_bindings 表（profile页从这里读取）
            try:
                from bot.config import ADMIN_USER_ID
                cursor.execute('DELETE FROM user_bindings WHERE telegram_id = ?', (ADMIN_USER_ID,))
                cursor.execute('''
                    INSERT INTO user_bindings (telegram_id, emby_username, emby_password, emby_user_id)
                    VALUES (?, ?, ?, ?)
                ''', (ADMIN_USER_ID, emby_name, '', emby_user_id))
            except Exception as e:
                print(f"保存管理员 Emby 绑定失败: {e}")
        else:
            # 普通用户: 更新 web_users 表
            cursor.execute('''
                UPDATE web_users SET emby_user_id = ?, emby_username = ? WHERE username = ?
            ''', (emby_user_id, emby_name, user['username']))
        conn.commit()
        conn.close()
    
    try:
        if emby_username:
            # === 绑定已有 Emby 账号 ===
            from bot.services.emby import authenticate_emby_user
            result = await asyncio.to_thread(authenticate_emby_user, emby_username, emby_password or '')
            
            if not result or not result.get('success'):
                return {"code": 401, "message": "Emby 账号验证失败"}
            
            emby_user_id = result.get('user_id')
            _save_binding(emby_user_id, emby_username)
            return {"code": 200, "message": "绑定成功", "emby_username": emby_username}
        else:
            # === 开通新 Emby 账号 ===
            from bot.services.emby import create_emby_user
            username = user.get('username', 'user')
            result = await asyncio.to_thread(create_emby_user, username, username)
            
            if not result or not result.get('success'):
                error_msg = result.get('message', '开通失败') if result else '开通失败'
                # 如果用户已存在，尝试直接绑定
                if 'already exists' in str(error_msg):
                    from bot.services.emby import authenticate_emby_user
                    auth_result = await asyncio.to_thread(authenticate_emby_user, username, username)
                    if auth_result and auth_result.get('success'):
                        emby_user_id = auth_result.get('user_id')
                        _save_binding(emby_user_id, username)
                        return {"code": 200, "message": "绑定成功（用户已存在，已自动关联）", "emby_username": username}
                return {"code": 500, "message": error_msg}
            
            emby_user_id = result.get('user_id')
            _save_binding(emby_user_id, username)
            return {"code": 200, "message": "开通成功", "emby_username": username}
        
    except Exception as e:
        return {"code": 500, "message": f"操作失败: {str(e)}"}

@app.post("/api/user/unbind-emby")
async def unbind_emby(user: dict = Depends(require_login)):
    """解绑 Emby 账号"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if user.get('role') == 'admin':
            from bot.config import ADMIN_USER_ID
            cursor.execute('DELETE FROM user_bindings WHERE telegram_id = ?', (ADMIN_USER_ID,))
            
        cursor.execute('UPDATE web_users SET emby_user_id = NULL, emby_username = NULL WHERE username = ?', (user['username'],))
        conn.commit()
        conn.close()
        
        return {"code": 200, "message": "解绑成功"}
    except Exception as e:
        return {"code": 500, "message": f"解绑失败: {str(e)}"}


class ResetEmbyPasswordRequest(BaseModel):
    new_password: str

@app.post("/api/user/reset-emby-password")
async def reset_emby_password(req: ResetEmbyPasswordRequest, user: dict = Depends(require_login)):
    """重置 Emby 密码"""
    if not user['emby_user_id']:
        return {"code": 400, "message": "未绑定 Emby 账号"}
    
    try:
        from bot.services.emby import update_emby_password
        
        # 调用 Emby API 更新密码
        success = await asyncio.to_thread(update_emby_password, user['emby_user_id'], req.new_password)
        
        if success:
            return {"code": 200, "message": "Emby 密码重置成功"}
        else:
            return {"code": 500, "message": "Emby 密码重置失败"}
    except Exception as e:
        return {"code": 500, "message": f"重置失败: {str(e)}"}


class ResetPasswordRequest(BaseModel):
    password: str

@app.post("/api/users/{user_id}/reset_password")
async def reset_user_password(user_id: int, req: ResetPasswordRequest, user: dict = Depends(require_login)):
    """重置用户密码（管理员）"""
    if user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="需要管理员权限")
    
    try:
        from werkzeug.security import generate_password_hash
        password_hash = generate_password_hash(req.password)
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE web_users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "密码重置成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ----- 用户页面路由 -----

@app.get("/user/register")
async def user_register_page(request: Request):
    """用户注册页面"""
    return templates.TemplateResponse("user_register.html", {"request": request})


@app.get("/user/login")
async def user_login_page(request: Request):
    """用户登录页面"""
    return templates.TemplateResponse("user_login.html", {"request": request})


@app.get("/user/dashboard")
async def user_dashboard_page(request: Request, user_session_id: Optional[str] = Cookie(None)):
    """用户仪表盘页面"""
    user = await get_current_member(user_session_id)
    if not user:
        return RedirectResponse(url="/user/login", status_code=302)
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "user": user})


# 导入 timedelta
from datetime import timedelta

