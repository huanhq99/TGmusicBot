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

from fastapi import FastAPI, Request, HTTPException, Form, Query, Depends, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

# 路径配置
SCRIPT_DIR = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
MUSIC_TARGET_DIR = Path(os.environ.get('MUSIC_TARGET_DIR', SCRIPT_DIR / 'uploads'))
DATABASE_FILE = DATA_DIR / 'bot.db'
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

# Session 存储
sessions = {}


def hash_password(password: str) -> str:
    """哈希密码"""
    return hashlib.sha256(password.encode()).hexdigest()


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DATABASE_FILE))
    conn.row_factory = sqlite3.Row
    return conn


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
    
    conn.commit()
    conn.close()


async def get_current_user(session_id: Optional[str] = Cookie(None)):
    """验证登录状态"""
    if not WEB_PASSWORD:
        # 未设置密码，跳过验证（开发模式）
        return {"username": "admin", "role": "admin"}
    
    if not session_id or session_id not in sessions:
        return None
    return sessions[session_id]


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
    yield


app = FastAPI(
    title="TGmusicbot 管理界面",
    description="Telegram 音乐机器人管理",
    version="2.2.0",
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


# ============================================================
# API 路由
# ============================================================

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
            cursor.execute('SELECT COUNT(*) as cnt FROM song_requests WHERE status = ?', ('pending',))
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


@app.get("/api/users", response_model=List[UserBinding])
async def get_users():
    """获取绑定用户列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('SELECT telegram_id, emby_username, created_at FROM user_bindings ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        
        return [UserBinding(
            telegram_id=row['telegram_id'],
            emby_username=row['emby_username'],
            created_at=row['created_at']
        ) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/users/{telegram_id}")
async def delete_user(telegram_id: str):
    """删除用户绑定"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_bindings WHERE telegram_id = ?', (telegram_id,))
        conn.commit()
        conn.close()
        return {"status": "ok", "message": f"用户 {telegram_id} 已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def get_config():
    """获取配置信息"""
    ncm_cookie = os.environ.get('NCM_COOKIE', '')
    
    # 从数据库获取设置（优先）
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 获取数据库中的 Cookie（优先）
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_cookie',))
        row = cursor.fetchone()
        if row and row['value']:
            ncm_cookie = row['value']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_quality',))
        row = cursor.fetchone()
        ncm_quality = row['value'] if row else os.environ.get('NCM_QUALITY', 'exhigh')
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_download',))
        row = cursor.fetchone()
        auto_download = row['value'] == 'true' if row else os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'
        
        # 获取下载目录配置
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_mode',))
        row = cursor.fetchone()
        download_mode = row['value'] if row else 'local'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        download_dir = row['value'] if row else str(MUSIC_TARGET_DIR)
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('musictag_dir',))
        row = cursor.fetchone()
        musictag_dir = row['value'] if row else ''
        
        conn.close()
    except:
        ncm_quality = os.environ.get('NCM_QUALITY', 'exhigh')
        auto_download = os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'
        download_mode = 'local'
        download_dir = str(MUSIC_TARGET_DIR)
        musictag_dir = ''
    
    # 检查网易云登录状态
    ncm_status = {
        'configured': bool(ncm_cookie),
        'logged_in': False,
        'nickname': '',
        'is_vip': False
    }
    
    if ncm_cookie:
        try:
            from bot.ncm_downloader import NeteaseMusicAPI
            api = NeteaseMusicAPI(ncm_cookie)
            logged_in, info = api.check_login()
            if logged_in:
                ncm_status['logged_in'] = True
                ncm_status['nickname'] = info.get('nickname', '')
                ncm_status['is_vip'] = info.get('is_vip', False)
        except:
            pass
    
    # 获取 Emby 扫描间隔
    emby_scan_interval = int(os.environ.get('EMBY_SCAN_INTERVAL', '0'))
    try:
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
        row = cursor.fetchone()
        if row:
            emby_scan_interval = int(row['value'] if isinstance(row, dict) else row[0])
    except:
        pass
    
    return {
        "emby_url": EMBY_URL,
        "data_dir": str(DATA_DIR),
        "database": str(DATABASE_FILE),
        "cache_exists": LIBRARY_CACHE_FILE.exists(),
        "ncm_status": ncm_status,
        "ncm_quality": ncm_quality,
        "auto_download": auto_download,
        "download_mode": download_mode,
        "download_dir": download_dir,
        "musictag_dir": musictag_dir,
        "emby_scan_interval": emby_scan_interval
    }


@app.post("/api/ncm/check")
async def check_ncm_cookie(cookie: str = Form(...)):
    """检查网易云 Cookie 是否有效"""
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(cookie)
        logged_in, info = api.check_login()
        
        if logged_in:
            return {
                "status": "ok",
                "logged_in": True,
                "nickname": info.get('nickname', ''),
                "user_id": info.get('user_id'),
                "is_vip": info.get('is_vip', False),
                "vip_type": info.get('vip_type', 0)
            }
        else:
            return {"status": "error", "message": "Cookie 无效或已过期"}
    except ImportError:
        return {"status": "error", "message": "下载模块未安装，请安装 pycryptodome 和 mutagen"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/settings/ncm")
async def save_ncm_settings(
    ncm_quality: str = Form(...),
    auto_download: bool = Form(False),
    download_mode: str = Form('local'),
    download_dir: str = Form(''),
    musictag_dir: str = Form(''),
    emby_scan_interval: int = Form(0)
):
    """保存网易云下载设置到数据库"""
    try:
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
                      ('auto_download', 'true' if auto_download else 'false'))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('download_mode', download_mode))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('download_dir', download_dir))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('musictag_dir', musictag_dir))
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                      ('emby_scan_interval', str(emby_scan_interval)))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "设置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/settings/ncm")
async def get_ncm_settings():
    """获取网易云下载设置"""
    try:
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
        
        # 获取设置（优先从数据库，否则从环境变量）
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_quality',))
        row = cursor.fetchone()
        ncm_quality = row['value'] if row else os.environ.get('NCM_QUALITY', 'exhigh')
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_download',))
        row = cursor.fetchone()
        auto_download = row['value'] == 'true' if row else os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'
        
        conn.close()
        
        return {
            "ncm_quality": ncm_quality,
            "auto_download": auto_download
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 网易云二维码登录 API
# ============================================================

# 存储当前二维码登录状态
qr_login_state = {}

@app.post("/api/ncm/qr/create")
async def ncm_qr_create():
    """创建网易云二维码登录"""
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI()
        success, data = api.qr_login_create()
        
        print(f"[QR Create] success={success}, data_keys={list(data.keys()) if isinstance(data, dict) else data}")
        
        if success:
            unikey = data['unikey']
            # 存储 API 实例用于后续检查
            qr_login_state[unikey] = {
                'api': api,
                'created_at': datetime.now().isoformat()
            }
            print(f"[QR Create] 成功创建二维码, unikey={unikey[:20]}..., 当前 state 数量={len(qr_login_state)}")
            return {
                "status": "ok",
                "unikey": unikey,
                "qr_url": data['qr_url'],
                "qr_img": data['qr_img']
            }
        else:
            print(f"[QR Create] 创建失败: {data.get('error', '未知错误')}")
            return {"status": "error", "message": data.get('error', '创建二维码失败')}
    except ImportError as e:
        print(f"[QR Create] 导入模块失败: {e}")
        return {"status": "error", "message": "下载模块未安装"}
    except Exception as e:
        import traceback
        print(f"[QR Create] 异常: {e}")
        print(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@app.post("/api/ncm/qr/check")
async def ncm_qr_check(unikey: str = Form(...)):
    """检查二维码扫描状态"""
    print(f"[QR Check] 收到请求, unikey={unikey[:20]}...")
    try:
        if unikey not in qr_login_state:
            print(f"[QR Check] unikey 不在 qr_login_state 中! 现有 keys: {list(qr_login_state.keys())}")
            return {"status": "error", "code": 800, "message": "二维码已失效，请重新获取"}
        
        api = qr_login_state[unikey]['api']
        code, data = api.qr_login_check(unikey)
        
        # 调试日志
        print(f"[QR Check] 返回 code={code}, message={data.get('message', '')}")
        
        result = {
            "status": "ok" if code in [801, 802, 803] else "error",
            "code": code,
            "message": data.get('message', '')
        }
        
        if code == 803:
            # 登录成功
            cookie = data.get('cookie', '')
            cookies_dict = data.get('cookies_dict', {})
            has_music_u = 'MUSIC_U' in cookies_dict or 'MUSIC_U' in cookie
            
            print(f"[QR Check] 登录成功!")
            print(f"[QR Check] Cookie长度={len(cookie)}")
            print(f"[QR Check] 包含MUSIC_U={has_music_u}")
            print(f"[QR Check] cookies_dict keys={list(cookies_dict.keys())}")
            
            result['cookie'] = cookie
            result['logged_in'] = True
            
            # 验证登录并获取用户信息
            logged_in, user_info = api.check_login()
            print(f"[QR Check] 验证登录结果: logged_in={logged_in}, user_info={user_info}")
            
            if logged_in:
                result['nickname'] = user_info.get('nickname', '')
                result['is_vip'] = user_info.get('is_vip', False)
            else:
                # 即使验证失败，也尝试保存 cookie（可能是临时问题）
                print(f"[QR Check] 验证失败，但仍保存 cookie")
                result['nickname'] = '未知用户'
                result['is_vip'] = False
            
            # 自动保存 Cookie 到数据库
            if cookie:
                try:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS bot_settings (
                            key TEXT PRIMARY KEY,
                            value TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    cursor.execute('''
                        INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
                    ''', ('ncm_cookie', cookie))
                    conn.commit()
                    conn.close()
                    result['cookie_saved'] = True
                    print(f"[QR Check] Cookie 已保存到数据库")
                except Exception as e:
                    result['cookie_saved'] = False
                    result['save_error'] = str(e)
                    print(f"[QR Check] 保存 Cookie 失败: {e}")
            
            # 清理状态
            del qr_login_state[unikey]
            
        elif code == 802:
            print(f"[QR Check] 已扫描，等待确认")
            
        elif code == 800:
            print(f"[QR Check] 二维码过期")
            # 二维码过期，清理状态
            if unikey in qr_login_state:
                del qr_login_state[unikey]
        
        return result
    except Exception as e:
        import traceback
        print(f"[QR Check] 异常: {e}")
        print(traceback.format_exc())
        return {"status": "error", "code": -1, "message": str(e)}


@app.post("/api/ncm/cookie/save")
async def save_ncm_cookie(cookie: str = Form(...)):
    """保存网易云 Cookie 到数据库"""
    try:
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
        
        # 保存 Cookie
        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)',
                      ('ncm_cookie', cookie, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        # 同时更新环境变量（当前进程）
        os.environ['NCM_COOKIE'] = cookie
        
        return {"status": "ok", "message": "Cookie 已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


# ============================================================
# 登录认证 API
# ============================================================

@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    """登录"""
    if not WEB_PASSWORD:
        return {"status": "error", "message": "未配置 WEB_PASSWORD，请在环境变量中设置"}
    
    if username == WEB_USERNAME and password == WEB_PASSWORD:
        session_id = secrets.token_hex(32)
        sessions[session_id] = {"username": username, "role": "admin"}
        response.set_cookie(key="session_id", value=session_id, httponly=True, max_age=86400*7)
        return {"status": "ok", "message": "登录成功"}
    else:
        raise HTTPException(status_code=401, detail="用户名或密码错误")


@app.post("/api/logout")
async def logout(response: Response, session_id: Optional[str] = Cookie(None)):
    """登出"""
    if session_id and session_id in sessions:
        del sessions[session_id]
    response.delete_cookie("session_id")
    return {"status": "ok", "message": "已登出"}


@app.get("/api/auth/status")
async def auth_status(session_id: Optional[str] = Cookie(None)):
    """检查登录状态"""
    user = await get_current_user(session_id)
    if user:
        return {"logged_in": True, "username": user["username"], "role": user["role"]}
    return {"logged_in": False, "need_password": bool(WEB_PASSWORD)}


# ============================================================
# 歌曲申请管理 API
# ============================================================

@app.get("/api/requests")
async def get_song_requests(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_login)
):
    """获取歌曲补全申请列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        offset = (page - 1) * per_page
        
        if status:
            cursor.execute('''
                SELECT * FROM song_requests 
                WHERE status = ? 
                ORDER BY created_at DESC 
                LIMIT ? OFFSET ?
            ''', (status, per_page, offset))
        else:
            cursor.execute('''
                SELECT * FROM song_requests 
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
    """批准歌曲申请"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE song_requests 
            SET status = ?, admin_note = ?, processed_at = ? 
            WHERE id = ?
        ''', ('approved', note, datetime.now().isoformat(), request_id))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok", "message": "申请已批准"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/requests/{request_id}/reject")
async def reject_request(
    request_id: int,
    note: str = Form(""),
    user: dict = Depends(require_login)
):
    """拒绝歌曲申请"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE song_requests 
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
    """删除歌曲申请"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM song_requests WHERE id = ?', (request_id,))
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session_id: Optional[str] = Cookie(None)):
    """首页仪表盘"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/playlists", response_class=HTMLResponse)
async def playlists_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """歌单记录页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("playlists.html", {"request": request})


@app.get("/uploads", response_class=HTMLResponse)
async def uploads_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """上传记录页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("uploads.html", {"request": request})


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """用户管理页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("users.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """设置页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, session_id: Optional[str] = Cookie(None)):
    """歌曲申请管理页"""
    user = await get_current_user(session_id)
    if WEB_PASSWORD and not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("requests.html", {"request": request})


# ============================================================
# 启动服务
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
