#!/usr/bin/env python3
"""
TGmusicbot - Telegram Bot for Music Management
功能：歌单同步到 Emby + 音乐上传到 NAS
"""

import logging
import os
import json
import time
import re
import html
import sqlite3
import asyncio
import shutil
from typing import List, Dict, Optional, Any, Union
import datetime as dt
from datetime import datetime, timedelta
from urllib.parse import urljoin
from pathlib import Path
from cryptography.fernet import Fernet

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rapidfuzz import fuzz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, InlineQueryHandler

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

# --- 全局配置 ---
APP_NAME = "TGmusicbot"
APP_VERSION = "1.10.9"
EMBY_CLIENT_NAME = "TGmusicbot"
DEVICE_ID = "TGmusicbot_Device_v2"

# 路径配置
SCRIPT_DIR = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/tmp/tgmusicbot_uploads'))
MUSIC_TARGET_DIR = Path(os.environ.get('MUSIC_TARGET_DIR', SCRIPT_DIR / 'uploads'))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_TARGET_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_FILE = DATA_DIR / 'bot.db'
LIBRARY_CACHE_FILE = DATA_DIR / 'library_cache.json'
LOG_FILE = DATA_DIR / f'bot_{datetime.now().strftime("%Y%m%d")}.log'

# 环境变量配置
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_API_URL = os.environ.get('TELEGRAM_API_URL', '')  # Local Bot API Server URL, e.g. http://localhost:8081/bot
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')
EMBY_URL = os.environ.get('EMBY_URL')
EMBY_USERNAME = os.environ.get('EMBY_USERNAME')
EMBY_PASSWORD = os.environ.get('EMBY_PASSWORD')

# Emby Webhook 通知开关
EMBY_WEBHOOK_NOTIFY = os.environ.get('EMBY_WEBHOOK_NOTIFY', 'true').lower() == 'true'
MAKE_PLAYLIST_PUBLIC = os.environ.get('MAKE_PLAYLIST_PUBLIC', 'false').lower() == 'true'

# 网易云/QQ音乐下载配置
NCM_COOKIE = os.environ.get('NCM_COOKIE', '')  # 网易云登录 Cookie
QQ_COOKIE = os.environ.get('QQ_COOKIE', '')  # QQ音乐登录 Cookie
NCM_QUALITY = os.environ.get('NCM_QUALITY', 'exhigh')  # 下载音质: standard/higher/exhigh/lossless/hires
AUTO_DOWNLOAD = os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'  # 是否自动下载缺失歌曲

# 国内代理服务配置（用于海外 VPS 下载 QQ/网易云音乐）
MUSIC_PROXY_URL = os.environ.get('MUSIC_PROXY_URL', '')  # 如 http://国内IP:8899
MUSIC_PROXY_KEY = os.environ.get('MUSIC_PROXY_KEY', '')  # 代理 API Key

# 定时扫描 Emby 媒体库（小时，0 表示禁用）
EMBY_SCAN_INTERVAL = int(os.environ.get('EMBY_SCAN_INTERVAL', '0'))

# Pyrogram 配置（大文件上传支持，可选）
TG_API_ID = os.environ.get('TG_API_ID', '')
TG_API_HASH = os.environ.get('TG_API_HASH', '')

# 允许上传的音频格式
ALLOWED_AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma', '.alac', '.aiff', '.dsd', '.dsf', '.dff')

# Pyrogram 客户端（用于接收大文件）
pyrogram_client = None


def get_ncm_cookie():
    """获取网易云 Cookie（优先从数据库读取）"""
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_cookie',))
            row = cursor.fetchone()
            if row:
                # row 可能是 tuple 或 dict
                value = row['value'] if isinstance(row, dict) else row[0]
                if value:
                    return value
    except Exception as e:
        logger.error(f"读取 ncm_cookie 失败: {e}")
    return NCM_COOKIE  # 回退到环境变量


def get_qq_cookie():
    """获取 QQ音乐 Cookie（优先从数据库读取）"""
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('qq_cookie',))
            row = cursor.fetchone()
            if row:
                # row 可能是 tuple 或 dict
                value = row['value'] if isinstance(row, dict) else row[0]
                if value:
                    return value
    except Exception as e:
        logger.error(f"读取 qq_cookie 失败: {e}")
    return QQ_COOKIE  # 回退到环境变量


# 下载管理器（全局实例）
from bot.download_manager import DownloadManager, init_download_manager as _init_dm, get_download_manager
from bot.ncm_downloader import NeteaseMusicAPI

download_manager = None


# 加密密钥
ENCRYPTION_KEY = os.environ.get('PLAYLIST_BOT_KEY')
if not ENCRYPTION_KEY:
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    print(f"警告：未设置 PLAYLIST_BOT_KEY，已生成新密钥：{ENCRYPTION_KEY}")

fernet = Fernet(ENCRYPTION_KEY.encode())

# API 端点
QQ_API_GET_PLAYLIST_URL = "http://i.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
NCM_API_PLAYLIST_DETAIL_URL = "https://music.163.com/api/v3/playlist/detail"
NCM_API_SONG_DETAIL_URL = "https://music.163.com/api/song/detail/"

# 匹配参数
MATCH_THRESHOLD = 9
EMBY_SCAN_PAGE_SIZE = 2000
EMBY_PLAYLIST_ADD_BATCH_SIZE = 10

# --- 全局状态 ---
emby_library_data = []
emby_auth = {'access_token': None, 'user_id': None}
database_conn = None
requests_session = None
ncm_downloader = None  # 网易云下载器实例

# 搜索缓存（避免重复 API 调用）
_cmd_search_cache = {}  # {(platform, keyword): (timestamp, results)}
_cmd_search_cache_ttl = 180  # 3分钟

# 歌单同步调度配置
DEFAULT_PLAYLIST_SYNC_INTERVAL_MINUTES = max(
    1,
    int(os.environ.get('PLAYLIST_SYNC_INTERVAL', os.environ.get('PLAYLIST_SYNC_INTERVAL_MINUTES', '360')))
)
MIN_PLAYLIST_SYNC_INTERVAL_MINUTES = max(1, int(os.environ.get('PLAYLIST_SYNC_MIN_INTERVAL', '1')))
PLAYLIST_SYNC_POLL_INTERVAL_SECONDS = max(30, int(os.environ.get('PLAYLIST_SYNC_POLL_INTERVAL', '60')))
PLAYLIST_SYNC_INITIAL_DELAY_SECONDS = max(0, int(os.environ.get('PLAYLIST_SYNC_INITIAL_DELAY', '10')))


# ============================================================
# 进度条工具函数
# ============================================================

def make_progress_bar(current: int, total: int, width: int = 10) -> str:
    """
    生成文本进度条
    
    Args:
        current: 当前进度
        total: 总数
        width: 进度条宽度（字符数）
        
    Returns:
        进度条字符串，如 "▓▓▓▓▓░░░░░ 50%"
    """
    if total <= 0:
        return "░" * width + " 0%"
    
    percent = min(current / total, 1.0)
    filled = int(width * percent)
    empty = width - filled
    
    bar = "▓" * filled + "░" * empty
    percent_text = f"{int(percent * 100)}%"
    
    return f"{bar} {percent_text}"


def make_progress_message(title: str, current: int, total: int, 
                          current_item: str = "", extra_info: str = "") -> str:
    """
    生成完整的进度消息
    
    Args:
        title: 标题（如 📥 下载中）
        current: 当前进度
        total: 总数
        current_item: 当前处理的项目名称
        extra_info: 额外信息
        
    Returns:
        格式化的进度消息
    """
    bar = make_progress_bar(current, total)
    msg = f"{title}\n\n{bar}\n📊 {current}/{total}"
    
    if current_item:
        # 截断过长的项目名
        if len(current_item) > 35:
            current_item = current_item[:32] + "..."
        msg += f"\n\n🎵 `{current_item}`"
    
    if extra_info:
        msg += f"\n\n{extra_info}"
    
    return msg


def ensure_bot_settings_table():
    """Ensure bot_settings table exists before accessing it."""
    if not database_conn:
        return
    try:
        cursor = database_conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        database_conn.commit()
    except Exception as exc:
        logger.error(f"初始化 bot_settings 表失败: {exc}")


def escape_markdown(text: str) -> str:
    """
    转义 Telegram Markdown 特殊字符
    
    Args:
        text: 原始文本
        
    Returns:
        转义后的文本
    """
    if not text:
        return ''
    # Markdown 特殊字符: _ * [ ] ( ) ~ ` > # + - = | { } . !
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


async def start_pyrogram_client():
    """启动 Pyrogram 客户端用于接收大文件"""
    global pyrogram_client
    
    if not TG_API_ID or not TG_API_HASH:
        logger.info("未配置 TG_API_ID/TG_API_HASH，大文件上传功能未启用")
        return
    
    try:
        from pyrogram import Client, filters as pyro_filters
        from pyrogram.handlers import MessageHandler as PyroMessageHandler
        
        # 创建 Pyrogram 客户端（Bot 模式）
        pyrogram_client = Client(
            name="tgmusicbot_pyrogram",
            api_id=int(TG_API_ID),
            api_hash=TG_API_HASH,
            bot_token=TELEGRAM_TOKEN,
            workdir=str(DATA_DIR)
        )
        
        @pyrogram_client.on_message(pyro_filters.audio | pyro_filters.document)
        async def handle_large_file(client, message):
            """处理大文件上传（Pyrogram）"""
            user_id = str(message.from_user.id)
            
            # 获取文件信息
            if message.audio:
                file = message.audio
                original_name = file.file_name or f"{file.title or 'audio'}.mp3"
                file_size = file.file_size or 0
            elif message.document:
                file = message.document
                original_name = file.file_name or "unknown"
                mime = file.mime_type or ""
                # 只处理音频文件
                if not (mime.startswith('audio/') or original_name.lower().endswith(ALLOWED_AUDIO_EXTENSIONS)):
                    return
                file_size = file.file_size or 0
            else:
                return
            
            # 只处理大于 20MB 的文件，小文件由 python-telegram-bot 处理
            if file_size <= 20 * 1024 * 1024:
                return
            
            try:
                status_msg = await message.reply_text(f"📥 正在下载大文件: {original_name} ({file_size / 1024 / 1024:.1f} MB)...")
                
                # 获取下载设置
                ncm_settings = get_ncm_settings()
                download_mode = ncm_settings.get('download_mode', 'local')
                download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
                musictag_dir = ncm_settings.get('musictag_dir', '')
                
                # 确保目录存在
                download_path = Path(download_dir)
                download_path.mkdir(parents=True, exist_ok=True)
                
                # 使用 Pyrogram 下载大文件
                temp_path = UPLOAD_DIR / original_name
                await message.download(file_name=str(temp_path))
                
                # 清理文件名并移动到下载目录
                clean_name = clean_filename(original_name)
                target_path = download_path / clean_name
                
                if target_path.exists():
                    target_path.unlink()
                
                shutil.move(str(temp_path), str(target_path))
                
                # 如果是 MusicTag 模式
                final_path = target_path
                if download_mode == 'musictag' and musictag_dir:
                    musictag_path = Path(musictag_dir)
                    musictag_path.mkdir(parents=True, exist_ok=True)
                    final_dest = musictag_path / clean_name
                    shutil.move(str(target_path), str(final_dest))
                    final_path = final_dest
                    logger.info(f"已移动大文件到 MusicTag: {clean_name}")
                
                # 记录
                save_upload_record(user_id, original_name, clean_name, file_size)
                
                size_mb = file_size / 1024 / 1024
                if download_mode == 'musictag' and musictag_dir:
                    await status_msg.edit_text(f"✅ 大文件上传成功！\n\n📁 文件: `{clean_name}`\n📦 大小: {size_mb:.2f} MB\n📂 已转移到 MusicTag 目录")
                else:
                    await status_msg.edit_text(f"✅ 大文件上传成功！\n\n📁 文件: `{clean_name}`\n📦 大小: {size_mb:.2f} MB\n📂 保存位置: {download_path}")
                
                logger.info(f"用户 {user_id} 上传大文件: {clean_name} ({size_mb:.2f} MB)")
                
            except Exception as e:
                logger.error(f"大文件上传失败: {e}")
                await message.reply_text(f"❌ 上传失败: {str(e)}")
        
        await pyrogram_client.start()
        logger.info("✅ Pyrogram 客户端已启动，大文件上传功能已启用 (最大 2GB)")
        
    except ImportError:
        logger.warning("Pyrogram 未安装，大文件上传功能不可用")
    except Exception as e:
        logger.error(f"Pyrogram 启动失败: {e}")


# --- 日志设置 ---
# 使用 TimedRotatingFileHandler 实现每天自动轮转
from logging.handlers import TimedRotatingFileHandler

# 主日志文件（不带日期后缀，由 handler 自动轮转）
MAIN_LOG_FILE = DATA_DIR / 'bot.log'

# 创建按天轮转的 handler，保留最近 30 天日志
file_handler = TimedRotatingFileHandler(
    MAIN_LOG_FILE,
    when='midnight',
    interval=1,
    backupCount=30,
    encoding='utf-8'
)
file_handler.suffix = '%Y%m%d.log'  # 轮转后的文件名格式：bot.log.20260111.log
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        file_handler,
        logging.StreamHandler()
    ]
)

# 添加 Redis 日志 Handler
try:
    from bot.utils.redis_client import get_redis, RedisLogHandler
    redis_client = get_redis()
    if redis_client.connected:
        redis_handler = RedisLogHandler()
        redis_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(redis_handler)
        print("Redis 日志 Handler 已添加")
except Exception as e:
    print(f"Redis 日志 Handler 初始化跳过: {e}")
logger = logging.getLogger(__name__)

# 降低第三方库的日志级别，避免刷屏
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ============================================================
# 工具函数
# ============================================================

def create_requests_session():
    session = requests.Session()
    retry_strategy = Retry(total=3, status_forcelist=[429, 500, 502, 503, 504], 
                          allowed_methods=["HEAD", "GET", "POST", "DELETE"], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def strip_jsonp(jsonp_str):
    match = re.match(r'^[^{]*\(({.*?})\)[^}]*$', jsonp_str.strip())
    return match.group(1) if match else jsonp_str

def encrypt_password(password):
    return fernet.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password):
    try:
        return fernet.decrypt(encrypted_password.encode()).decode()
    except Exception:
        # 解密失败，可能是旧 key 加密的，返回原文（假设是明文）
        logger.warning("密码解密失败，可能需要重新绑定账号")
        return encrypted_password

def _normalize_artists(artist_str: str) -> set:
    if not isinstance(artist_str, str): return set()
    s = artist_str.lower()
    s = re.sub(r'\s*[\(（].*?[\)）]', '', s)
    s = re.sub(r'\s*[\[【].*?[\]】]', '', s)
    s = re.sub(r'\s+(feat|ft|with|vs|presents|pres\.|starring)\.?\s+', '/', s)
    s = re.sub(r'\s*&\s*', '/', s)
    return {artist.strip() for artist in re.split(r'\s*[/•,、;&|]\s*', s) if artist.strip()}

def _get_title_lookup_key(title: str) -> str:
    if not isinstance(title, str): return ""
    key = title.lower()
    key = re.sub(r'\s*[\(（【\[].*?[\)）】\]]', '', key).strip()
    return key

def _resolve_short_url(url: str) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'}
        response = requests_session.get(url, headers=headers, timeout=(10, 20), allow_redirects=True)
        if response.url != url:
            logger.info(f"短链接解析: {url} -> {response.url}")
        return response.url
    except:
        return url

def clean_filename(name: str) -> str:
    """清理文件名"""
    name = re.sub(r'^\d+\s*[-_. ]+\s*', '', name)
    name = re.sub(r'[_]+', ' ', name)
    name = re.sub(r'\s*\(\d+\)\s*', '', name)
    # 移除非法字符
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip()


# ============================================================
# Emby API
# ============================================================

def authenticate_emby(base_url, username, password):
    api_url = urljoin(base_url, "/emby/Users/AuthenticateByName")
    auth_header = f'Emby Client="{EMBY_CLIENT_NAME}", Device="Docker", DeviceId="{DEVICE_ID}", Version="{APP_VERSION}"'
    headers = {
        'X-Emby-Authorization': auth_header,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    try:
        response = requests_session.post(api_url, params={'format': 'json'},
                                        json={"Username": username, "Pw": password},
                                        headers=headers, timeout=(10, 20))
        response.raise_for_status()
        data = response.json()
        if data and 'AccessToken' in data and 'User' in data:
            logger.info(f"Emby 认证成功: {username}")
            return data['AccessToken'], data['User']['Id']
    except requests.RequestException as e:
        logger.error(f"Emby 认证失败: {e}")
    return None, None

def call_emby_api(endpoint, params=None, method='GET', data=None, user_auth=None, timeout=(15, 60)):
    auth = user_auth or emby_auth
    access_token = auth.get('access_token')
    user_id = auth.get('user_id')
    if not access_token or not user_id:
        return None
    
    api_url = urljoin(EMBY_URL, f"/emby/{endpoint.lstrip('/')}")
    auth_header = f'Emby UserId="{user_id}", Client="{EMBY_CLIENT_NAME}", Device="Docker", DeviceId="{DEVICE_ID}", Version="{APP_VERSION}", Token="{access_token}"'
    headers = {
        'X-Emby-Authorization': auth_header,
        'X-Emby-Token': access_token,
        'Accept': 'application/json'
    }
    query_params = {'format': 'json', **(params or {})}
    
    try:
        if method.upper() == 'GET':
            response = requests_session.get(api_url, params=query_params, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            headers['Content-Type'] = 'application/json'
            response = requests_session.post(api_url, params=query_params, json=data, headers=headers, timeout=timeout)
        elif method.upper() == 'DELETE':
            response = requests_session.delete(api_url, params=query_params, headers=headers, timeout=timeout)
        else:
            return None
        
        if response.status_code == 204:
            return {"status": "ok"}
        response.raise_for_status()
        try:
            return response.json()
        except:
            return {"status": "ok"}
    except requests.RequestException as e:
        logger.error(f"Emby API ({endpoint}) 失败: {e}")
        return None


def trigger_emby_library_scan(user_auth=None):
    """触发 Emby 媒体库扫描"""
    try:
        # 刷新整个媒体库
        result = call_emby_api("Library/Refresh", method='POST', user_auth=user_auth)
        if result:
            logger.info("已触发 Emby 媒体库扫描")
            return True
        return False
    except Exception as e:
        logger.error(f"触发 Emby 扫库失败: {e}")
        return False


# ============================================================
# 媒体库扫描
# ============================================================

def scan_emby_library(save_to_cache=True, user_id=None, access_token=None):
    global emby_library_data
    logger.info("开始扫描 Emby 媒体库...")
    scanned_songs = []
    start_index = 0
    
    scan_user_id = user_id or emby_auth['user_id']
    scan_access_token = access_token or emby_auth['access_token']
    if not scan_user_id or not scan_access_token:
        return []
    
    temp_auth = {'user_id': scan_user_id, 'access_token': scan_access_token}
    
    while True:
        params = {
            'IncludeItemTypes': 'Audio', 'Recursive': 'true',
            'Limit': EMBY_SCAN_PAGE_SIZE, 'StartIndex': start_index,
            'Fields': 'Id,Name,ArtistItems,Album,AlbumArtist'  # 添加 Album 字段
        }
        response = call_emby_api(f"Users/{scan_user_id}/Items", params, user_auth=temp_auth, timeout=(15, 180))
        
        if response and 'Items' in response:
            items = response['Items']
            if not items: break
            for item in items:
                artists = "/".join([a.get('Name', '') for a in item.get('ArtistItems', [])])
                album = item.get('Album', '') or item.get('AlbumArtist', '')  # 获取专辑名
                scanned_songs.append({
                    'id': str(item.get('Id')),
                    'title': html.unescape(item.get('Name', '')),
                    'artist': html.unescape(artists),
                    'album': html.unescape(album) if album else ''  # 保存专辑名
                })
            logger.info(f"已扫描 {len(scanned_songs)} 首歌曲...")
            if len(items) < EMBY_SCAN_PAGE_SIZE: break
            start_index += EMBY_SCAN_PAGE_SIZE
        else:
            break
    
    emby_library_data = scanned_songs
    logger.info(f"扫描完成，共 {len(emby_library_data)} 首歌曲")
    
    if save_to_cache:
        try:
            with open(LIBRARY_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(emby_library_data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")
    
    return emby_library_data


def get_user_emby_playlists(user_auth):
    if not user_auth: return []
    params = {'IncludeItemTypes': 'Playlist', 'Recursive': 'true', 'Fields': 'Id,Name'}
    response = call_emby_api(f"Users/{user_auth['user_id']}/Items", params, user_auth=user_auth)
    if response and 'Items' in response:
        return [{'id': p.get('Id'), 'name': p.get('Name')} for p in response['Items']]
    return []

def delete_emby_playlist(playlist_id, user_auth):
    return call_emby_api(f"Items/{playlist_id}", {}, method='DELETE', user_auth=user_auth) is not None


# ============================================================
# 歌单解析
# ============================================================

def parse_playlist_input(input_str: str):
    input_str = input_str.strip()
    url_match = re.search(r'https?://\S+', input_str)
    url = url_match.group(0) if url_match else input_str
    
    if '163cn.tv' in url or 'c6.y.qq.com' in url:
        url = _resolve_short_url(url)
    
    # 网易云
    for pattern in [r"music\.163\.com.*[?&/#]id=(\d+)", r"music\.163\.com/playlist/(\d+)"]:
        match = re.search(pattern, url)
        if match: return "netease", match.group(1)
    
    # QQ音乐
    for pattern in [r"y\.qq\.com/n/ryqq/playlist/(\d+)", r"(?:y|i)\.qq\.com/.*?[?&](id|dissid)=(\d+)"]:
        match = re.search(pattern, url)
        if match:
            return "qq", match.group(2) if len(match.groups()) > 1 and match.group(2) else match.group(1)
    
    # Spotify
    for pattern in [r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)", r"spotify:playlist:([a-zA-Z0-9]+)"]:
        match = re.search(pattern, url)
        if match: return "spotify", match.group(1)
    
    return None, None


def extract_playlist_id(playlist_url: str, platform: str) -> str:
    """从歌单 URL 中提取 ID"""
    playlist_type, playlist_id = parse_playlist_input(playlist_url)
    if playlist_type == platform or (platform == 'netease' and playlist_type == 'ncm'):
        return playlist_id
    return None

def get_qq_playlist_details(playlist_id):
    qq_cookie = get_qq_cookie()
    params = {'type': 1, 'utf8': 1, 'disstid': playlist_id, 'loginUin': 0, '_': int(time.time() * 1000)}
    headers = {'Referer': 'https://y.qq.com/', 'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    if qq_cookie: headers['Cookie'] = qq_cookie
    try:
        response = requests_session.get(QQ_API_GET_PLAYLIST_URL, params=params, headers=headers, timeout=(10, 15))
        response.raise_for_status()
        data = json.loads(strip_jsonp(response.text))
        if not data or 'cdlist' not in data or not data['cdlist']:
            return None, []
        playlist = data['cdlist'][0]
        name = html.unescape(playlist.get('dissname', f"QQ歌单{playlist_id}"))
        songs = []
        for s in playlist.get('songlist', []):
            if s:
                artists = "/".join([a.get('name', '') for a in s.get('singer', [])])
                # 获取专辑信息
                album = s.get('albumname', '') or s.get('album', {}).get('name', '')
                songs.append({
                    'source_id': str(s.get('songid') or s.get('id')),
                    'title': html.unescape(s.get('songname') or s.get('title', '')),
                    'artist': html.unescape(artists),
                    'album': html.unescape(album) if album else '',
                    'coverUrl': f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{s.get('albummid') or s.get('album', {}).get('mid')}.jpg" if (s.get('albummid') or s.get('album', {}).get('mid')) else '',
                    'platform': 'QQ'
                })
        return name, songs
    except Exception as e:
        logger.error(f"获取 QQ 歌单失败: {e}")
        return None, []

def get_ncm_playlist_details(playlist_id):
    try:
        ncm_cookie = get_ncm_cookie()
        # 使用 EAPI 获取准确的歌单详情 (能获取完整列表，不管 Cookie 是否过期，EAPI 通常比 V3 API 更准确)
        api = NeteaseMusicAPI(ncm_cookie)
        playlist_data = api.get_playlist_detail(playlist_id)
        
        if not playlist_data or not playlist_data.get('playlist'):
            logger.warning(f"EAPI 获取歌单失败，尝试回退到旧 API: {playlist_id}")
            # Fallback to old method if EAPI fails
            headers = {'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
            if ncm_cookie: headers['Cookie'] = ncm_cookie
            response = requests_session.get(NCM_API_PLAYLIST_DETAIL_URL, 
                                            params={'id': playlist_id, 'n': 100000, 'timestamp': int(time.time() * 1000)},
                                            headers=headers, timeout=(10, 20))
            if response.status_code != 200: return None, []
            playlist = response.json().get('playlist')
        else:
            playlist = playlist_data.get('playlist')
            
        if not playlist: return None, []
        
        logger.info(f"DEBUG NCM Playlist [{playlist_id}] fetched: {len(playlist.get('trackIds', []))} tracks (API: {'EAPI' if playlist_data else 'V3'})")
        name = html.unescape(playlist.get('name', f"网易云歌单{playlist_id}"))
        track_ids = [str(t['id']) for t in playlist.get('trackIds', [])]
        
        # 获取歌曲详情 (Batch)
        # 注意: 歌曲详情其实也可以用 EAPI (/api/v3/song/detail) 获取，但旧 API 似乎够用且不限制
        # 为了保险，trackIds 拿到了 912 个，只要 song/detail 能查到就行
        songs = []
        headers = {'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0'}
        if ncm_cookie: headers['Cookie'] = ncm_cookie
        
        for i in range(0, len(track_ids), 200):
            batch_ids = track_ids[i:i + 200]
            try:
                # 尝试用 EAPI 批量获取详情? NeteaseMusicAPI 还没有批量获取详情的方法
                # 暂时保留旧 API，因为 ids 参数传过去了，一般都能查到 (除了被下架的)
                detail_response = requests_session.get(NCM_API_SONG_DETAIL_URL,
                                                       params={'ids': f"[{','.join(batch_ids)}]"},
                                                       headers=headers, timeout=(10, 15))
                if detail_response.status_code == 200:
                    for s in detail_response.json().get('songs', []):
                        artist_list = s.get('ar') or s.get('artists') or []
                        artists = "/".join([a.get('name', '') for a in artist_list])
                        # 获取专辑信息
                        album_info = s.get('al') or s.get('album') or {}
                        album = album_info.get('name', '') if isinstance(album_info, dict) else ''
                        songs.append({
                            'source_id': str(s.get('id')),
                            'title': html.unescape(s.get('name', '')),
                            'artist': html.unescape(artists),
                            'album': html.unescape(album) if album else '',
                            'coverUrl': album_info.get('picUrl') if isinstance(album_info, dict) else None,
                            'platform': 'NCM'
                        })
            except Exception as e:
                logger.error(f"批量获取歌曲详情失败: {e}")
                
        return name, songs
    except Exception as e:
        logger.error(f"获取网易云歌单失败: {e}")
        return None, []


def get_spotify_playlist_details(playlist_id: str):
    """
    获取 Spotify 歌单详情（通过网页解析，无需 API Key）
    
    Args:
        playlist_id: Spotify 歌单 ID
        
    Returns:
        (歌单名称, 歌曲列表)
    """
    try:
        # 使用 Spotify embed 页面获取歌单信息
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        response = requests_session.get(embed_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # 从 HTML 中提取 JSON 数据
        html_content = response.text
        
        # 尝试找到歌单数据
        import re
        
        # 方法1: 找 <script id="__NEXT_DATA__" 
        json_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                # 解析歌单信息
                playlist_data = data.get('props', {}).get('pageProps', {})
                
                playlist_name = playlist_data.get('state', {}).get('data', {}).get('entity', {}).get('name', f'Spotify 歌单')
                tracks_data = playlist_data.get('state', {}).get('data', {}).get('entity', {}).get('trackList', [])
                
                songs = []
                for track in tracks_data:
                    title = track.get('title', '')
                    artists = track.get('subtitle', '')  # Spotify embed 中 subtitle 是艺术家
                    
                    if title:
                        songs.append({
                            'source_id': track.get('uri', ''),
                            'title': title,
                            'artist': artists,
                            'platform': 'Spotify'
                        })
                
                if songs:
                    logger.info(f"成功获取 Spotify 歌单: {playlist_name}, {len(songs)} 首歌曲")
                    return playlist_name, songs
            except json.JSONDecodeError:
                pass
        
        # 方法2: 使用 Spotify oembed API
        oembed_url = f"https://open.spotify.com/oembed?url=https://open.spotify.com/playlist/{playlist_id}"
        oembed_resp = requests_session.get(oembed_url, headers=headers, timeout=10)
        if oembed_resp.status_code == 200:
            oembed_data = oembed_resp.json()
            playlist_name = oembed_data.get('title', 'Spotify 歌单')
            # oembed 不包含歌曲列表，但至少能获取歌单名称
            logger.info(f"获取到 Spotify 歌单名称: {playlist_name}")
            
            # 尝试从网页版获取歌曲列表
            web_url = f"https://open.spotify.com/playlist/{playlist_id}"
            web_resp = requests_session.get(web_url, headers=headers, timeout=15)
            
            # 使用正则提取歌曲信息
            # Spotify 网页中歌曲通常在 data-testid="tracklist-row" 元素中
            track_pattern = r'"name":"([^"]+)"[^}]*"artists":\[(\{[^]]+\})\]'
            matches = re.findall(track_pattern, web_resp.text)
            
            songs = []
            seen = set()
            for title, artists_json in matches:
                try:
                    # 解析艺术家
                    artist_names = re.findall(r'"name":"([^"]+)"', artists_json)
                    artist = '/'.join(artist_names) if artist_names else ''
                    
                    key = f"{title}|{artist}"
                    if key not in seen and title:
                        seen.add(key)
                        songs.append({
                            'source_id': '',
                            'title': html.unescape(title),
                            'artist': html.unescape(artist),
                            'platform': 'Spotify'
                        })
                except:
                    continue
            
            if songs:
                logger.info(f"从 Spotify 网页解析到 {len(songs)} 首歌曲")
                return playlist_name, songs
        
        logger.warning(f"无法解析 Spotify 歌单: {playlist_id}")
        return None, []
        
    except Exception as e:
        logger.error(f"获取 Spotify 歌单失败: {e}")
        return None, []


# ============================================================
# 匹配逻辑
# ============================================================

def find_best_match(source_track, candidates, match_mode):
    if not candidates: return None
    source_title = source_track.get('title', '').strip()
    source_artist = source_track.get('artist', '').strip()
    source_album = source_track.get('album', '').strip()  # 新增专辑匹配
    
    if match_mode == "完全匹配":
        source_artists_norm = sorted(list(_normalize_artists(source_artist)))
        for track in candidates:
            # 标题标准化比较 (忽略括号内的后缀，如 "爱你没错 (电视剧...)" == "爱你没错")
            if _get_title_lookup_key(source_title) == _get_title_lookup_key(track.get('title', '').strip()):
                track_artists_norm = sorted(list(_normalize_artists(track.get('artist', ''))))
                if source_artists_norm == track_artists_norm:
                    # 如果源歌曲有专辑信息，必须严格匹配专辑
                    if source_album:
                        track_album = track.get('album', '').strip()
                        # 只有当候选歌曲也有专辑信息时才比对
                        if track_album:
                            # 专辑名匹配优化：使用 token_set_ratio 解决 "古剑奇谭" vs "古剑奇谭 电视原声带" 问题
                            # token_set_ratio 会自动处理单词顺序和多余词汇
                            album_sim = fuzz.token_set_ratio(source_album.lower(), track_album.lower())
                            
                            if album_sim < 80:  # 专辑名相似度低于80%
                                # 强制匹配逻辑：虽然专辑名不对，但因为前面已经确认了标题(归一化后)和歌手一致
                                # 所以我们认为这是同一首歌的不同版本 (如 单曲 vs 专辑 vs OST)
                                logger.info(f"专辑不匹配[{album_sim}%]但标题歌手一致，强制匹配: {source_title} (源[{source_album}] vs 本地[{track_album}])")
                                return track
                                
                                # logger.info(f"专辑不匹配: 源[{source_album}] vs 本地[{track_album}] = {album_sim}% (token_set)")
                                # continue
                        # 如果候选歌曲没有专辑信息，但标题和歌手完全匹配，我们认为是匹配的 (宽容模式)
                    return track
         # 循环结束未找到匹配
        logger.info(f"未找到匹配: 源[{source_title} - {source_artist}]")
        return None
    
    # 模糊匹配
    best_match, best_score = None, -1
    source_title_lower = source_title.lower()
    source_album_lower = source_album.lower() if source_album else ''
    source_artists_norm = _normalize_artists(source_artist)
    
    for track in candidates:
        track_title_lower = track.get('title', '').lower()
        title_sim = fuzz.ratio(source_title_lower, track_title_lower)
        
        title_pts = 10 if title_sim >= 95 else (8 if title_sim >= 88 else (5 if title_sim >= 75 else 0))
        
        track_artists_norm = _normalize_artists(track.get('artist', ''))
        artist_pts = 0
        if source_artists_norm and track_artists_norm:
            if source_artists_norm == track_artists_norm: artist_pts = 5
            elif source_artists_norm.issubset(track_artists_norm) or track_artists_norm.issubset(source_artists_norm): artist_pts = 4
            elif source_artists_norm.intersection(track_artists_norm): artist_pts = 2
        
        # 新增：专辑匹配加分（权重显著提高）
        album_pts = 0
        if source_album_lower:
            track_album_lower = track.get('album', '').lower()
            if track_album_lower:
                album_sim = fuzz.ratio(source_album_lower, track_album_lower)
                if album_sim >= 95: album_pts = 8  # 专辑名几乎完全匹配 - 大幅加分
                elif album_sim >= 80: album_pts = 5  # 专辑名较相似
                elif album_sim >= 60: album_pts = 2  # 专辑名有些相似
                # 如果专辑名差异很大，严重扣分（避免匹配到同名但不同专辑的歌曲）
                elif album_sim < 50 and title_sim >= 85:
                    album_pts = -10  # 标题很像但专辑完全不同，很可能是不同版本
            else:
                # 候选歌曲没有专辑信息但源歌曲有，轻微扣分
                album_pts = -3
        
        score = title_pts + artist_pts + album_pts
        if score > best_score:
            best_match, best_score = track, score
    
    return best_match if best_score >= MATCH_THRESHOLD else None


def process_playlist(playlist_url, user_id=None, force_public=False, user_binding=None, match_mode="完全匹配"):
    global emby_library_data
    
    playlist_type, playlist_id = parse_playlist_input(playlist_url)
    if not playlist_type:
        return None, "无法识别的歌单链接"
    
    # 检查并重新加载缓存（如果缓存文件比内存数据新）
    if LIBRARY_CACHE_FILE.exists():
        try:
            cache_mtime = LIBRARY_CACHE_FILE.stat().st_mtime
            # 检查是否需要重新加载（如果没有加载过或缓存文件更新了）
            if not hasattr(process_playlist, '_last_cache_load') or process_playlist._last_cache_load < cache_mtime:
                with open(LIBRARY_CACHE_FILE, 'r', encoding='utf-8') as f:
                    cached_data = json.load(f)
                if cached_data:
                    emby_library_data = cached_data
                    process_playlist._last_cache_load = cache_mtime
                    logger.info(f"重新加载 Emby 缓存: {len(emby_library_data)} 首歌曲")
        except Exception as e:
            logger.warning(f"重新加载缓存失败: {e}")
    
    # 用户认证
    if user_binding:
        token, emby_user_id = authenticate_emby(EMBY_URL, user_binding['emby_username'], user_binding['emby_password'])
        if not token:
            return None, "Emby 认证失败"
        temp_auth = {'access_token': token, 'user_id': emby_user_id}
    else:
        temp_auth = None
    
    # 获取歌单
    logger.info(f"处理 {playlist_type.upper()} 歌单: {playlist_id}")
    if playlist_type == "qq":
        source_name, source_songs = get_qq_playlist_details(playlist_id)
    elif playlist_type == "spotify":
        source_name, source_songs = get_spotify_playlist_details(playlist_id)
    else:  # netease
        source_name, source_songs = get_ncm_playlist_details(playlist_id)
    
    source_songs = [s for s in source_songs if s and s.get('title')]
    if not source_songs:
        return None, "无法获取歌单内容"
    
    # 构建索引并匹配
    emby_index = {}
    for track in emby_library_data:
        key = _get_title_lookup_key(track.get('title'))
        if key: emby_index.setdefault(key, []).append(track)
    
    matched_ids, unmatched = [], []
    for source_track in source_songs:
        key = _get_title_lookup_key(source_track.get('title'))
        match = find_best_match(source_track, emby_index.get(key, []), match_mode)
        
        # --- DEBUG 匿名的好友 ---
        if "匿名的好友" in source_track.get('title', ''):
            if match:
                 logger.warning(f"DEBUG TARGET [匿名的好友] 匹配情况: 成功匹配到本地 -> {match.get('title')} (ID:{match.get('id')}) 专辑:{match.get('album')}")
            else:
                 logger.warning(f"DEBUG TARGET [匿名的好友] 匹配情况: 匹配失败 (候选数: {len(emby_index.get(key, []))})")
        # -----------------------

        if match:
            matched_ids.append(match['id'])
        else:
            unmatched.append(source_track)
    
    logger.info(f"匹配完成: {len(matched_ids)} 成功, {len(unmatched)} 失败")
    
    if not matched_ids:
        return None, f"歌单 '{source_name}' 未匹配到任何歌曲"
    
    # 删除同名歌单
    # 检查是否存在同名歌单
    target_playlist_id = None
    user_api_id = temp_auth['user_id'] if temp_auth else emby_auth['user_id']
    
    for p in get_user_emby_playlists(temp_auth or emby_auth):
        if p.get('name') == source_name:
            target_playlist_id = p['id']
            logger.info(f"找到同名歌单: {source_name} (ID: {target_playlist_id})")
            break
    
    # 确保 ID 比较类型一致
    is_admin = str(user_id) == str(ADMIN_USER_ID)
    is_public = force_public or (MAKE_PLAYLIST_PUBLIC and is_admin)
    
    if target_playlist_id:
        # --- 增量更新逻辑 ---
        logger.info(f"正在增量更新歌单: {source_name}")
        
        # 尝试更新公开状态 (Retroactive update)
        if is_public:
            try:
                call_emby_api(f"Items/{target_playlist_id}/MakePublic", {}, method='POST', user_auth=temp_auth)
            except Exception as e:
                logger.error(f"[增量更新] 尝试设为公开失败: {e}")
        
        # 获取现有歌单的所有 Item ID
        existing_items = call_emby_api(f"Playlists/{target_playlist_id}/Items", 
                                      {'Fields': 'Id', 'UserId': user_api_id}, 
                                      user_auth=temp_auth)
        
        existing_ids = set()
        if existing_items and 'Items' in existing_items:
            existing_ids = {item['Id'] for item in existing_items['Items']}
            
        # 1. 移除不再匹配的歌曲 (清理错误匹配或已删除的歌曲)
        ids_to_remove = [eid for eid in existing_ids if eid not in matched_ids]
        if ids_to_remove:
            logger.info(f"需要移除 {len(ids_to_remove)} 首不再匹配的歌曲")
            for i in range(0, len(ids_to_remove), EMBY_PLAYLIST_ADD_BATCH_SIZE):
                batch = ids_to_remove[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
                try:
                    call_emby_api(f"Playlists/{target_playlist_id}/Items",
                                 {'Ids': ",".join(batch), 'UserId': user_api_id},
                                 method='DELETE', user_auth=temp_auth)
                    time.sleep(0.2)
                except Exception as e:
                    logger.error(f"移除歌单歌曲失败: {e}")

        # 2. 添加新匹配的歌曲
        ids_to_add = [mid for mid in matched_ids if mid not in existing_ids]
        
        if not ids_to_add:
            logger.info("无新歌曲需要添加")
        else:
            unique_ids_to_add = list(dict.fromkeys(ids_to_add))
            logger.info(f"需要添加 {len(unique_ids_to_add)} 首新歌曲")
            
            for i in range(0, len(unique_ids_to_add), EMBY_PLAYLIST_ADD_BATCH_SIZE):
                batch = unique_ids_to_add[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
                call_emby_api(f"Playlists/{target_playlist_id}/Items",
                             {'Ids': ",".join(batch), 'UserId': user_api_id},
                             method='POST', user_auth=temp_auth)
                time.sleep(0.2)
                
    else:
        # --- 创建新歌单逻辑 ---
        
        # 确定可见性:
        # 1. 优先使用 force_public (代码强制)
        # 2. 其次查询数据库中该订阅的设置
        # 3. 最后使用全局配置
        
        is_public = force_public
        
        # 尝试从数据库查询订阅设置
        if database_conn and playlist_type:
            try:
                cursor = database_conn.cursor()
                # 根据 URL 查找是否已订阅
                cursor.execute('SELECT is_public FROM scheduled_playlists WHERE telegram_id = ? AND playlist_url LIKE ?', 
                              (user_id, f'%{playlist_id}%')) # 模糊匹配 ID 比较保险
                row = cursor.fetchone()
                if row:
                    is_public = bool(row[0]) if row[0] is not None else True # 假如订阅存在，默认为 True
                    logger.info(f"[歌单同步] 使用数据库订阅设置: is_public={is_public}")
                else:
                    # 如果未订阅（手动单次同步），默认使用全局设置或 Public
                    is_public = is_public or MAKE_PLAYLIST_PUBLIC or True # 默认 Public
            except Exception as e:
                logger.warning(f"[歌单同步] 查询订阅设置失败: {e}")
                is_public = is_public or MAKE_PLAYLIST_PUBLIC
        else:
             is_public = is_public or MAKE_PLAYLIST_PUBLIC

        logger.info(f"[歌单同步] 准备创建歌单: {source_name}, Visible={is_public}")
        
        from bot.services.emby import create_emby_playlist
        
        unique_ids = list(dict.fromkeys(matched_ids))
        new_playlist_id = create_emby_playlist(source_name, unique_ids, temp_auth or emby_auth, is_public=is_public)
        
        if not new_playlist_id:
             return None, "创建歌单失败"
        
        logger.info(f"[歌单同步] 歌单创建成功: {new_playlist_id}")
    
    # 记录到数据库
    save_playlist_record(user_id, source_name, playlist_type, len(source_songs), len(matched_ids))
    
    # --- 计算真正缺失的歌曲 (同步后实际不在 Emby 歌单中的歌曲) ---
    # 获取最终歌单内容
    final_playlist_id = target_playlist_id if target_playlist_id else new_playlist_id
    final_items = call_emby_api(f"Playlists/{final_playlist_id}/Items", 
                                {'Fields': 'Name,Album,Artists', 'UserId': user_api_id}, 
                                user_auth=temp_auth)
    
    # 构建 Emby 歌单中的歌曲名索引 (用于检查源歌曲是否真的在歌单中)
    final_song_keys = set()
    if final_items and 'Items' in final_items:
        for item in final_items['Items']:
            key = _get_title_lookup_key(item.get('Name', ''))
            if key:
                final_song_keys.add(key)
    
    # 检查源歌曲哪些真的不在最终歌单中
    truly_unmatched = []
    for source_track in source_songs:
        source_key = _get_title_lookup_key(source_track.get('title', ''))
        if source_key and source_key not in final_song_keys:
            truly_unmatched.append(source_track)
    
    # 同步后的真实匹配数 = 源歌单歌曲数 - 真正缺失的歌曲数
    final_matched_count = len(source_songs) - len(truly_unmatched)
    
    logger.info(f"同步完成: 源歌单 {len(source_songs)} 首, Emby 歌单 {len(final_song_keys)} 首, 真正缺失 {len(truly_unmatched)} 首")
    
    result = {
        'name': source_name,
        'total': len(source_songs),
        'matched': final_matched_count,
        'unmatched': len(truly_unmatched),
        'unmatched_songs': truly_unmatched[:15],  # 显示前15首
        'all_unmatched': truly_unmatched,  # 保存所有未匹配歌曲用于下载
        'mode': match_mode
    }
    return result, None


# ============================================================
# 数据库操作
# ============================================================

def init_database():
    global database_conn
    database_conn = sqlite3.connect(str(DATABASE_FILE), check_same_thread=False)
    cursor = database_conn.cursor()
    
    # 用户绑定表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bindings (
            telegram_id TEXT PRIMARY KEY,
            emby_username TEXT NOT NULL,
            emby_password TEXT NOT NULL,
            emby_user_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 歌单同步记录
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS playlist_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            playlist_name TEXT,
            platform TEXT,
            total_songs INTEGER,
            matched_songs INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 上传记录
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS upload_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            original_name TEXT,
            saved_name TEXT,
            file_size INTEGER,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 定时同步歌单
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT NOT NULL,
            playlist_url TEXT NOT NULL,
            playlist_name TEXT,
            platform TEXT,
            last_song_ids TEXT,
            last_sync_at TIMESTAMP,
            sync_interval INTEGER DEFAULT 360,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, playlist_url)
        )
    ''')
    
    # 添加 is_active 字段（兼容旧数据库）
    try:
        cursor.execute('ALTER TABLE scheduled_playlists ADD COLUMN is_active INTEGER DEFAULT 1')
    except:
        pass  # 字段已存在
    
    # 添加 sync_interval 字段（兼容旧数据库）
    try:
        cursor.execute('ALTER TABLE scheduled_playlists ADD COLUMN sync_interval INTEGER DEFAULT 360')
    except:
        pass  # 字段已存在
        
    # 添加 is_public 字段（新增）
    try:
        cursor.execute('ALTER TABLE scheduled_playlists ADD COLUMN is_public INTEGER DEFAULT 1')
    except:
        pass  # 字段已存在
    
    # ============================================================
    # 用户会员系统相关表
    # ============================================================
    
    # Web 用户表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_encrypted TEXT,
            email TEXT,
            role TEXT DEFAULT 'user',
            emby_user_id TEXT,
            emby_username TEXT,
            points INTEGER DEFAULT 0,
            expire_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            last_checkin_at DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 升级旧表：添加 password_encrypted 列
    try:
        cursor.execute('ALTER TABLE web_users ADD COLUMN password_encrypted TEXT')
    except:
        pass  # 列已存在
    
    # 升级旧表：添加 telegram_id 列
    try:
        cursor.execute('ALTER TABLE web_users ADD COLUMN telegram_id TEXT')
    except:
        pass  # 列已存在
    
    # 升级旧表：添加 invite_code 列
    try:
        cursor.execute('ALTER TABLE web_users ADD COLUMN invite_code TEXT UNIQUE')
    except:
        pass  # 列已存在
    
    # 卡密表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS card_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_key TEXT UNIQUE NOT NULL,
            duration_days INTEGER NOT NULL,
            used_by INTEGER,
            used_at TIMESTAMP,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (used_by) REFERENCES web_users(id)
        )
    ''')
    
    # 积分记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS points_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            change_amount INTEGER NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users(id)
        )
    ''')
    
    # 会员记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS membership_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            duration_days INTEGER,
            source TEXT,
            source_detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users(id)
        )
    ''')
    
    # 系统配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 初始化默认系统配置
    default_configs = [
        ('enable_user_register', 'true'),
        ('require_email_verify', 'false'),
        ('checkin_points_mode', 'random'),  # 'fixed' or 'random'
        ('checkin_points_fixed', '10'),
        ('checkin_points_min', '5'),
        ('checkin_points_max', '20'),
        ('points_per_day', '100'),  # 多少积分换1天
    ]
    for key, value in default_configs:
        cursor.execute('''
            INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)
        ''', (key, value))
    
    database_conn.commit()
    logger.info(f"数据库初始化完成: {DATABASE_FILE}")

def get_user_binding(telegram_id):
    if not database_conn: return None
    cursor = database_conn.cursor()
    cursor.execute('SELECT emby_username, emby_password, emby_user_id FROM user_bindings WHERE telegram_id = ?',
                  (str(telegram_id),))
    result = cursor.fetchone()
    if result:
        try:
            return {'emby_username': result[0], 'emby_password': decrypt_password(result[1]), 'emby_user_id': result[2]}
        except:
            return None
    return None

def save_user_binding(telegram_id, emby_username, emby_password, emby_user_id=None):
    if not database_conn: return False
    try:
        cursor = database_conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO user_bindings VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
                      (str(telegram_id), emby_username, encrypt_password(emby_password), emby_user_id))
        database_conn.commit()
        return True
    except:
        return False

def delete_user_binding(telegram_id):
    if not database_conn: return False
    try:
        cursor = database_conn.cursor()
        cursor.execute('DELETE FROM user_bindings WHERE telegram_id = ?', (str(telegram_id),))
        database_conn.commit()
        return True
    except:
        return False

def save_playlist_record(telegram_id, name, platform, total, matched):
    if not database_conn: return
    try:
        cursor = database_conn.cursor()
        cursor.execute('INSERT INTO playlist_records (telegram_id, playlist_name, platform, total_songs, matched_songs) VALUES (?, ?, ?, ?, ?)',
                      (str(telegram_id), name, platform, total, matched))
        database_conn.commit()
    except:
        pass

def save_upload_record(telegram_id, original_name, saved_name, file_size):
    if not database_conn: return
    try:
        cursor = database_conn.cursor()
        cursor.execute('INSERT INTO upload_records (telegram_id, original_name, saved_name, file_size) VALUES (?, ?, ?, ?)',
                      (str(telegram_id), original_name, saved_name, file_size))
        database_conn.commit()
    except:
        pass


def save_download_record(songs: list, success_files: list, failed_songs: list, 
                         platform: str, quality: str, user_id: str = None):
    """保存下载记录到历史表"""
    if not database_conn:
        return
    try:
        cursor = database_conn.cursor()
        
        # 确保表存在
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                song_id TEXT,
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
        
        import uuid
        
        # 记录成功的下载
        for i, file_path in enumerate(success_files):
            song = songs[i] if i < len(songs) else {}
            
            # 获取文件大小
            file_size = 0
            if file_path:
                try:
                    p = Path(file_path)
                    if p.exists():
                        file_size = p.stat().st_size
                        logger.debug(f"获取文件大小成功: {file_size} bytes")
                    else:
                        logger.warning(f"保存下载记录时文件不存在: {file_path}")
                except Exception as e:
                    logger.warning(f"获取文件大小失败: {e}")
            
            cursor.execute('''
                INSERT INTO download_history 
                (task_id, song_id, title, artist, platform, quality, status, file_path, file_size, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4())[:8],
                str(song.get('id', '')),
                song.get('title', Path(file_path).stem if file_path else ''),
                song.get('artist', ''),
                platform,
                quality,
                'completed',
                file_path,
                file_size,
                user_id
            ))
        
        # 记录失败的下载
        for song in failed_songs:
            cursor.execute('''
                INSERT INTO download_history 
                (task_id, song_id, title, artist, platform, quality, status, error_message, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4())[:8],
                str(song.get('id', '')),
                song.get('title', ''),
                song.get('artist', ''),
                platform,
                quality,
                'failed',
                song.get('error', '下载失败'),
                user_id
            ))
        
        database_conn.commit()
        logger.debug(f"保存下载记录: {len(success_files)} 成功, {len(failed_songs)} 失败")
    except Exception as e:
        logger.error(f"保存下载记录失败: {e}")


def save_download_record_v2(success_results: list, failed_songs: list, 
                            quality: str, user_id: str = None):
    """保存下载记录到历史表（支持按实际平台记录）
    
    Args:
        success_results: [{'file': path, 'platform': 'NCM'/'QQ', 'song': song_info}, ...]
        failed_songs: 失败的歌曲列表
        quality: 下载音质
        user_id: 用户ID
    """
    if not database_conn:
        return
    try:
        cursor = database_conn.cursor()
        
        # 确保表存在
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                song_id TEXT,
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
        
        import uuid
        
        # 记录成功的下载（按实际下载平台）
        for result in success_results:
            # 兼容字符串路径和字典结果
            if isinstance(result, str):
                file_path = result
                platform = 'NCM'
                song = {}
            else:
                file_path = result.get('file', '')
                platform = result.get('platform', 'NCM')
                song = result.get('song', {})
            
            # 优先使用传入的 file_size（在下载时立即获取的），避免文件被外部程序移走后无法获取
            file_size = result.get('file_size', 0) if isinstance(result, dict) else 0
            
            # 如果没有预先获取的大小，尝试从文件获取
            if not file_size and file_path:
                try:
                    p = Path(file_path)
                    if p.exists():
                        file_size = p.stat().st_size
                        logger.debug(f"获取文件大小成功: {file_size} bytes, 路径: {file_path}")
                    else:
                        logger.warning(f"保存下载记录时文件不存在（可能已被外部程序移走）: {file_path}")
                except Exception as e:
                    logger.warning(f"获取文件大小失败: {e}, 路径: {file_path}")
            
            cursor.execute('''
                INSERT INTO download_history 
                (task_id, song_id, title, artist, platform, quality, status, file_path, file_size, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4())[:8],
                str(song.get('id', song.get('source_id', ''))),
                song.get('title', Path(file_path).stem if file_path else ''),
                song.get('artist', ''),
                platform,
                quality,
                'completed',
                file_path,
                file_size,
                user_id
            ))
        
        # 记录失败的下载
        for song in failed_songs:
            cursor.execute('''
                INSERT INTO download_history 
                (task_id, song_id, title, artist, platform, quality, status, error_message, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4())[:8],
                str(song.get('id', song.get('source_id', ''))),
                song.get('title', ''),
                song.get('artist', ''),
                song.get('platform', 'NCM'),  # 失败的记录原始平台
                quality,
                'failed',
                song.get('error', '下载失败'),
                user_id
            ))
        
        database_conn.commit()
        
        ncm_count = sum(1 for r in success_results if r.get('platform') == 'NCM')
        qq_count = sum(1 for r in success_results if r.get('platform') == 'QQ')
        logger.debug(f"保存下载记录: NCM {ncm_count} 首, QQ {qq_count} 首, 失败 {len(failed_songs)} 首")
    except Exception as e:
        logger.error(f"保存下载记录失败: {e}")


# ============================================================
# 定时同步歌单
# ============================================================

def _parse_db_timestamp(value):
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        normalized = value.replace('Z', '+00:00')
        return dt.datetime.fromisoformat(normalized)
    except Exception:
        logger.debug(f"无法解析时间戳: {value}")
        return None


def get_playlist_sync_interval():
    """获取全局默认歌单同步间隔（分钟）"""
    default_interval = max(MIN_PLAYLIST_SYNC_INTERVAL_MINUTES, DEFAULT_PLAYLIST_SYNC_INTERVAL_MINUTES)
    if not database_conn:
        return default_interval
    try:
        cursor = database_conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('playlist_sync_interval',))
        row = cursor.fetchone()
        if row:
            raw_value = row[0] if isinstance(row, tuple) else row['value']
            try:
                interval = int(raw_value)
                final_interval = max(MIN_PLAYLIST_SYNC_INTERVAL_MINUTES, interval)
                logger.debug(f"[SyncInterval] DB值={raw_value}, 使用={final_interval}分钟")
                return final_interval
            except ValueError:
                logger.warning(f"无效的 playlist_sync_interval 配置: {raw_value}")
        logger.debug(f"[SyncInterval] 未找到DB配置，使用默认值={default_interval}分钟")
        return default_interval
    except Exception as e:
        logger.error(f"读取歌单同步间隔失败: {e}")
        return default_interval


def add_scheduled_playlist(telegram_id: str, playlist_url: str, playlist_name: str, platform: str, song_ids: list):
    """添加定时同步歌单"""
    if not database_conn:
        return False
    try:
        cursor = database_conn.cursor()
        song_ids_json = json.dumps(song_ids)
        default_interval = get_playlist_sync_interval()
        cursor.execute('''
            INSERT INTO scheduled_playlists 
            (telegram_id, playlist_url, playlist_name, platform, last_song_ids, last_sync_at, sync_interval, is_active)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, 1)
            ON CONFLICT(telegram_id, playlist_url) DO UPDATE SET
                playlist_name=excluded.playlist_name,
                platform=excluded.platform,
                last_song_ids=excluded.last_song_ids,
                last_sync_at=excluded.last_sync_at,
                is_active=1,
                sync_interval=CASE
                    WHEN scheduled_playlists.sync_interval IS NULL OR scheduled_playlists.sync_interval < 1
                        THEN excluded.sync_interval
                    ELSE scheduled_playlists.sync_interval
                END
        ''', (str(telegram_id), playlist_url, playlist_name, platform, song_ids_json, default_interval))
        database_conn.commit()
        return True
    except Exception as e:
        logger.error(f"添加定时同步歌单失败: {e}")
        return False

def get_scheduled_playlists(telegram_id: str = None):
    """获取定时同步歌单列表"""
    if not database_conn:
        return []
    try:
        database_conn.row_factory = sqlite3.Row
        cursor = database_conn.cursor()
        if telegram_id:
            cursor.execute('''
                SELECT id, telegram_id, playlist_url, playlist_name, platform,
                       last_song_ids, last_sync_at, sync_interval, is_active
                FROM scheduled_playlists WHERE telegram_id = ? ORDER BY created_at DESC
            ''', (str(telegram_id),))
        else:
            cursor.execute('''
                SELECT id, telegram_id, playlist_url, playlist_name, platform,
                       last_song_ids, last_sync_at, sync_interval, is_active
                FROM scheduled_playlists ORDER BY created_at DESC
            ''')
        rows = cursor.fetchall()
        playlists = []
        for row in rows:
            try:
                last_song_ids = json.loads(row['last_song_ids']) if row['last_song_ids'] else []
            except Exception:
                logger.debug(f"无法解析 last_song_ids: {row['last_song_ids']}")
                last_song_ids = []
            playlists.append({
                'id': row['id'],
                'telegram_id': row['telegram_id'],
                'playlist_url': row['playlist_url'],
                'playlist_name': row['playlist_name'],
                'platform': row['platform'],
                'last_song_ids': last_song_ids,
                'last_sync_at': row['last_sync_at'],
                'sync_interval': row['sync_interval'],
                'is_active': row['is_active'] if row['is_active'] is not None else 1
            })
        return playlists
    except Exception as e:
        logger.error(f"获取定时同步歌单失败: {e}")
        return []

def delete_scheduled_playlist(playlist_id: int, telegram_id: str = None):
    """删除定时同步歌单"""
    if not database_conn:
        return False
    try:
        cursor = database_conn.cursor()
        if telegram_id:
            cursor.execute('DELETE FROM scheduled_playlists WHERE id = ? AND telegram_id = ?', 
                          (playlist_id, str(telegram_id)))
        else:
            cursor.execute('DELETE FROM scheduled_playlists WHERE id = ?', (playlist_id,))
        database_conn.commit()
        return cursor.rowcount > 0
    except:
        return False

def update_scheduled_playlist_songs(playlist_id: int, song_ids: list, playlist_name: str = None):
    """更新歌单的歌曲列表"""
    if not database_conn:
        return False
    try:
        cursor = database_conn.cursor()
        song_ids_json = json.dumps(song_ids)
        fields = ['last_song_ids = ?', 'last_sync_at = CURRENT_TIMESTAMP']
        params = [song_ids_json]
        if playlist_name:
            fields.append('playlist_name = ?')
            params.append(playlist_name)
        params.append(playlist_id)
        cursor.execute(f"UPDATE scheduled_playlists SET {', '.join(fields)} WHERE id = ?", params)
        database_conn.commit()
        return True
    except Exception as e:
        logger.error(f"更新歌单 {playlist_id} 失败: {e}")
        return False


async def check_playlist_updates(app):
    """根据各自间隔检查歌单更新并同步新歌曲"""
    playlists = get_scheduled_playlists()
    if not playlists:
        logger.info("没有订阅歌单，跳过同步检查")
        return
    
    # 重置所有歌单间隔为全局设置（确保全局配置生效）
    global_interval = get_playlist_sync_interval()
    if database_conn:
        try:
            cursor = database_conn.cursor()
            cursor.execute('UPDATE scheduled_playlists SET sync_interval = ? WHERE sync_interval != ?', 
                          (global_interval, global_interval))
            if cursor.rowcount > 0:
                logger.info(f"已重置 {cursor.rowcount} 个歌单的同步间隔为全局设置 ({global_interval} 分钟)")
            database_conn.commit()
        except Exception as e:
            logger.warning(f"重置歌单同步间隔失败: {e}")
    
    logger.info(f"检查 {len(playlists)} 个订阅歌单...")
    
    # First, identify which playlists are due for sync
    playlists_due = []
    default_interval = get_playlist_sync_interval()
    now = dt.datetime.utcnow()

    for playlist in playlists:
        try:
            playlist_name = playlist.get('playlist_name') or '未知歌单'
            # Skip inactive playlists
            if not playlist.get('is_active', 1):
                logger.debug(f"歌单 '{playlist_name}' 未激活，跳过")
                continue
            
            interval = playlist.get('sync_interval') or default_interval
            interval = max(MIN_PLAYLIST_SYNC_INTERVAL_MINUTES, interval)
            last_sync_at = _parse_db_timestamp(playlist.get('last_sync_at'))
            
            is_due = False
            if last_sync_at:
                elapsed_minutes = (now - last_sync_at).total_seconds() / 60
                if elapsed_minutes >= interval:
                    logger.info(f"歌单 '{playlist_name}' 到期同步 ({elapsed_minutes:.1f} >= {interval} 分钟)")
                    is_due = True
                else:
                    logger.debug(f"歌单 '{playlist_name}' 未到同步时间 ({elapsed_minutes:.1f}/{interval} 分钟)")
            else:
                logger.info(f"歌单 '{playlist_name}' 首次同步 (无上次同步记录)")
                is_due = True
            
            if is_due:
                playlists_due.append(playlist)

        except Exception as e:
            logger.error(f"检查歌单 '{playlist.get('playlist_name')}' 状态失败: {e}")
            continue

    if not playlists_due:
        return

    logger.info(f"发现 {len(playlists_due)} 个歌单需要同步，准备刷新 Emby 缓存...")

    # Only scan Emby if we have playlists to sync
    if emby_auth:
        try:
            scan_emby_library(save_to_cache=True)
            logger.info(f"Emby 库缓存已刷新: {len(emby_library_data)} 首歌曲")
        except Exception as e:
            logger.warning(f"刷新 Emby 库缓存失败: {e}")
    
    # Process only due playlists
    for playlist in playlists_due:
        try:
            playlist_name = playlist.get('playlist_name') or '未知歌单'
            telegram_id = playlist['telegram_id']
            playlist_url = playlist['playlist_url']
            platform = playlist['platform']
            last_ids = playlist.get('last_song_ids') or []
            old_song_ids = set(str(sid) for sid in last_ids)
            songs = []
            remote_name = None
            logger.info(f"正在检查歌单 '{playlist_name}' (平台: {platform})...")
            if platform == 'netease':
                playlist_id = extract_playlist_id(playlist_url, 'netease')
                if not playlist_id:
                    logger.warning(f"无法解析网易云歌单链接: {playlist_url}")
                    continue
                remote_name, songs = get_ncm_playlist_details(playlist_id)
            elif platform == 'qq':
                playlist_id = extract_playlist_id(playlist_url, 'qq')
                if not playlist_id:
                    logger.warning(f"无法解析 QQ 歌单链接: {playlist_url}")
                    continue
                remote_name, songs = get_qq_playlist_details(playlist_id)
            else:
                logger.debug(f"暂不支持的平台 {platform}")
                continue
            if remote_name:
                playlist_name = remote_name
            if not songs:
                logger.warning(f"歌单 '{playlist_name}' 获取失败或为空，跳过")
                # 检查 Cookie 状态并提醒管理员
                try:
                    cookie_ok = True
                    if platform == 'netease':
                        from bot.ncm_downloader import check_ncm_cookie
                        ncm_cookie = get_ncm_cookie()
                        if ncm_cookie:
                            cookie_ok = check_ncm_cookie(ncm_cookie)
                            if not cookie_ok:
                                logger.warning(f"网易云 Cookie 可能已失效，无法获取完整歌单")
                    elif platform == 'qq':
                        from bot.ncm_downloader import check_qq_cookie
                        qq_cookie = get_qq_cookie()
                        if qq_cookie:
                            cookie_ok = check_qq_cookie(qq_cookie)
                            if not cookie_ok:
                                logger.warning(f"QQ音乐 Cookie 可能已失效，无法获取完整歌单")
                    # 发送提醒
                    if not cookie_ok and ADMIN_USER_ID:
                        try:
                            admin_ids = [int(x.strip()) for x in str(ADMIN_USER_ID).split(',') if x.strip()]
                            for admin_id in admin_ids[:1]:
                                await app.bot.send_message(
                                    chat_id=admin_id,
                                    text=f"⚠️ **歌单同步异常**\n\n歌单: `{playlist_name}`\n平台: {platform}\n原因: Cookie 可能已失效，请检查并重新登录",
                                    parse_mode='Markdown'
                                )
                        except:
                            pass
                except Exception as cookie_e:
                    logger.debug(f"Cookie 检查异常: {cookie_e}")
                continue
            logger.info(f"歌单 '{playlist_name}' 共 {len(songs)} 首，旧记录 {len(old_song_ids)} 首")
            current_song_ids = [str(s.get('source_id') or s.get('id') or s.get('title', '')) for s in songs]
            new_songs = [s for s in songs if str(s.get('source_id') or s.get('id') or s.get('title', '')) not in old_song_ids]
            if new_songs:
                logger.info(f"歌单 '{playlist_name}' 发现 {len(new_songs)} 首新歌曲 (间隔 {interval} 分钟)")
                try:
                    # 直接显示新歌列表，不做库匹配预检查（避免缓存导致的误报）
                    message = f"🔔 **歌单更新通知**\n\n"
                    message += f"📋 歌单: `{playlist_name}`\n"
                    message += f"🆕 发现 {len(new_songs)} 首新歌曲\n\n"
                    
                    # 显示新歌列表
                    for i, s in enumerate(new_songs[:5]):
                        message += f"🎵 {s['title']} - {s['artist']}\n"
                    if len(new_songs) > 5:
                        message += f"... 还有 {len(new_songs) - 5} 首\n"
                    
                    # 按钮：同步到Emby（同步时会准确检查缺失）
                    buttons = [InlineKeyboardButton("🔄 同步到Emby", callback_data=f"sync_emby_{playlist['id']}")]
                    keyboard = [buttons]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"发送歌单更新通知失败: {e}")
                
                # 自动同步歌单到 Emby（有新歌时自动同步）
                if new_songs and emby_auth:
                    logger.info(f"歌单 '{playlist_name}' 有 {len(new_songs)} 首新歌，自动同步到 Emby...")
                    try:
                        result, error = process_playlist(playlist['playlist_url'], int(telegram_id), force_public=False, match_mode="模糊匹配")
                        if error:
                            logger.error(f"自动同步歌单 '{playlist_name}' 失败: {error}")
                        else:
                            logger.info(f"自动同步歌单 '{playlist_name}' 成功: {result['matched']}/{result['total']} 首已匹配")
                            # 发送同步完成通知
                            try:
                                await app.bot.send_message(
                                    chat_id=int(telegram_id),
                                    text=f"✅ **已自动同步歌单到 Emby**\n\n📋 `{playlist_name}`\n✅ 已匹配: {result['matched']}/{result['total']} 首",
                                    parse_mode='Markdown'
                                )
                            except:
                                pass
                    except Exception as e:
                        logger.error(f"自动同步歌单出错: {e}")
            else:
                # 即使没有新歌，也检查并同步歌单（确保 Emby 歌单完整）
                logger.info(f"歌单 '{playlist_name}' 无新歌曲，但仍验证 Emby 同步状态...")
                if emby_auth:
                    try:
                        result, error = process_playlist(playlist['playlist_url'], int(telegram_id), force_public=False, match_mode="模糊匹配")
                        if error:
                            logger.warning(f"验证同步歌单 '{playlist_name}' 失败: {error}")
                        else:
                            logger.info(f"歌单 '{playlist_name}' 同步验证完成: {result['matched']}/{result['total']} 首")
                            
                            # 如果有未匹配的歌曲，发送通知提示下载
                            unmatched_songs = result.get('all_unmatched', [])
                            if unmatched_songs and len(unmatched_songs) > 0:
                                try:
                                    # 保存未匹配歌曲到临时存储，用于后续下载
                                    # database_conn 是文件顶部定义的全局变量
                                    if database_conn:
                                        cursor = database_conn.cursor()
                                        cursor.execute('''
                                            INSERT OR REPLACE INTO bot_settings (key, value)
                                            VALUES (?, ?)
                                        ''', (f'unmatched_songs_{playlist["id"]}', json.dumps(unmatched_songs)))
                                        database_conn.commit()
                                    
                                    # 构建通知消息
                                    msg = f"📋 **歌单同步完成**\n\n"
                                    msg += f"🎵 `{playlist_name}`\n"
                                    msg += f"✅ 已匹配: {result['matched']}/{result['total']} 首\n"
                                    msg += f"❌ 未找到: {len(unmatched_songs)} 首\n\n"
                                    
                                    # 显示前5首未匹配歌曲
                                    msg += "**未匹配歌曲:**\n"
                                    for i, s in enumerate(unmatched_songs[:5]):
                                        msg += f"  • {s.get('title', '')} - {s.get('artist', '')}\n"
                                    if len(unmatched_songs) > 5:
                                        msg += f"  ... 还有 {len(unmatched_songs) - 5} 首\n"
                                    
                                    keyboard = [
                                        [InlineKeyboardButton(f"📥 下载 {len(unmatched_songs)} 首缺失歌曲", 
                                                             callback_data=f"sync_dl_unmatched_{playlist['id']}")]
                                    ]
                                    
                                    await app.bot.send_message(
                                        chat_id=int(telegram_id),
                                        text=msg,
                                        parse_mode='Markdown',
                                        reply_markup=InlineKeyboardMarkup(keyboard)
                                    )
                                except Exception as notify_err:
                                    logger.error(f"发送未匹配歌曲通知失败: {notify_err}")
                    except Exception as e:
                        logger.error(f"验证同步歌单出错: {e}")
            update_scheduled_playlist_songs(playlist['id'], current_song_ids, playlist_name)
        except Exception as e:
            logger.error(f"检查歌单 '{playlist.get('playlist_name', '')}' 更新失败: {e}")
    if not due_found:
        logger.debug("没有到达同步间隔的歌单，等待下一个周期")


# 注: scheduled_sync_job 和 scheduled_emby_scan_job 的主实现在文件后面

def get_ncm_settings():
    """获取网易云下载设置（优先从数据库读取，否则从环境变量）"""
    default_settings = {
        'ncm_quality': os.environ.get('NCM_QUALITY', 'exhigh'),
        'auto_download': os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true',
        'download_mode': 'local',
        'download_dir': str(MUSIC_TARGET_DIR),
        'musictag_dir': '',
        'organize_dir': ''
    }
    
    if not database_conn:
        return default_settings
    
    try:
        cursor = database_conn.cursor()
        
        # 确保设置表存在
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('ncm_quality',))
        row = cursor.fetchone()
        ncm_quality = row[0] if row else default_settings['ncm_quality']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_download',))
        row = cursor.fetchone()
        auto_download = row[0] == 'true' if row else default_settings['auto_download']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_mode',))
        row = cursor.fetchone()
        download_mode = row[0] if row else default_settings['download_mode']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
        row = cursor.fetchone()
        download_dir = row[0] if row else default_settings['download_dir']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('musictag_dir',))
        row = cursor.fetchone()
        musictag_dir = row[0] if row else default_settings['musictag_dir']
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_dir',))
        row = cursor.fetchone()
        organize_dir = row[0] if row else default_settings['organize_dir']
        
        return {
            'ncm_quality': ncm_quality,
            'auto_download': auto_download,
            'download_mode': download_mode,
            'download_dir': download_dir,
            'musictag_dir': musictag_dir,
            'organize_dir': organize_dir
        }
    except:
        return default_settings

def get_stats():
    """获取统计数据"""
    if not database_conn: return {}
    cursor = database_conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM user_bindings')
    users = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*), SUM(matched_songs) FROM playlist_records')
    row = cursor.fetchone()
    playlists, songs_synced = row[0] or 0, row[1] or 0
    
    cursor.execute('SELECT COUNT(*), SUM(file_size) FROM upload_records')
    row = cursor.fetchone()
    uploads, upload_size = row[0] or 0, row[1] or 0
    
    return {
        'users': users,
        'playlists': playlists,
        'songs_synced': songs_synced,
        'uploads': uploads,
        'upload_size': upload_size,
        'library_songs': len(emby_library_data)
    }

def get_recent_records(limit=20):
    """获取最近记录"""
    if not database_conn: return [], []
    cursor = database_conn.cursor()
    
    cursor.execute('SELECT playlist_name, platform, total_songs, matched_songs, created_at FROM playlist_records ORDER BY created_at DESC LIMIT ?', (limit,))
    playlists = cursor.fetchall()
    
    cursor.execute('SELECT original_name, saved_name, file_size, created_at FROM upload_records ORDER BY created_at DESC LIMIT ?', (limit,))
    uploads = cursor.fetchall()
    
    return playlists, uploads


# ============================================================
# Telegram 命令处理 - 主菜单
# ============================================================

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 歌单同步", callback_data="menu_playlist"),
         InlineKeyboardButton("📤 音乐上传", callback_data="menu_upload")],
        [InlineKeyboardButton("⚙️ 设置", callback_data="menu_settings"),
         InlineKeyboardButton("📊 状态", callback_data="menu_status")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    binding = get_user_binding(user_id)
    
    welcome = f"👋 欢迎使用 **{APP_NAME}**！\n\n"
    if binding:
        welcome += f"已绑定 Emby: `{binding['emby_username']}`\n\n"
    else:
        welcome += "⚠️ 尚未绑定 Emby 账户\n\n"
    welcome += "请选择功能："
    
    await update.message.reply_text(welcome, reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
**📖 TGmusicbot 使用帮助**

**🎵 歌单同步：** 直接发送歌单链接
**📤 上传音乐：** 直接发送音频文件

**📋 搜索下载：**
`/ss 关键词` - 网易云搜索歌曲
`/al 专辑名` - 网易云搜索专辑
`/qs 关键词` - QQ音乐搜索歌曲
`/qa 专辑名` - QQ音乐搜索专辑

**📥 下载管理：**
`/ds` - 查看下载状态
`/dq` - 查看下载队列
`/dh` - 查看下载历史

**📋 其他命令：**
`/req 歌曲-歌手` - 申请补全歌曲
`/mr` - 查看我的申请
`/sub` - 查看订阅歌单
`/unsub 序号` - 取消订阅
`/scan` - 手动扫描Emby库
`/si 小时` - 设置自动扫描间隔

**🔧 基础命令：**
`/b 用户名 密码` - 绑定Emby
`/unbind` - 解除绑定
`/s` - 查看状态

💡 所有短命令都有完整版本，如 /ss = /search
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')


# ============================================================
# Telegram 命令处理 - 歌单同步
# ============================================================

async def handle_playlist_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    
    playlist_type, _ = parse_playlist_input(text)
    if not playlist_type:
        return False
    
    binding = get_user_binding(user_id)
    if not binding:
        await update.message.reply_text("请先绑定 Emby 账户：/bind <用户名> <密码>")
        return True
    
    context.user_data['playlist_url'] = text
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ 模糊匹配", callback_data="match_fuzzy"),
         InlineKeyboardButton("🎯 完全匹配", callback_data="match_exact")]
    ])
    await update.message.reply_text("请选择匹配模式：", reply_markup=keyboard)
    return True

async def handle_match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    playlist_url = context.user_data.pop('playlist_url', None)
    
    if not playlist_url:
        await query.edit_message_text("请重新发送歌单链接")
        return
    
    match_mode = "完全匹配" if query.data == 'match_exact' else "模糊匹配"
    await query.edit_message_text(
        f"🔄 **正在同步歌单**\n\n"
        f"▓░░░░░░░░░ 10%\n\n"
        f"📋 模式: `{match_mode}`\n"
        f"⏳ 正在获取歌单信息...",
        parse_mode='Markdown'
    )
    
    binding = get_user_binding(user_id)
    if not binding:
        await query.message.reply_text("请先绑定 Emby 账户")
        return
    
    try:
        result, error = await asyncio.to_thread(process_playlist, playlist_url, user_id, False, binding, match_mode)
        
        if error:
            await query.message.reply_text(f"❌ {error}")
        else:
            # 自动添加到定时同步列表
            playlist_type, _ = parse_playlist_input(playlist_url)
            if playlist_type and user_id == ADMIN_USER_ID:
                # 获取歌曲 ID 列表用于后续比较 (使用 source_id，与 check_playlist_updates 一致)
                song_ids = []
                # 从原始歌单获取
                if playlist_type == "netease":
                    _, songs = get_ncm_playlist_details(extract_playlist_id(playlist_url, 'netease'))
                else:
                    _, songs = get_qq_playlist_details(extract_playlist_id(playlist_url, 'qq'))
                if songs:
                    song_ids = [str(s.get('source_id') or s.get('id') or s.get('title', '')) for s in songs]
                add_scheduled_playlist(user_id, playlist_url, result['name'], playlist_type, song_ids)
            
            msg = f"✅ **歌单同步完成**\n\n"
            msg += f"📋 歌单: `{result['name']}`\n"
            msg += f"🎯 模式: `{result['mode']}`\n"
            msg += f"📊 总数: {result['total']} 首\n"
            msg += f"✅ 匹配: {result['matched']} 首\n"
            msg += f"❌ 未匹配: {result['unmatched']} 首\n"
            msg += f"📅 已添加到定时同步\n"
            
            # 检查是否可以自动下载（网易云歌单且有未匹配歌曲时）
            ncm_unmatched = [s for s in result.get('all_unmatched', result.get('unmatched_songs', [])) if s.get('platform') == 'NCM']
            all_unmatched = result.get('all_unmatched', result.get('unmatched_songs', []))
            
            if all_unmatched:
                # 保存所有未匹配歌曲用于翻页
                context.user_data['all_unmatched_songs'] = all_unmatched
                context.user_data['unmatched_page'] = 0
                
                msg += "\n**未匹配歌曲：**\n"
                page_size = 10
                for i, s in enumerate(all_unmatched[:page_size]):
                    msg += f"`{i+1}. {s['title']} - {s['artist']}`\n"
                if len(all_unmatched) > page_size:
                    msg += f"...还有 {len(all_unmatched) - page_size} 首\n"
            
            keyboard_buttons = []
            
            # 翻页按钮（如果超过10首）
            if len(all_unmatched) > 10:
                keyboard_buttons.append([
                    InlineKeyboardButton("📄 查看更多", callback_data="unmatched_page_1")
                ])
            
            if ncm_unmatched and user_id == ADMIN_USER_ID:
                # 保存未匹配歌曲到用户数据
                context.user_data['unmatched_ncm_songs'] = ncm_unmatched
                msg += f"\n💡 检测到 {len(ncm_unmatched)} 首网易云歌曲可自动下载"
                keyboard_buttons.append([
                    InlineKeyboardButton("📥 自动下载缺失歌曲", callback_data="download_missing")
                ])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None
            
            await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
    except Exception as e:
        logger.exception(f"处理歌单失败: {e}")
        await query.message.reply_text(f"处理失败: {e}")


async def handle_unmatched_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理未匹配歌曲翻页"""
    query = update.callback_query
    await query.answer()
    
    # 解析页码
    data = query.data  # unmatched_page_1
    try:
        page = int(data.split('_')[-1])
    except:
        page = 0
    
    all_unmatched = context.user_data.get('all_unmatched_songs', [])
    if not all_unmatched:
        await query.edit_message_text("未匹配歌曲列表已过期，请重新同步歌单")
        return
    
    page_size = 10
    total_pages = (len(all_unmatched) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(all_unmatched))
    
    # 构建消息
    msg = f"**未匹配歌曲** (第 {page + 1}/{total_pages} 页)\n\n"
    for i, s in enumerate(all_unmatched[start_idx:end_idx], start=start_idx + 1):
        msg += f"`{i}. {s['title']} - {s['artist']}`\n"
    
    msg += f"\n📊 共 {len(all_unmatched)} 首未匹配"
    
    # 构建翻页按钮
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"unmatched_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"unmatched_page_{page + 1}"))
    
    keyboard_buttons = []
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)
    
    # 如果有网易云歌曲且是管理员，显示下载按钮
    user_id = str(query.from_user.id)
    ncm_unmatched = context.user_data.get('unmatched_ncm_songs', [])
    if ncm_unmatched and user_id == ADMIN_USER_ID:
        keyboard_buttons.append([
            InlineKeyboardButton("📥 自动下载缺失歌曲", callback_data="download_missing")
        ])
    
    keyboard = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None
    
    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)


async def handle_need_dl_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理需下载歌曲列表翻页"""
    query = update.callback_query
    await query.answer()
    
    # 解析 callback_data: need_dl_page_{page}_{playlist_id}
    data = query.data  # need_dl_page_0_123
    parts = data.split('_')
    try:
        page = int(parts[3])  # page number
        playlist_id = parts[4]  # playlist id
    except (IndexError, ValueError):
        await query.edit_message_text("参数错误，请重新操作")
        return
    
    # 从数据库读取 need_download 列表
    need_download = []
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (f'need_download_{playlist_id}',))
            row = cursor.fetchone()
            if row:
                value = row['value'] if isinstance(row, dict) else row[0]
                need_download = json.loads(value)
    except Exception as e:
        logger.warning(f"读取 need_download 列表失败: {e}")
    
    if not need_download:
        await query.edit_message_text("需下载歌曲列表已过期，请重新触发歌单更新检查")
        return
    
    page_size = 10
    total_pages = (len(need_download) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(need_download))
    
    # 构建消息
    msg = f"**需下载歌曲** (第 {page + 1}/{total_pages} 页)\n\n"
    for i, s in enumerate(need_download[start_idx:end_idx], start=start_idx + 1):
        msg += f"`{i}. {s.get('title', '')} - {s.get('artist', '')}`\n"
    
    msg += f"\n📊 共 {len(need_download)} 首需下载"
    
    # 构建翻页按钮
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"need_dl_page_{page - 1}_{playlist_id}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"need_dl_page_{page + 1}_{playlist_id}"))
    
    keyboard_buttons = []
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)
    
    # 添加下载按钮
    keyboard_buttons.append([
        InlineKeyboardButton("📥 下载全部", callback_data=f"sync_dl_{playlist_id}")
    ])
    
    keyboard = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None
    
    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)


async def handle_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理自动下载回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("仅管理员可使用此功能")
        return
    
    ncm_cookie = get_ncm_cookie()
    if not ncm_cookie:
        await query.edit_message_text("未配置网易云 Cookie，无法下载\n\n请在 Web 界面使用扫码登录或手动配置 Cookie")
        return
    
    unmatched_songs = context.user_data.get('unmatched_ncm_songs', [])
    ncm_songs = [s for s in unmatched_songs if s.get('platform') == 'NCM']
    
    if not ncm_songs:
        await query.edit_message_text("没有需要下载的网易云歌曲")
        return
    
    await query.edit_message_text(f"🔄 正在下载 {len(ncm_songs)} 首歌曲...\n\n请耐心等待，下载完成后会通知您。")
    
    try:
        # 动态导入下载模块
        from bot.ncm_downloader import MusicAutoDownloader
        
        # 从数据库读取下载设置
        ncm_settings = get_ncm_settings()
        download_quality = ncm_settings.get('ncm_quality', 'exhigh')
        download_mode = ncm_settings.get('download_mode', 'local')
        download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
        musictag_dir = ncm_settings.get('musictag_dir', '')
        
        # 确保下载目录存在
        download_path = Path(download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        
        # 获取 QQ 音乐 Cookie 用于降级下载
        qq_cookie = get_qq_cookie()
        
        downloader = MusicAutoDownloader(
            ncm_cookie, qq_cookie, str(download_path),
            proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
        )
        
        # 检查登录状态
        logged_in, info = downloader.check_ncm_login()
        if not logged_in:
            await query.message.reply_text("❌ 网易云 Cookie 已失效，请更新")
            return
        
        await query.message.reply_text(f"🎵 网易云登录成功: {info.get('nickname')} (VIP: {'是' if info.get('is_vip') else '否'})")
        
        # 创建进度消息
        progress_msg = await query.message.reply_text(
            make_progress_message("📥 下载中", 0, len(ncm_songs), "准备开始...")
        )
        last_update_time = [0]  # 用列表来允许在闭包中修改
        main_loop = asyncio.get_running_loop()  # 在主线程获取 loop
        
        async def update_progress(current, total, song):
            """更新下载进度"""
            import time as time_module
            now = time_module.time()
            # 限制更新频率，避免 Telegram API 限流
            if now - last_update_time[0] < 1.5:
                return
            last_update_time[0] = now
            try:
                song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                await progress_msg.edit_text(
                    make_progress_message("📥 下载中", current, total, song_name),
                    parse_mode='Markdown'
                )
            except:
                pass
        
        # 包装同步回调为异步
        def sync_progress_callback(current, total, song, status=None):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        # 开始下载
        success_results, failed_songs = await asyncio.to_thread(
            downloader.download_missing_songs,
            ncm_songs,
            download_quality,
            sync_progress_callback
        )
        
        # 检查是否有 Cookie 过期提示
        cookie_warning = ""
        if hasattr(downloader, 'qq_api') and downloader.qq_api:
            if getattr(downloader.qq_api, '_cookie_expired', False):
                cookie_warning = "\n\n⚠️ **QQ音乐 Cookie 已过期**\n请重新登录 y.qq.com 获取新Cookie"
        
        # 提取成功的文件路径列表
        success_files = [r['file'] for r in success_results]
        
        # 如果设置了 MusicTag 模式，移动文件到 MusicTag 目录
        moved_files = []
        if download_mode == 'musictag' and musictag_dir and success_files:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            
            for i, file_path in enumerate(success_files):
                try:
                    src = Path(file_path)
                    if not src.exists():
                        logger.warning(f"源文件不存在，跳过移动: {file_path}")
                        continue
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                    # 更新 success_results 中的文件路径，以便正确记录文件大小
                    success_results[i]['file'] = str(dst)
                    logger.info(f"已移动文件到 MusicTag: {src.name}")
                except Exception as e:
                    logger.error(f"移动文件失败 {file_path}: {e}")
        
        # 删除进度消息
        try:
            await progress_msg.delete()
        except:
            pass
        
        # 保存下载记录（按实际下载平台记录）
        save_download_record_v2(success_results, failed_songs, download_quality, user_id)
        
        # 构建完成消息
        success_rate = len(success_files) / max(len(ncm_songs), 1) * 100
        msg = f"✅ **下载完成**\n\n"
        msg += f"{make_progress_bar(len(success_files), len(ncm_songs))}\n\n"
        msg += f"🎵 音质: `{download_quality}`\n"
        msg += f"📊 成功: {len(success_files)}/{len(ncm_songs)} 首\n"
        
        # 统计平台分布
        ncm_count = sum(1 for r in success_results if r.get('platform') == 'NCM')
        qq_count = sum(1 for r in success_results if r.get('platform') == 'QQ')
        if qq_count > 0:
            msg += f"   • 网易云: {ncm_count} 首, QQ音乐: {qq_count} 首\n"
        
        if success_files:
            if moved_files:
                msg += f"\n📁 已转移到 MusicTag\n"
            else:
                msg += f"\n📁 已保存到本地\n"
        
        if failed_songs and len(failed_songs) <= 5:
            msg += "\n**❌ 下载失败：**\n"
            for s in failed_songs:
                msg += f"• `{s['title']}`\n"
        elif failed_songs:
            msg += f"\n❌ {len(failed_songs)} 首下载失败\n"
        
        # 添加 Cookie 过期警告
        if cookie_warning:
            msg += cookie_warning
        
        await query.message.reply_text(msg, parse_mode='Markdown')
        
        # 自动触发 Emby 扫库（仅本地模式）
        if success_files and not moved_files:
            binding = get_user_binding(user_id)
            if binding:
                try:
                    user_access_token, user_id_emby = authenticate_emby(
                        EMBY_URL, binding['emby_username'], decrypt_password(binding['emby_password'])
                    )
                    if user_access_token:
                        user_auth = {'access_token': user_access_token, 'user_id': user_id_emby}
                        if trigger_emby_library_scan(user_auth):
                            await query.message.reply_text("🔄 已自动触发 Emby 媒体库扫描，请稍等几分钟后重新同步歌单")
                        else:
                            await query.message.reply_text("💡 提示：请使用 /rescan 刷新 Emby 媒体库")
                except Exception as e:
                    logger.exception(f"自动扫库失败: {e}")
                    await query.message.reply_text("💡 提示：请使用 /rescan 刷新 Emby 媒体库")
        
    except ImportError as e:
        logger.error(f"导入下载模块失败: {e}")
        await query.message.reply_text("❌ 下载模块未正确安装，请检查 pycryptodome 和 mutagen 依赖")
    except Exception as e:
        logger.exception(f"下载失败: {e}")
        await query.message.reply_text(f"❌ 下载失败: {e}")


# ============================================================
# Telegram 命令处理 - 音乐上传
# ============================================================

def check_user_permission(telegram_id: str, permission: str) -> bool:
    """检查用户权限"""
    # 管理员始终有权限
    if telegram_id == ADMIN_USER_ID:
        return True
    
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('SELECT * FROM user_permissions WHERE telegram_id = ?', (telegram_id,))
            row = cursor.fetchone()
            if row:
                if permission == 'upload':
                    return bool(row['can_upload'] if isinstance(row, dict) else row[2])
                elif permission == 'request':
                    return bool(row['can_request'] if isinstance(row, dict) else row[3])
            # 默认允许
            return True
    except Exception as e:
        logger.error(f"检查用户权限失败: {e}")
    return True


async def handle_audio_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理音频上传"""
    user_id = str(update.effective_user.id)
    message = update.message
    
    # 检查上传权限
    if not check_user_permission(user_id, 'upload'):
        await message.reply_text("❌ 你没有上传权限，请联系管理员")
        return True
    
    # 获取文件信息
    if message.audio:
        file = message.audio
        original_name = file.file_name or f"{file.title or 'audio'}.mp3"
    elif message.document:
        file = message.document
        original_name = file.file_name or "unknown"
        # 检查是否是音频文件
        mime = file.mime_type or ""
        if not (mime.startswith('audio/') or original_name.lower().endswith(ALLOWED_AUDIO_EXTENSIONS)):
            return False
    else:
        return False
    
    file_size = file.file_size or 0
    
    # 大于 20MB 的文件由 Pyrogram 处理，这里跳过
    if file_size > 20 * 1024 * 1024:
        if pyrogram_client:
            # Pyrogram 已启用，大文件会由它处理
            return True
        else:
            await message.reply_text(f"❌ 文件太大 ({file_size / 1024 / 1024:.1f} MB)，请配置 TG_API_ID/TG_API_HASH 启用大文件上传")
            return True
    
    try:
        status_msg = await message.reply_text(f"📥 正在下载: {original_name}...")
        
        # 获取下载设置
        ncm_settings = get_ncm_settings()
        download_mode = ncm_settings.get('download_mode', 'local')
        download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
        musictag_dir = ncm_settings.get('musictag_dir', '')
        
        # 确保目录存在
        download_path = Path(download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        
        # 下载文件
        tg_file = await context.bot.get_file(file.file_id)
        temp_path = UPLOAD_DIR / original_name
        await tg_file.download_to_drive(temp_path)
        
        # 清理文件名并移动到下载目录
        clean_name = clean_filename(original_name)
        target_path = download_path / clean_name
        
        # 如果目标已存在，删除
        if target_path.exists():
            target_path.unlink()
        
        shutil.move(str(temp_path), str(target_path))
        
        # 如果是 MusicTag 模式，继续移动到 MusicTag 目录
        final_path = target_path
        if download_mode == 'musictag' and musictag_dir:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            final_dest = musictag_path / clean_name
            shutil.move(str(target_path), str(final_dest))
            final_path = final_dest
            logger.info(f"已移动上传文件到 MusicTag: {clean_name}")
        
        # 记录
        save_upload_record(user_id, original_name, clean_name, file_size)
        
        size_mb = file_size / 1024 / 1024
        if download_mode == 'musictag' and musictag_dir:
            await status_msg.edit_text(f"✅ 上传成功！\n\n📁 文件: `{clean_name}`\n📦 大小: {size_mb:.2f} MB\n📂 已转移到 MusicTag 目录")
        else:
            await status_msg.edit_text(f"✅ 上传成功！\n\n📁 文件: `{clean_name}`\n📦 大小: {size_mb:.2f} MB")
        
    except Exception as e:
        logger.exception(f"上传失败: {e}")
        await message.reply_text(f"❌ 上传失败: {e}")
    
    return True


# ============================================================
# Telegram 命令处理 - 设置和状态
# ============================================================

async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if get_user_binding(user_id):
        await update.message.reply_text("您已绑定账户，如需重新绑定请先 /unbind")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("格式: /bemby <用户名> <密码>")
        return
    
    username = context.args[0]
    password = ' '.join(context.args[1:])
    
    token, emby_user_id = authenticate_emby(EMBY_URL, username, password)
    if not token:
        await update.message.reply_text("绑定失败：Emby 登录失败")
        return
    
    if save_user_binding(user_id, username, password, emby_user_id):
        await update.message.reply_text(f"✅ 绑定成功！\n用户名: {username}")
    else:
        await update.message.reply_text("绑定失败")

async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    binding = get_user_binding(user_id)
    
    if not binding:
        await update.message.reply_text("您尚未绑定账户")
        return
    
    if delete_user_binding(user_id):
        await update.message.reply_text(f"已解除绑定: {binding['emby_username']}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    msg = f"""
📊 **TGmusicbot 状态**

🎵 Emby 媒体库: {stats.get('library_songs', 0)} 首歌曲
👥 绑定用户: {stats.get('users', 0)}
📋 同步歌单: {stats.get('playlists', 0)} 个
🎶 同步歌曲: {stats.get('songs_synced', 0)} 首
📤 上传文件: {stats.get('uploads', 0)} 个
"""
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_ncm_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """检查网易云登录状态"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    ncm_cookie = get_ncm_cookie()
    if not ncm_cookie:
        await update.message.reply_text("❌ 未配置网易云 Cookie\n\n请在 Web 界面使用扫码登录，或在 .env 文件中添加 NCM_COOKIE")
        return
    
    await update.message.reply_text("🔄 正在检查网易云登录状态...")
    
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        logged_in, info = api.check_login()
        
        # 获取数据库设置
        ncm_settings = get_ncm_settings()
        quality_names = {
            'standard': '标准音质 (128kbps)',
            'higher': '较高音质 (192kbps)',
            'exhigh': '极高音质 (320kbps)',
            'lossless': '无损音质 (FLAC)',
            'hires': 'Hi-Res'
        }
        quality_display = quality_names.get(ncm_settings['ncm_quality'], ncm_settings['ncm_quality'])
        
        if logged_in:
            msg = f"✅ **网易云登录状态**\n\n"
            msg += f"👤 昵称: `{info.get('nickname', '未知')}`\n"
            msg += f"🆔 用户ID: `{info.get('user_id', '未知')}`\n"
            msg += f"💎 VIP: {'是' if info.get('is_vip') else '否'}\n"
            msg += f"📊 VIP类型: {info.get('vip_type', 0)}\n\n"
            msg += f"🎵 下载音质: `{quality_display}`\n"
            msg += f"🔄 自动下载: {'已启用' if ncm_settings['auto_download'] else '未启用'}\n"
            msg += f"📁 下载目录: `{MUSIC_TARGET_DIR}`"
        else:
            msg = "❌ 网易云 Cookie 已失效\n\n请在 Web 界面使用扫码登录"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except ImportError:
        await update.message.reply_text("❌ 下载模块未安装\n\n请确保已安装 pycryptodome 和 mutagen")
    except Exception as e:
        await update.message.reply_text(f"❌ 检查失败: {e}")

async def cmd_rescan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    await update.message.reply_text("开始扫描 Emby 媒体库...")
    binding = get_user_binding(user_id)
    
    if binding:
        token, emby_user_id = authenticate_emby(EMBY_URL, binding['emby_username'], binding['emby_password'])
        new_data = await asyncio.to_thread(scan_emby_library, True, emby_user_id, token)
    else:
        new_data = await asyncio.to_thread(scan_emby_library, True)
    
    await update.message.reply_text(f"✅ 扫描完成，共 {len(new_data)} 首歌曲")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """搜索歌曲"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /search <关键词>\n例如: /search 周杰伦 晴天")
        return
    
    keyword = ' '.join(context.args)
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await update.message.reply_text("❌ 未配置网易云 Cookie")
        return
    
    # 检查缓存
    cache_key = ('ncm', keyword.lower())
    cached = _cmd_search_cache.get(cache_key)
    if cached and time.time() - cached[0] < _cmd_search_cache_ttl:
        results = cached[1]
        logger.debug(f"使用缓存的搜索结果: {keyword}")
    else:
        await update.message.reply_text(f"🔍 正在搜索: {keyword}...")
        
        try:
            from bot.ncm_downloader import NeteaseMusicAPI
            api = NeteaseMusicAPI(ncm_cookie)
            results = api.search_song(keyword, limit=10)
            
            # 缓存结果
            _cmd_search_cache[cache_key] = (time.time(), results)
            
            # 清理过期缓存
            if len(_cmd_search_cache) > 50:
                now = time.time()
                expired = [k for k, v in _cmd_search_cache.items() if now - v[0] > _cmd_search_cache_ttl]
                for k in expired:
                    _cmd_search_cache.pop(k, None)
        except Exception as e:
            logger.exception(f"搜索失败: {e}")
            await update.message.reply_text(f"❌ 搜索失败: {e}")
            return
    
    try:
        if not results:
            await update.message.reply_text("未找到相关歌曲")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['search_results'] = results
        
        msg = f"🎵 *搜索结果* \\({len(results)} 首\\)\n\n"
        keyboard_buttons = []
        
        for i, song in enumerate(results):
            title = escape_markdown(song['title'])
            artist = escape_markdown(song['artist'])
            album = escape_markdown(song.get('album', '未知专辑'))
            msg += f"`{i+1}\\.` {title} \\- {artist}\n"
            msg += f"    📀 {album}\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {i+1}. {song['title'][:20]}", callback_data=f"dl_song_{i}")
            ])
        
        keyboard_buttons.append([InlineKeyboardButton("📥 全部下载", callback_data="dl_song_all")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"搜索失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """搜索并下载专辑"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /album <专辑名或关键词>\n例如: /album 范特西")
        return
    
    keyword = ' '.join(context.args)
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await update.message.reply_text("❌ 未配置网易云 Cookie")
        return
    
    await update.message.reply_text(f"🔍 正在搜索专辑: {keyword}...")
    
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        results = api.search_album(keyword, limit=5)
        
        if not results:
            await update.message.reply_text("未找到相关专辑")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['album_results'] = results
        
        msg = f"💿 *专辑搜索结果* \\({len(results)} 张\\)\n\n"
        keyboard_buttons = []
        
        for i, album in enumerate(results):
            album_name = escape_markdown(album['name'])
            artist = escape_markdown(album['artist'])
            msg += f"`{i+1}\\.` {album_name}\n"
            msg += f"    🎤 {artist} · {album['size']} 首歌\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {album['name'][:25]}", callback_data=f"dl_album_{i}")
            ])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"搜索专辑失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_qq_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QQ音乐搜索歌曲"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /qs <关键词>\n例如: /qs 周杰伦 晴天")
        return
    
    keyword = ' '.join(context.args)
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await update.message.reply_text("❌ 未配置 QQ音乐 Cookie，请在 Web 设置中配置")
        return
    
    # 检查缓存
    cache_key = ('qq', keyword.lower())
    cached = _cmd_search_cache.get(cache_key)
    if cached and time.time() - cached[0] < _cmd_search_cache_ttl:
        results = cached[1]
        logger.debug(f"使用缓存的 QQ 搜索结果: {keyword}")
    else:
        await update.message.reply_text(f"🔍 正在搜索 QQ音乐: {keyword}...")
        
        try:
            from bot.ncm_downloader import QQMusicAPI
            api = QQMusicAPI(qq_cookie)
            results = api.search_song(keyword, limit=10)
            
            # 缓存结果
            _cmd_search_cache[cache_key] = (time.time(), results)
        except Exception as e:
            logger.exception(f"QQ音乐搜索失败: {e}")
            await update.message.reply_text(f"❌ 搜索失败: {e}")
            return
    
    try:
        if not results:
            await update.message.reply_text("未找到相关歌曲")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['qq_search_results'] = results
        
        msg = f"🎵 *QQ音乐搜索结果* \\({len(results)} 首\\)\n\n"
        keyboard_buttons = []
        
        for i, song in enumerate(results):
            title = escape_markdown(song['title'])
            artist = escape_markdown(song['artist'])
            album = escape_markdown(song.get('album', '未知专辑'))
            msg += f"`{i+1}\\.` {title} \\- {artist}\n"
            msg += f"    📀 {album}\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {i+1}. {song['title'][:20]}", callback_data=f"qdl_song_{i}")
            ])
        
        keyboard_buttons.append([InlineKeyboardButton("📥 全部下载", callback_data="qdl_song_all")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"QQ音乐搜索失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_qq_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QQ音乐搜索并下载专辑"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /qa <专辑名或关键词>\n例如: /qa 范特西")
        return
    
    keyword = ' '.join(context.args)
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await update.message.reply_text("❌ 未配置 QQ音乐 Cookie，请在 Web 设置中配置")
        return
    
    await update.message.reply_text(f"🔍 正在搜索 QQ音乐专辑: {keyword}...")
    
    try:
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(qq_cookie)
        results = api.search_album(keyword, limit=5)
        
        if not results:
            await update.message.reply_text("未找到相关专辑")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['qq_album_results'] = results
        
        msg = f"💿 *QQ音乐专辑搜索结果* \\({len(results)} 张\\)\n\n"
        keyboard_buttons = []
        
        for i, album in enumerate(results):
            album_name = escape_markdown(album['name'])
            artist = escape_markdown(album['artist'])
            msg += f"`{i+1}\\.` {album_name}\n"
            msg += f"    🎤 {artist} · {album['size']} 首歌\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {album['name'][:25]}", callback_data=f"qdl_album_{i}")
            ])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"QQ音乐搜索专辑失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


# ============================================================
# 下载管理命令
# ============================================================

def format_file_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


async def cmd_download_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看下载状态 /ds"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("📊 下载管理器未启用\n\n使用传统下载模式")
        return
    
    stats = manager.get_stats()
    queue = stats['queue']
    today = stats['today']
    
    msg = "📊 **下载状态**\n\n"
    
    # 队列状态
    msg += "**📥 下载队列**\n"
    msg += f"├ 等待中: {queue['pending']}\n"
    msg += f"├ 下载中: {queue['downloading']}\n"
    msg += f"├ 重试中: {queue['retrying']}\n"
    msg += f"├ 已完成: {queue['completed']}\n"
    msg += f"└ 失败: {queue['failed']}\n\n"
    
    # 今日统计
    msg += "**📈 今日统计**\n"
    msg += f"├ 成功: {today['total_success']} 首\n"
    msg += f"├ 失败: {today['total_fail']} 首\n"
    msg += f"└ 总大小: {format_file_size(today['total_size'])}\n\n"
    
    # 平台分布
    if today['by_platform']:
        msg += "**🎵 平台分布**\n"
        for platform, data in today['by_platform'].items():
            msg += f"├ {platform}: {data['success']} 成功 / {data['fail']} 失败\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_download_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看下载队列 /dq"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("📭 下载管理器未启用")
        return
    
    queue_status = manager.get_queue_status()
    tasks = queue_status['tasks']
    
    if not tasks:
        await update.message.reply_text("📭 下载队列为空")
        return
    
    msg = f"📥 **下载队列** ({queue_status['total']} 个任务)\n\n"
    
    status_emoji = {
        'pending': '⏳',
        'downloading': '📥',
        'completed': '✅',
        'failed': '❌',
        'retrying': '🔄',
        'cancelled': '🚫'
    }
    
    for i, task in enumerate(tasks[-10:], 1):
        emoji = status_emoji.get(task['status'], '❓')
        name = task.get('title', '未知')[:25]
        artist = task.get('artist', '')[:15]
        msg += f"{emoji} `{name}` - {artist}\n"
    
    if len(tasks) > 10:
        msg += f"\n... 还有 {len(tasks) - 10} 个任务"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_download_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看下载历史 /dh"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("📭 下载管理器未启用")
        return
    
    history = manager.stats.get_recent_history(20)
    
    if not history:
        await update.message.reply_text("📭 暂无下载历史")
        return
    
    msg = "📜 **最近下载历史**\n\n"
    
    status_emoji = {
        'completed': '✅',
        'failed': '❌',
    }
    
    for item in history:
        emoji = status_emoji.get(item['status'], '❓')
        title = (item.get('title') or '未知')[:20]
        artist = (item.get('artist') or '')[:12]
        platform = item.get('platform', '?')
        
        msg += f"{emoji} `{title}` - {artist} [{platform}]\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


# ============================================================
# 定时任务 (注: scheduled_sync_job 和 scheduled_emby_scan_job 的主实现在文件后面)
# ============================================================


async def check_expired_users_job(application):
    """检查并禁用过期会员的定时任务 - 每小时执行一次"""
    import asyncio
    from datetime import datetime
    
    logger.info("过期会员检查任务已启动")
    
    while True:
        try:
            await asyncio.sleep(3600)  # 每小时检查一次
            
            conn = sqlite3.connect(str(DATABASE_FILE), check_same_thread=False)
            cursor = conn.cursor()
            
            # 查找已过期但仍活跃的用户
            now = datetime.now().isoformat()
            cursor.execute('''
                SELECT id, username, emby_user_id, expire_at 
                FROM web_users 
                WHERE expire_at IS NOT NULL 
                  AND expire_at < ? 
                  AND is_active = 1
                  AND emby_user_id IS NOT NULL
            ''', (now,))
            
            expired_users = cursor.fetchall()
            
            if expired_users:
                logger.info(f"发现 {len(expired_users)} 个过期用户，正在禁用...")
                
                from bot.services.emby import disable_emby_user
                
                for user in expired_users:
                    user_id, username, emby_user_id, expire_at = user
                    
                    # 禁用 Emby 账号
                    result = await asyncio.to_thread(disable_emby_user, emby_user_id)
                    
                    if result.get('success'):
                        # 更新数据库状态
                        cursor.execute('''
                            UPDATE web_users SET is_active = 0 WHERE id = ?
                        ''', (user_id,))
                        logger.info(f"已禁用过期用户: {username} (过期时间: {expire_at})")
                    else:
                        logger.warning(f"禁用用户失败: {username} - {result.get('error')}")
                
                conn.commit()
            
            conn.close()
            
        except Exception as e:
            logger.error(f"过期用户检查任务异常: {e}")
            await asyncio.sleep(60)


async def daily_stats_job(application):
    """每日统计报告任务 - 基于数据库配置发送"""
    import datetime as dt
    import asyncio
    from bot.utils.database import get_database
    from bot.services.playback_stats import get_playback_stats
    from bot.utils.ranking_image import generate_daily_ranking_image
    from io import BytesIO
    from bot.config import ADMIN_USER_ID

    logger.info("每日统计任务已启动")
    
    while True:
        try:
            # 1. Check Config
            db = get_database()
            daily_time_str = db.get_setting('ranking_daily_time', '')
            target_chat_str = db.get_setting('ranking_target_chat', '')
            
            if not daily_time_str:
                # Disabled
                await asyncio.sleep(60)
                continue
            
            # 2. Check Time
            now = dt.datetime.now()
            target_time = dt.datetime.strptime(daily_time_str, "%H:%M").time()
            
            # If current minute matches target minute
            if now.hour == target_time.hour and now.minute == target_time.minute:
                logger.info(f"触发每日统计推送: {daily_time_str}")
                
                # Fetch Data
                stats_svc = get_playback_stats()
                data = stats_svc.get_global_daily_stats()
                
                # Debug logging
                logger.info(f"[DailyPush] Data received: leaderboard={len(data.get('leaderboard', []))}, top_songs={len(data.get('top_songs', []))}")
                
                target_id = target_chat_str.strip() if target_chat_str else ADMIN_USER_ID
                logger.info(f"[DailyPush] target_chat_str={target_chat_str}, target_id={target_id}")
                
                if not target_id:
                    logger.info("未配置推送目标，跳过")
                elif data and data.get('leaderboard'):
                    try:
                        img_bytes = generate_daily_ranking_image(data, emby_url=stats_svc.emby_url, emby_token=stats_svc.emby_token)
                        if img_bytes:
                            # 生成完整歌曲列表 caption (和 /daily 命令一致)
                            from bot.config import DAILY_RANKING_SUBTITLE
                            import sqlite3
                            
                            ranking_subtitle = DAILY_RANKING_SUBTITLE
                            try:
                                with sqlite3.connect(DATABASE_FILE) as conn:
                                    cursor = conn.cursor()
                                    cursor.execute("SELECT value FROM bot_settings WHERE key = 'ranking_daily_subtitle'")
                                    row = cursor.fetchone()
                                    if row and row[0]:
                                        ranking_subtitle = row[0]
                            except:
                                pass
                            
                            caption_lines = [
                                f"【{ranking_subtitle} 播放日榜】\n",
                                "▎热门歌曲：\n"
                            ]
                            
                            top_songs = data.get('top_songs', [])[:10]
                            for i, song in enumerate(top_songs):
                                title = song.get('title', 'Unknown')
                                artist = song.get('artist', 'Unknown')
                                album = song.get('album', '')
                                count = song.get('count', 0)
                                
                                caption_lines.append(f"{i+1}. {title}")
                                if artist and artist != 'Unknown':
                                    caption_lines.append(f"歌手: {artist}")
                                if album:
                                    caption_lines.append(f"专辑: {album}")
                                caption_lines.append(f"播放次数: {count}")
                                caption_lines.append("")
                            
                            caption_lines.append(f"\n#DayRanks  {data.get('date', '')}")
                            caption = "\n".join(caption_lines)
                            
                            if len(caption) > 1024:
                                caption = caption[:1020] + "..."
                            
                            await application.bot.send_photo(
                                chat_id=int(target_id) if str(target_id).lstrip('-').isdigit() else target_id,
                                photo=BytesIO(img_bytes),
                                caption=caption
                            )
                            logger.info(f"每日统计推送成功 -> {target_id}")
                        else:
                            logger.error("生成每日统计图片失败")
                    except Exception as e:
                        logger.error(f"每日统计推送异常: {e}")
                else:
                    # 即使没有数据也发送一条消息
                    try:
                        await application.bot.send_message(
                            chat_id=int(target_id) if str(target_id).lstrip('-').isdigit() else target_id,
                            text="📅 每日听歌榜\n\n今日暂无播放数据 🎵"
                        )
                        logger.info(f"每日统计推送(无数据) -> {target_id}")
                    except Exception as e:
                        logger.error(f"发送无数据消息失败: {e}")
                
                # Wait 61s to avoid double send
                await asyncio.sleep(61)
            else:
                # Sleep until next minute check
                await asyncio.sleep(30)
                
        except Exception as e:
            logger.error(f"每日任务循环错误: {e}")
            await asyncio.sleep(60)


async def cookie_check_job(application):
    """Cookie 过期检查任务 - 每6小时检查一次"""
    # 启动后等待 1 分钟再执行第一次检查
    await asyncio.sleep(60)
    
    while True:
        try:
            logger.info("检查 Cookie 状态...")
            
            notifications = []
            
            # 检查网易云 Cookie
            ncm_cookie = get_ncm_cookie()
            if ncm_cookie:
                try:
                    from bot.ncm_downloader import NeteaseMusicAPI
                    api = NeteaseMusicAPI(ncm_cookie)
                    logged_in, info = api.check_login()
                    if not logged_in:
                        notifications.append("🔴 **网易云 Cookie 已失效**\n请重新登录获取 Cookie")
                    else:
                        logger.info(f"网易云 Cookie 有效: {info.get('nickname', '未知')}")
                except Exception as e:
                    logger.error(f"检查网易云 Cookie 失败: {e}")
            
            # 检查 QQ Cookie
            qq_cookie = get_qq_cookie()
            if qq_cookie:
                try:
                    from bot.ncm_downloader import QQMusicAPI
                    api = QQMusicAPI(qq_cookie)
                    logged_in, info = api.check_login()
                    if not logged_in:
                        notifications.append("🔴 **QQ音乐 Cookie 已失效**\n请重新登录获取 Cookie")
                    else:
                        logger.info(f"QQ音乐 Cookie 有效: {info.get('nickname', '未知')}")
                except Exception as e:
                    logger.error(f"检查 QQ Cookie 失败: {e}")
            
            # 发送通知
            if notifications and ADMIN_USER_ID:
                msg = "⚠️ **Cookie 状态告警**\n\n" + "\n\n".join(notifications)
                msg += "\n\n💡 请在 Web 管理界面重新配置 Cookie"
                
                await application.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=msg,
                    parse_mode='Markdown'
                )
                logger.warning("已发送 Cookie 过期通知")
            
            # 等待 6 小时后再次检查
            await asyncio.sleep(6 * 3600)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cookie 检查任务错误: {e}")
            await asyncio.sleep(3600)


# ============================================================
# Inline 模式搜索（任意聊天中 @bot 歌名 搜索）
# ============================================================

# 搜索结果缓存
_search_cache = {}
_cache_ttl = 300  # 5分钟

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 Inline 查询 - 任意聊天中 @bot 歌名 搜索"""
    query = update.inline_query
    search_text = query.query.strip()
    
    if not search_text or len(search_text) < 2:
        return
    
    # 检查缓存
    cache_key = search_text.lower()
    if cache_key in _search_cache:
        cached_time, cached_results = _search_cache[cache_key]
        if time.time() - cached_time < _cache_ttl:
            await query.answer(cached_results, cache_time=60)
            return
    
    results = []
    
    try:
        # 搜索网易云
        ncm_cookie = get_ncm_cookie()
        if ncm_cookie:
            from bot.ncm_downloader import NeteaseMusicAPI
            api = NeteaseMusicAPI(ncm_cookie)
            songs = api.search_songs(search_text, limit=5)
            
            for i, song in enumerate(songs):
                song_id = song.get('id', '')
                title = song.get('title', '未知')
                artist = song.get('artist', '未知')
                album = song.get('album', '')
                
                # 创建结果
                results.append(
                    InlineQueryResultArticle(
                        id=f"ncm_{song_id}",
                        title=f"🔴 {title}",
                        description=f"{artist} · {album}" if album else artist,
                        input_message_content=InputTextMessageContent(
                            message_text=f"🎵 *{title}*\n👤 {artist}\n💿 {album}\n\n🔗 网易云: https://music.163.com/song?id={song_id}",
                            parse_mode='Markdown'
                        ),
                        thumbnail_url=song.get('cover', '')
                    )
                )
        
        # 搜索 QQ 音乐
        qq_cookie = get_qq_cookie()
        if qq_cookie:
            from bot.ncm_downloader import QQMusicAPI
            api = QQMusicAPI(qq_cookie)
            songs = api.search_songs(search_text, limit=5)
            
            for i, song in enumerate(songs):
                song_id = song.get('id', '')
                mid = song.get('mid', '')
                title = song.get('title', '未知')
                artist = song.get('artist', '未知')
                album = song.get('album', '')
                
                results.append(
                    InlineQueryResultArticle(
                        id=f"qq_{song_id}",
                        title=f"🟢 {title}",
                        description=f"{artist} · {album}" if album else artist,
                        input_message_content=InputTextMessageContent(
                            message_text=f"🎵 *{title}*\n👤 {artist}\n💿 {album}\n\n🔗 QQ音乐: https://y.qq.com/n/ryqq/songDetail/{mid}",
                            parse_mode='Markdown'
                        ),
                        thumbnail_url=song.get('cover', '')
                    )
                )
        
        # 缓存结果
        _search_cache[cache_key] = (time.time(), results)
        
        # 清理过期缓存
        if len(_search_cache) > 100:
            now = time.time()
            _search_cache.clear()
        
    except Exception as e:
        logger.error(f"Inline 搜索失败: {e}")
    
    await query.answer(results, cache_time=60)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看定时同步歌单"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    playlists = get_scheduled_playlists(user_id)
    
    if not playlists:
        await update.message.reply_text(
            "📅 **定时同步歌单**\n\n"
            "暂无订阅的歌单\n\n"
            "💡 同步歌单后会自动添加到定时同步列表",
            parse_mode='Markdown'
        )
        return
    
    default_interval = get_playlist_sync_interval()
    msg = "📅 **定时同步歌单**\n\n"
    for i, p in enumerate(playlists, 1):
        platform_icon = "🔴" if p['platform'] == 'netease' else "🟢"
        last_sync = p['last_sync_at'][:16] if p['last_sync_at'] else "未同步"
        interval = p.get('sync_interval') or default_interval
        interval = max(MIN_PLAYLIST_SYNC_INTERVAL_MINUTES, interval)
        if interval >= 60:
            hours = interval // 60
            minutes = interval % 60
            if minutes:
                interval_str = f"{hours}h{minutes}m"
            else:
                interval_str = f"{hours}h"
        else:
            interval_str = f"{interval}m"
        msg += f"`{i}.` {platform_icon} {p['playlist_name']}\n"
        msg += f"    📊 {len(p['last_song_ids'])} 首 · ⏱ {interval_str} · 最后同步: {last_sync}\n\n"
    
    msg += "💡 使用 `/unschedule <序号>` 取消订阅\n"
    msg += "💡 使用 `/syncinterval <序号> <分钟>` 设置同步间隔"
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_syncinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置歌单同步间隔 /syncinterval"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    default_interval = get_playlist_sync_interval()
    if not context.args:
        msg = "⏱ **歌单同步间隔设置**\n\n"
        msg += f"📊 当前默认间隔: **{default_interval} 分钟**\n\n"
        msg += "**用法：**\n"
        msg += "• `/syncinterval <序号> <分钟>` - 设置指定歌单的同步间隔\n"
        msg += "• `/syncinterval default <分钟>` - 设置全局默认间隔\n"
        msg += "\n**示例：**\n"
        msg += "• `/syncinterval 1 30` - 第1个歌单每30分钟同步\n"
        msg += "• `/syncinterval default 60` - 全局默认每60分钟同步\n"
        msg += f"\n💡 最小间隔: {MIN_PLAYLIST_SYNC_INTERVAL_MINUTES} 分钟"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return
    if context.args[0].lower() == 'default':
        if len(context.args) < 2:
            await update.message.reply_text("用法: `/syncinterval default <分钟>`", parse_mode='Markdown')
            return
        try:
            interval = int(context.args[1])
            if interval < MIN_PLAYLIST_SYNC_INTERVAL_MINUTES:
                await update.message.reply_text(f"❌ 间隔不能小于 {MIN_PLAYLIST_SYNC_INTERVAL_MINUTES} 分钟")
                return
            if interval > 10080:
                await update.message.reply_text("❌ 间隔不能超过 10080 分钟（一周）")
                return
            if database_conn:
                ensure_bot_settings_table()
                cursor = database_conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                ''', ('playlist_sync_interval', str(interval), datetime.now().isoformat()))
                database_conn.commit()
            else:
                await update.message.reply_text("❌ 数据库未初始化，无法保存设置")
                return
            await update.message.reply_text(f"✅ 已设置全局默认同步间隔为 **{interval} 分钟**", parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
        return
    try:
        index = int(context.args[0]) - 1
        if len(context.args) < 2:
            await update.message.reply_text("用法: `/syncinterval <序号> <分钟>`", parse_mode='Markdown')
            return
        interval = int(context.args[1])
        if interval < MIN_PLAYLIST_SYNC_INTERVAL_MINUTES:
            await update.message.reply_text(f"❌ 间隔不能小于 {MIN_PLAYLIST_SYNC_INTERVAL_MINUTES} 分钟")
            return
        if interval > 10080:
            await update.message.reply_text("❌ 间隔不能超过 10080 分钟（一周）")
            return
        playlists = get_scheduled_playlists(user_id)
        if index < 0 or index >= len(playlists):
            await update.message.reply_text("❌ 序号无效，请使用 /schedule 查看歌单列表")
            return
        playlist = playlists[index]
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('''
                UPDATE scheduled_playlists SET sync_interval = ?
                WHERE id = ? AND telegram_id = ?
            ''', (interval, playlist['id'], user_id))
            database_conn.commit()
        else:
            await update.message.reply_text("❌ 数据库未初始化，无法保存设置")
            return
        await update.message.reply_text(
            f"✅ 已设置歌单 **{playlist['playlist_name']}** 的同步间隔为 **{interval} 分钟**",
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("❌ 请输入有效的数字")


async def cmd_scaninterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置 Emby 媒体库自动扫描间隔"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    # 获取当前设置
    current_interval = EMBY_SCAN_INTERVAL
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
            row = cursor.fetchone()
            if row:
                current_interval = int(row[0] if isinstance(row, tuple) else row['value'])
    except:
        pass
    
    if not context.args:
        status = f"每 {current_interval} 小时" if current_interval > 0 else "已禁用"
        await update.message.reply_text(
            f"🔄 **Emby 媒体库自动扫描**\n\n"
            f"当前状态: {status}\n\n"
            f"用法: `/scaninterval <小时>`\n"
            f"示例:\n"
            f"• `/scaninterval 6` - 每 6 小时扫描\n"
            f"• `/scaninterval 0` - 禁用自动扫描\n\n"
            f"💡 也可在 Web 设置页面配置",
            parse_mode='Markdown'
        )
        return
    
    try:
        interval = int(context.args[0])
        if interval < 0:
            await update.message.reply_text("❌ 间隔不能为负数")
            return
        
        # 保存到数据库
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO bot_settings (key, value, updated_at)
                VALUES (?, ?, ?)
            ''', ('emby_scan_interval', str(interval), datetime.now().isoformat()))
            database_conn.commit()
        
        if interval == 0:
            await update.message.reply_text("✅ 已禁用 Emby 自动扫描")
        else:
            await update.message.reply_text(f"✅ 已设置 Emby 自动扫描间隔为 {interval} 小时")
            
    except ValueError:
        await update.message.reply_text("❌ 请输入有效的数字")


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消定时同步歌单"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /unschedule <序号>\n例如: /unschedule 1")
        return
    
    try:
        index = int(context.args[0]) - 1
        playlists = get_scheduled_playlists(user_id)
        
        if index < 0 or index >= len(playlists):
            await update.message.reply_text("❌ 序号无效")
            return
        
        playlist = playlists[index]
        if delete_scheduled_playlist(playlist['id'], user_id):
            await update.message.reply_text(f"✅ 已取消订阅: {playlist['playlist_name']}")
        else:
            await update.message.reply_text("❌ 取消失败")
    except ValueError:
        await update.message.reply_text("❌ 请输入有效的序号")


async def handle_sync_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理定时同步相关的回调"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # 忽略回调超时错误，不影响实际功能
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("无权执行此操作")
        return
    
    data = query.data
    
    if data.startswith("sync_dl_pending_"):
        # 下载之前 process_playlist 返回的未匹配歌曲
        pending_songs = context.user_data.get('pending_download_songs', [])
        if not pending_songs:
            await query.edit_message_text("❌ 没有待下载的歌曲，请重新同步歌单")
            return
        
        await query.edit_message_text(f"📥 开始下载 {len(pending_songs)} 首缺失歌曲...")
        
        try:
            ncm_cookie = get_ncm_cookie()
            if not ncm_cookie:
                await query.message.reply_text("❌ 未配置网易云 Cookie")
                return
            
            from bot.ncm_downloader import MusicAutoDownloader
            ncm_settings = get_ncm_settings()
            download_quality = ncm_settings.get('ncm_quality', 'exhigh')
            download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
            
            download_path = Path(download_dir)
            download_path.mkdir(parents=True, exist_ok=True)
            
            qq_cookie = get_qq_cookie()
            downloader = MusicAutoDownloader(
                ncm_cookie, qq_cookie, str(download_path),
                proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
            )
            
            progress_msg = await query.message.reply_text(
                make_progress_message("📥 下载缺失歌曲", 0, len(pending_songs), "准备开始...")
            )
            main_loop = asyncio.get_running_loop()
            last_update_time = [0]
            
            async def update_progress(current, total, song):
                import time as time_module
                now = time_module.time()
                if now - last_update_time[0] < 1.5:
                    return
                last_update_time[0] = now
                try:
                    song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                    await progress_msg.edit_text(
                        make_progress_message("📥 下载缺失歌曲", current, total, song_name),
                        parse_mode='Markdown'
                    )
                except:
                    pass
            
            def sync_progress_callback(current, total, song, status=None):
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
                )
            
            success_results, failed = await asyncio.to_thread(
                downloader.download_missing_songs,
                pending_songs,
                download_quality,
                sync_progress_callback
            )
            
            # 提取文件列表
            success_files = []
            for r in success_results:
                if isinstance(r, str):
                    success_files.append(r)
                elif isinstance(r, dict) and 'file' in r:
                    success_files.append(r['file'])
            
            try:
                await progress_msg.delete()
            except:
                pass
            
            # 保存下载记录
            save_download_record_v2(success_results, failed, download_quality, user_id)
            
            # 清理 context
            context.user_data.pop('pending_download_songs', None)
            
            await query.message.reply_text(
                f"✅ 下载完成\n成功: {len(success_files)} 首\n失败: {len(failed)} 首"
            )
            
            # 触发 Emby 扫库
            asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))
            
        except Exception as e:
            logger.exception(f"下载缺失歌曲失败: {e}")
            await query.message.reply_text(f"❌ 下载失败: {e}")
        return
    
    if data.startswith("sync_dl_unmatched_"):
        # 下载未匹配的歌曲（从数据库获取）
        playlist_id = int(data.replace("sync_dl_unmatched_", ""))
        
        try:
            # 从数据库获取未匹配歌曲
            cursor = database_conn.cursor()
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (f'unmatched_songs_{playlist_id}',))
            row = cursor.fetchone()
            
            if not row:
                await query.edit_message_text("❌ 未找到缺失歌曲记录")
                return
            
            unmatched_songs = json.loads(row[0])
            if not unmatched_songs:
                await query.edit_message_text("❌ 没有需要下载的歌曲")
                return
            
            await query.edit_message_text(f"📥 正在下载 {len(unmatched_songs)} 首缺失歌曲...")
            
            # 初始化下载器
            ncm_cookie = get_ncm_cookie()
            qq_cookie = get_qq_cookie()
            
            from bot.ncm_downloader import MusicAutoDownloader
            ncm_settings = get_ncm_settings()
            download_quality = ncm_settings.get('ncm_quality', 'exhigh')
            download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
            
            downloader = MusicAutoDownloader(
                ncm_cookie, qq_cookie, download_dir,
                proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
            )
            
            progress_msg = await query.message.reply_text(
                make_progress_message("📥 下载缺失歌曲", 0, len(unmatched_songs), "准备开始...")
            )
            main_loop = asyncio.get_running_loop()
            last_update_time = [0]
            
            async def update_progress(current, total, song):
                import time as time_module
                now = time_module.time()
                if now - last_update_time[0] < 1.5:
                    return
                last_update_time[0] = now
                try:
                    song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                    await progress_msg.edit_text(
                        make_progress_message("📥 下载缺失歌曲", current, total, song_name),
                        parse_mode='Markdown'
                    )
                except:
                    pass
            
            def sync_progress_callback(current, total, song, status=None):
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
                )
            
            success_results, failed = await asyncio.to_thread(
                downloader.download_missing_songs,
                unmatched_songs,
                download_quality,
                sync_progress_callback,
                False, # is_organize_mode
                None,  # organize_dir
                True   # fallback_to_qq
            )
            
            try:
                await progress_msg.delete()
            except:
                pass
            
            # 清理数据库中的临时记录
            cursor.execute('DELETE FROM bot_settings WHERE key = ?', (f'unmatched_songs_{playlist_id}',))
            database_conn.commit()
            
            # 保存下载记录
            save_download_record_v2(success_results, failed, download_quality, user_id)
            
            await query.message.reply_text(
                f"✅ **下载完成**\n\n"
                f"成功: {len(success_results)} 首\n"
                f"失败: {len(failed)} 首\n\n"
                f"下一次同步将自动把新歌曲添加到 Emby 歌单",
                parse_mode='Markdown'
            )
            
            # 触发 Emby 扫库
            asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))
            
        except Exception as e:
            logger.exception(f"下载未匹配歌曲失败: {e}")
            await query.message.reply_text(f"❌ 下载失败: {e}")
        return
    
    if data.startswith("sync_dl_"):
        # 下载新歌
        playlist_id = int(data.replace("sync_dl_", ""))
        playlists = get_scheduled_playlists(user_id)
        playlist = next((p for p in playlists if p['id'] == playlist_id), None)
        
        if not playlist:
            await query.edit_message_text("❌ 歌单不存在")
            return
        
        await query.edit_message_text("📥 正在获取新歌曲...")
        
        # 获取歌单并找出新歌曲
        try:
            platform = playlist['platform']
            playlist_url = playlist['playlist_url']
            playlist_name = playlist.get('playlist_name') or '订阅歌单'
            remote_name = None
            # 修复逻辑：不再依赖 last_song_ids 判断新歌（因为通知发出时已更新 DB，导致此处判空）
            #改为检查是否已在 Emby 库中或本地
            
            # 加载 Emby 缓存
            emby_library_data = []
            if os.path.exists(LIBRARY_CACHE_FILE):
                try:
                    with open(LIBRARY_CACHE_FILE, 'r', encoding='utf-8') as f:
                        cache = json.load(f)
                        emby_library_data = cache.get('items', [])
                except:
                    pass
            
            # 获取歌单歌曲列表
            if platform == 'netease':
                p_id = extract_playlist_id(playlist_url, 'netease')
                remote_name, songs = get_ncm_playlist_details(p_id)
            else:
                p_id = extract_playlist_id(playlist_url, 'qq')
                remote_name, songs = get_qq_playlist_details(p_id)
            
            if not songs:
                await query.edit_message_text("❌ 获取歌单内容失败")
                return
            
            new_songs = []
            for s in songs:
                # 检查 Emby
                if emby_library_data:
                    title = s.get('title', '').lower()
                    found = any(
                        (title in item.get('title', '').lower() or 
                         item.get('title', '').lower() in title)
                        for item in emby_library_data
                    )
                    if not found:
                        new_songs.append(s)
                else:
                    # 如果没有 Emby 数据，则默认全部下载（或者可以加本地文件检查，但这里简化处理）
                    # 为了避免每次全量下载，这里做一个妥协：
                    # 如果 old_song_ids 为空（首次），全量下载
                    # 如果 old_song_ids 不为空，且当前歌曲ID不在其中，则下载
                    # 但考虑到 "通知后立即更新ID" 的 Bug，我们这里应该忽略 old_song_ids
                    # 更好的方式是：如果没有 Emby，我们检查本地文件是否存在
                    
                    # 简单检查本地文件是否存在 (基于文件名预测)
                    # 这种检查不一定准确，但比直接返回空好
                    filename_guess = clean_filename(f"{s.get('title', '')} - {s.get('artist', '')}")
                    # 在下载目录搜索
                    download_dir = get_ncm_settings().get('download_dir', str(MUSIC_TARGET_DIR))
                    found_local = False
                    for ext in ['.mp3', '.flac', '.m4a']:
                        if os.path.exists(os.path.join(download_dir, filename_guess + ext)):
                            found_local = True
                            break
                    
                    if not found_local:
                        new_songs.append(s)

            if not new_songs:
                await query.edit_message_text(f"✅ 所有歌曲似乎都已下载/存在于库中 (共 {len(songs)} 首)")
                return
            
            # 开始下载
            ncm_cookie = get_ncm_cookie()
            if not ncm_cookie:
                await query.message.reply_text("❌ 未配置网易云 Cookie")
                return
            
            from bot.ncm_downloader import MusicAutoDownloader
            ncm_settings = get_ncm_settings()
            download_quality = ncm_settings.get('ncm_quality', 'exhigh')
            download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
            
            download_path = Path(download_dir)
            download_path.mkdir(parents=True, exist_ok=True)
            
            # 获取 QQ 音乐 Cookie 用于降级下载
            qq_cookie = get_qq_cookie()
            
            downloader = MusicAutoDownloader(
                ncm_cookie, qq_cookie, str(download_path),
                proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
            )
            
            progress_msg = await query.message.reply_text(
                make_progress_message("📥 下载新歌曲", 0, len(new_songs), "准备开始...")
            )
            main_loop = asyncio.get_running_loop()
            last_update_time = [0]
            
            async def update_progress(current, total, song):
                import time as time_module
                now = time_module.time()
                if now - last_update_time[0] < 1.5:
                    return
                last_update_time[0] = now
                try:
                    song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                    await progress_msg.edit_text(
                        make_progress_message("📥 下载新歌曲", current, total, song_name),
                        parse_mode='Markdown'
                    )
                except:
                    pass
            
            def sync_progress_callback(current, total, song, status=None):
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
                )
            
            success_results, failed = await asyncio.to_thread(
                downloader.download_missing_songs,
                new_songs,
                download_quality,
                sync_progress_callback
            )
            
            # 提取文件列表（兼容字符串列表和字典列表）
            success_files = []
            for r in success_results:
                if isinstance(r, str):
                    success_files.append(r)
                elif isinstance(r, dict) and 'file' in r:
                    success_files.append(r['file'])
            
            try:
                await progress_msg.delete()
            except:
                pass
            
            # 保存下载记录（按实际平台）
            save_download_record_v2(success_results, failed, download_quality, user_id)
            
            # 更新歌曲列表
            current_song_ids = [
                str(s.get('source_id') or s.get('id') or s.get('title', ''))
                for s in songs
            ]
            update_scheduled_playlist_songs(playlist['id'], current_song_ids, playlist_name)
            
            # 统计平台分布
            ncm_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'NCM')
            qq_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'QQ')
            platform_info = f"\n• 网易云: {ncm_count}, QQ音乐: {qq_count}" if qq_count > 0 else ""
            
            await query.message.reply_text(
                f"✅ 下载完成\n歌单: {playlist_name}\n成功: {len(success_files)} 首{platform_info}\n失败: {len(failed)} 首"
            )

            # 自动触发 Emby 扫描和歌单同步
            if len(success_files) > 0 and emby_auth:
                status_msg = await query.message.reply_text("⏳ 正在触发 Emby 媒体库扫描...")
                
                # 1. 触发扫描
                trigger_emby_library_scan()
                
                # 2. 等待索引建立 (15秒)
                await asyncio.sleep(15)
                
                # 3. 更新本地缓存
                await status_msg.edit_text("⏳ 正在更新本地索引缓存...")
                await asyncio.to_thread(scan_emby_library, save_to_cache=True, user_id=user_id)
                
                # 4. 同步歌单
                await status_msg.edit_text(f"⏳ 正在将歌单 '{playlist_name}' 同步到 Emby...")
                try:
                    result, error = await asyncio.to_thread(
                        process_playlist, playlist['playlist_url'], user_id, force_public=False
                    )
                    
                    if error:
                        await status_msg.edit_text(f"❌ 歌单同步失败: {error}")
                    else:
                        msg = f"✅ **Emby 歌单同步完成**\n\n"
                        msg += f"📋 歌单: `{result['name']}`\n"
                        msg += f"📊 总计: {result['total']} 首\n"
                        msg += f"✅ 已匹配: {result['matched']} 首\n"
                        msg += f"❌ 未匹配: {result['unmatched']} 首"
                        await status_msg.edit_text(msg, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"自动同步歌单失败: {e}")
                    await status_msg.edit_text(f"❌ 自动同步出错: {e}")
            
        except Exception as e:
            logger.exception(f"下载新歌曲失败: {e}")
            await query.message.reply_text(f"❌ 下载失败: {e}")
    
    elif data.startswith("sync_emby_"):
        # 同步到 Emby
        playlist_id = int(data.replace("sync_emby_", ""))
        playlists = get_scheduled_playlists(user_id)
        playlist = next((p for p in playlists if p['id'] == playlist_id), None)
        
        if not playlist:
            await query.edit_message_text("❌ 歌单不存在")
            return
        
        # 重新同步整个歌单到 Emby
        await query.edit_message_text("🔄 正在同步到 Emby...")
        
        # 触发歌单同步
        context.user_data['sync_playlist_url'] = playlist['playlist_url']
        context.user_data['sync_from_scheduled'] = True
        
        # 模拟发送歌单链接
        await query.message.reply_text(f"请稍候，正在处理歌单...")
        
        try:
            # Call process_playlist
            result, error = await asyncio.to_thread(
                process_playlist, playlist['playlist_url'], user_id
            )
            
            if error:
                await query.message.reply_text(f"❌ 同步失败: {error}")
                return

            msg = f"✅ **歌单已同步到 Emby**\n\n"
            msg += f"📋 歌单: `{result['name']}`\n"
            msg += f"📊 总计: {result['total']} 首\n"
            msg += f"✅ 已匹配: {result['matched']} 首\n"
            msg += f"❌ 未匹配: {result['unmatched']} 首\n"
            
            await query.message.reply_text(msg, parse_mode='Markdown')
            
            # 如果有未匹配的歌曲，提供下载选项
            unmatched_songs = result.get('all_unmatched', [])
            if unmatched_songs:
                 # 显示未匹配歌曲列表
                unmatched_msg = f"📥 **以下 {len(unmatched_songs)} 首需要下载**:\n\n"
                for i, s in enumerate(unmatched_songs[:10]):
                    unmatched_msg += f"• {s.get('title', '')} - {s.get('artist', '')}\n"
                if len(unmatched_songs) > 10:
                    unmatched_msg += f"... 还有 {len(unmatched_songs) - 10} 首\n"
                
                # 提供下载按钮
                keyboard = [[
                    InlineKeyboardButton("📥 下载缺失歌曲", callback_data=f"sync_dl_pending_{playlist_id}"),
                    InlineKeyboardButton("⏭ 跳过", callback_data="menu_close")
                ]]
                await query.message.reply_text(
                    unmatched_msg, 
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                # 保存未匹配歌曲到 context 供后续下载使用
                context.user_data['pending_download_songs'] = unmatched_songs
            else:
                await query.message.reply_text("🎉 所有歌曲都已在库中！")

            # 触发 Emby 扫库
            asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))

        except Exception as e:
            logger.exception(f"同步处理异常: {e}")
            await query.message.reply_text(f"❌ 处理异常: {e}")


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """申请同步歌单 - 用户提交歌单链接，管理员审核后下载缺失歌曲"""
    user_id = str(update.effective_user.id)
    
    # 检查申请权限
    if not check_user_permission(user_id, 'request'):
        await update.message.reply_text("❌ 你没有申请权限，请联系管理员")
        return
    
    args = ' '.join(context.args) if context.args else ''
    
    if not args:
        await update.message.reply_text(
            "📝 **申请同步歌单**\n\n"
            "发送歌单链接申请同步到音乐库，管理员审核通过后会自动下载缺失的歌曲。\n\n"
            "**用法：**\n"
            "`/request <歌单链接>`\n\n"
            "**支持平台：**\n"
            "• 网易云音乐\n"
            "• QQ音乐\n"
            "• Spotify\n\n"
            "**示例：**\n"
            "`/request https://music.163.com/playlist?id=123456`\n"
            "`/request https://y.qq.com/n/ryqq/playlist/123456`\n"
            "`/request https://open.spotify.com/playlist/xxxxx`",
            parse_mode='Markdown'
        )
        return
    
    # 解析歌单链接
    import re
    playlist_url = args.strip()
    
    # 检测平台
    platform = None
    playlist_id = None
    playlist_name = "未知歌单"
    song_count = 0
    
    if 'music.163.com' in playlist_url or 'y.music.163.com' in playlist_url:
        platform = 'netease'
        playlist_id = extract_playlist_id(playlist_url, 'netease')
    elif 'y.qq.com' in playlist_url or 'qq.com' in playlist_url:
        platform = 'qq'
        playlist_id = extract_playlist_id(playlist_url, 'qq')
    elif 'spotify.com' in playlist_url or 'spotify:' in playlist_url:
        platform = 'spotify'
        playlist_id = extract_playlist_id(playlist_url, 'spotify')
    
    if not platform or not playlist_id:
        await update.message.reply_text(
            "❌ 无法识别歌单链接\n\n"
            "支持的平台：网易云音乐、QQ音乐、Spotify"
        )
        return
    
    # 获取歌单信息
    try:
        if platform == 'netease':
            playlist_name, songs = get_ncm_playlist_details(playlist_id)
        elif platform == 'spotify':
            playlist_name, songs = get_spotify_playlist_details(playlist_id)
        else:
            playlist_name, songs = get_qq_playlist_details(playlist_id)
        song_count = len(songs) if songs else 0
    except Exception as e:
        logger.warning(f"获取歌单信息失败: {e}")
        playlist_name = f"歌单 {playlist_id}"
    
    # 检查是否已有相同申请
    try:
        cursor = database_conn.cursor()
        cursor.execute('''
            SELECT id, status FROM playlist_requests 
            WHERE telegram_id = ? AND playlist_url = ? AND status = 'pending'
        ''', (user_id, playlist_url))
        existing = cursor.fetchone()
        if existing:
            await update.message.reply_text("⏳ 你已经申请过这个歌单，请等待管理员审核")
            return
    except:
        pass
    
    # 创建申请表（如果不存在）
    try:
        cursor = database_conn.cursor()
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
        database_conn.commit()
    except:
        pass
    
    # 提交申请
    try:
        cursor = database_conn.cursor()
        cursor.execute('''
            INSERT INTO playlist_requests (telegram_id, playlist_url, playlist_name, platform, song_count)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, playlist_url, playlist_name, platform, song_count))
        database_conn.commit()
        request_id = cursor.lastrowid
        
        platform_name = "网易云音乐" if platform == 'netease' else "QQ音乐"
        
        await update.message.reply_text(
            f"✅ **申请已提交**\n\n"
            f"📋 歌单: {playlist_name}\n"
            f"🎵 平台: {platform_name}\n"
            f"🔢 歌曲数: {song_count}\n\n"
            f"管理员审核通过后会自动下载缺失的歌曲",
            parse_mode='Markdown'
        )
        
        # 通知管理员
        if ADMIN_USER_ID:
            user = update.effective_user
            user_info = f"@{user.username}" if user.username else f"{user.first_name} ({user_id})"
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ 批准并下载", callback_data=f"req_approve_{request_id}"),
                    InlineKeyboardButton("❌ 拒绝", callback_data=f"req_reject_{request_id}")
                ],
                [
                    InlineKeyboardButton("👁️ 预览歌单", callback_data=f"req_preview_{request_id}")
                ]
            ])
            
            admin_msg = (
                f"📝 **新歌单同步申请**\n\n"
                f"👤 用户: {user_info}\n"
                f"📋 歌单: {playlist_name}\n"
                f"🎵 平台: {platform_name}\n"
                f"🔢 歌曲数: {song_count}\n"
                f"🔗 链接: {playlist_url}"
            )
            
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=admin_msg,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"通知管理员失败: {e}")
                
    except Exception as e:
        logger.error(f"提交歌单申请失败: {e}")
        await update.message.reply_text(f"❌ 提交失败: {e}")


async def cmd_myrequests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看我的歌单申请"""
    user_id = str(update.effective_user.id)
    
    try:
        if database_conn:
            cursor = database_conn.cursor()
            
            # 先查歌单申请
            cursor.execute('''
                SELECT * FROM playlist_requests 
                WHERE telegram_id = ? 
                ORDER BY created_at DESC 
                LIMIT 10
            ''', (user_id,))
            rows = cursor.fetchall()
            
            if not rows:
                await update.message.reply_text("📝 你还没有提交过申请")
                return
            
            msg = "📝 **我的歌单申请**\n\n"
            for row in rows:
                status_emoji = {'pending': '⏳', 'approved': '✅', 'rejected': '❌'}.get(row['status'], '❓')
                platform_name = "网易云" if row['platform'] == 'netease' else "QQ音乐"
                msg += f"{status_emoji} {row['playlist_name']}\n"
                msg += f"   🎵 {platform_name} · {row['song_count']} 首\n"
                msg += f"   状态: {row['status']}"
                if row['download_count']:
                    msg += f" (已下载 {row['download_count']} 首)"
                if row['admin_note']:
                    msg += f"\n   备注: {row['admin_note']}"
                msg += "\n\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ 查询失败: {e}")


async def handle_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理歌单申请审核回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.answer("仅管理员可操作", show_alert=True)
        return
    
    data = query.data
    
    if data.startswith("req_approve_"):
        request_id = int(data.replace("req_approve_", ""))
        await process_playlist_request(query, context, request_id, 'approved')
        
    elif data.startswith("req_reject_"):
        request_id = int(data.replace("req_reject_", ""))
        await process_playlist_request(query, context, request_id, 'rejected')
    
    elif data.startswith("req_preview_"):
        request_id = int(data.replace("req_preview_", ""))
        await preview_playlist_request(query, context, request_id)


async def preview_playlist_request(query, context, request_id: int):
    """预览歌单内容"""
    try:
        cursor = database_conn.cursor()
        cursor.execute('SELECT * FROM playlist_requests WHERE id = ?', (request_id,))
        row = cursor.fetchone()
        
        if not row:
            await query.message.reply_text("❌ 申请不存在")
            return
        
        playlist_url = row['playlist_url']
        platform = row['platform']
        
        # 获取歌单详情
        if platform == 'netease':
            playlist_id = extract_playlist_id(playlist_url, 'netease')
            playlist_name, songs = get_ncm_playlist_details(playlist_id)
        else:
            playlist_id = extract_playlist_id(playlist_url, 'qq')
            playlist_name, songs = get_qq_playlist_details(playlist_id)
        
        if not songs:
            await query.message.reply_text("❌ 获取歌单内容失败")
            return
        
        # 显示前10首
        msg = f"📋 **{playlist_name}** ({len(songs)} 首)\n\n"
        for i, song in enumerate(songs[:10]):
            msg += f"{i+1}. {song.get('title', '未知')} - {song.get('artist', '未知')}\n"
        
        if len(songs) > 10:
            msg += f"\n... 还有 {len(songs) - 10} 首"
        
        await query.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        await query.message.reply_text(f"❌ 预览失败: {e}")


async def process_playlist_request(query, context, request_id: int, action: str):
    """处理歌单申请（批准/拒绝）"""
    try:
        cursor = database_conn.cursor()
        cursor.execute('SELECT * FROM playlist_requests WHERE id = ?', (request_id,))
        row = cursor.fetchone()
        
        if not row:
            await query.message.reply_text("❌ 申请不存在")
            return
        
        requester_id = row['telegram_id']
        playlist_url = row['playlist_url']
        playlist_name = row['playlist_name']
        platform = row['platform']
        
        if action == 'rejected':
            # 拒绝申请
            cursor.execute('''
                UPDATE playlist_requests 
                SET status = 'rejected', processed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            ''', (request_id,))
            database_conn.commit()
            
            await query.edit_message_text(
                query.message.text + "\n\n❌ **已拒绝**",
                parse_mode='Markdown'
            )
            
            # 通知用户
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=f"❌ 你的歌单申请被拒绝\n\n📋 歌单: {playlist_name}"
                )
            except:
                pass
            return
        
        # 批准并下载
        await query.edit_message_text(
            query.message.text + "\n\n⏳ **正在匹配并下载缺失歌曲...**",
            parse_mode='Markdown'
        )
        
        # 获取歌单内容
        if platform == 'netease':
            playlist_id = extract_playlist_id(playlist_url, 'netease')
            _, songs = get_ncm_playlist_details(playlist_id)
        else:
            playlist_id = extract_playlist_id(playlist_url, 'qq')
            _, songs = get_qq_playlist_details(playlist_id)
        
        if not songs:
            await query.message.reply_text("❌ 获取歌单内容失败")
            return
        
        # 匹配 Emby 媒体库，找出缺失歌曲
        admin_binding = get_user_binding(ADMIN_USER_ID)
        if not admin_binding:
            await query.message.reply_text("❌ 管理员未绑定 Emby")
            return
        
        # 获取媒体库
        library_songs = load_library_cache()
        if not library_songs:
            await query.message.reply_text("❌ 媒体库缓存为空，请先 /rescan")
            return
        
        # 匹配
        missing_songs = []
        for song in songs:
            matched = False
            song_title = song.get('title', '')
            song_artist = song.get('artist', '')
            
            for lib_song in library_songs:
                lib_title = lib_song.get('Name', '')
                lib_artist = lib_song.get('Artists', [''])[0] if lib_song.get('Artists') else ''
                
                # 模糊匹配
                title_ratio = fuzz.ratio(song_title.lower(), lib_title.lower())
                if title_ratio > 85:
                    artist_ratio = fuzz.ratio(song_artist.lower(), lib_artist.lower())
                    if artist_ratio > 70 or not song_artist:
                        matched = True
                        break
            
            if not matched:
                missing_songs.append(song)
        
        if not missing_songs:
            # 更新状态
            cursor.execute('''
                UPDATE playlist_requests 
                SET status = 'approved', download_count = 0, processed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            ''', (request_id,))
            database_conn.commit()
            
            await query.edit_message_text(
                query.message.text.replace("⏳ **正在匹配并下载缺失歌曲...**", "") +
                "\n\n✅ **已批准** - 所有歌曲已在媒体库中",
                parse_mode='Markdown'
            )
            
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=f"✅ 你的歌单申请已通过！\n\n📋 歌单: {playlist_name}\n🎵 所有歌曲已在音乐库中"
                )
            except:
                pass
            return
        
        # 下载缺失歌曲
        ncm_cookie = get_ncm_cookie()
        if not ncm_cookie:
            await query.message.reply_text("❌ 未配置网易云 Cookie")
            return
        
        from bot.ncm_downloader import MusicAutoDownloader
        ncm_settings = get_ncm_settings()
        download_quality = ncm_settings.get('ncm_quality', 'exhigh')
        download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
        
        # 获取 QQ 音乐 Cookie 用于降级下载
        qq_cookie = get_qq_cookie()
        
        downloader = MusicAutoDownloader(
            ncm_cookie, qq_cookie, download_dir,
            proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
        )
        
        progress_msg = await query.message.reply_text(
            f"📥 正在下载 {len(missing_songs)} 首缺失歌曲..."
        )
        
        main_loop = asyncio.get_running_loop()
        last_update_time = [0]
        
        async def update_progress(current, total, song):
            import time as time_module
            now = time_module.time()
            if now - last_update_time[0] < 2:
                return
            last_update_time[0] = now
            try:
                await progress_msg.edit_text(
                    f"📥 下载中 ({current}/{total})\n🎵 {song.get('title', '')} - {song.get('artist', '')}"
                )
            except:
                pass
        
        def sync_progress_callback(current, total, song, status=None):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        success_results, failed_songs = await asyncio.to_thread(
            downloader.download_missing_songs,
            missing_songs,
            download_quality,
            sync_progress_callback
        )
        
        # 提取文件列表
        success_files = [r['file'] for r in success_results]
        
        try:
            await progress_msg.delete()
        except:
            pass
        
        # 保存下载记录（按实际平台）
        save_download_record_v2(success_results, failed_songs, download_quality, ADMIN_USER_ID)
        
        # 统计平台分布
        ncm_count = sum(1 for r in success_results if r.get('platform') == 'NCM')
        qq_count = sum(1 for r in success_results if r.get('platform') == 'QQ')
        platform_info = f"\n   • 网易云: {ncm_count}, QQ音乐: {qq_count}" if qq_count > 0 else ""
        
        # 更新申请状态
        cursor.execute('''
            UPDATE playlist_requests 
            SET status = 'approved', download_count = ?, processed_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        ''', (len(success_files), request_id))
        database_conn.commit()
        
        await query.edit_message_text(
            query.message.text.replace("⏳ **正在匹配并下载缺失歌曲...**", "") +
            f"\n\n✅ **已批准并下载**\n"
            f"📊 缺失: {len(missing_songs)} 首\n"
            f"✅ 成功: {len(success_files)} 首{platform_info}\n"
            f"❌ 失败: {len(failed_songs)} 首",
            parse_mode='Markdown'
        )
        
        # 通知用户
        try:
            await context.bot.send_message(
                chat_id=requester_id,
                text=f"✅ 你的歌单申请已通过！\n\n"
                     f"📋 歌单: {playlist_name}\n"
                     f"📥 已下载 {len(success_files)} 首新歌曲到音乐库"
            )
        except:
            pass
        
        # 触发 Emby 扫库
        if success_files:
            try:
                user_access_token, user_id_emby = authenticate_emby(
                    EMBY_URL, admin_binding['emby_username'], decrypt_password(admin_binding['emby_password'])
                )
                if user_access_token:
                    user_auth = {'access_token': user_access_token, 'user_id': user_id_emby}
                    trigger_emby_library_scan(user_auth)
            except:
                pass
                
    except Exception as e:
        logger.exception(f"处理歌单申请失败: {e}")
        await query.message.reply_text(f"❌ 处理失败: {e}")


async def handle_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理网易云试听回调"""
    query = update.callback_query
    await query.answer("🎧 正在获取试听...")
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        return
    
    data = query.data
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await query.message.reply_text("❌ 未配置网易云 Cookie")
        return
    
    try:
        idx = int(data.replace("preview_song_", ""))
        search_results = context.user_data.get('search_results', [])
        
        if not search_results or idx >= len(search_results):
            await query.message.reply_text("搜索结果已过期，请重新搜索")
            return
        
        song = search_results[idx]
        song_id = song['source_id']
        
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        
        # 获取歌曲URL（使用标准音质以加快速度）
        song_urls = api.get_song_url([song_id], 'standard')
        
        if not song_urls or song_id not in song_urls:
            await query.message.reply_text("❌ 无法获取试听链接，可能是版权限制")
            return
        
        url_info = song_urls[song_id]
        audio_url = url_info.get('url')
        
        if not audio_url:
            await query.message.reply_text("❌ 无法获取试听链接")
            return
        
        # 发送音频
        caption = f"🎵 {song['title']}\n🎤 {song['artist']}\n📀 {song.get('album', '未知专辑')}"
        await query.message.reply_audio(
            audio=audio_url,
            caption=caption,
            title=song['title'],
            performer=song['artist']
        )
        
    except Exception as e:
        logger.exception(f"试听失败: {e}")
        await query.message.reply_text(f"❌ 试听失败: {e}")


async def handle_qq_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理QQ音乐试听回调"""
    query = update.callback_query
    await query.answer("🎧 正在获取试听...")
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        return
    
    data = query.data
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await query.message.reply_text("❌ 未配置 QQ音乐 Cookie")
        return
    
    try:
        idx = int(data.replace("qpreview_song_", ""))
        search_results = context.user_data.get('qq_search_results', [])
        
        if not search_results or idx >= len(search_results):
            await query.message.reply_text("搜索结果已过期，请重新搜索")
            return
        
        song = search_results[idx]
        song_mid = song['source_id']
        
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(qq_cookie)
        
        # 获取歌曲URL（使用标准音质）
        song_urls = api.get_song_url([song_mid], 'standard')
        
        if not song_urls or song_mid not in song_urls:
            await query.message.reply_text("❌ 无法获取试听链接，可能是版权限制")
            return
        
        url_info = song_urls[song_mid]
        audio_url = url_info.get('url')
        
        if not audio_url:
            await query.message.reply_text("❌ 无法获取试听链接")
            return
        
        # 发送音频
        caption = f"🎵 {song['title']}\n🎤 {song['artist']}\n📀 {song.get('album', '未知专辑')}"
        await query.message.reply_audio(
            audio=audio_url,
            caption=caption,
            title=song['title'],
            performer=song['artist']
        )
        
    except Exception as e:
        logger.exception(f"QQ音乐试听失败: {e}")
        await query.message.reply_text(f"❌ 试听失败: {e}")


async def handle_search_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理搜索结果下载回调"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # 忽略过期的回调查询
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("仅管理员可使用此功能")
        return
    
    data = query.data
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await query.edit_message_text("❌ 未配置网易云 Cookie")
        return
    
    try:
        from bot.ncm_downloader import MusicAutoDownloader, NeteaseMusicAPI
        
        # 获取下载设置
        ncm_settings = get_ncm_settings()
        download_quality = ncm_settings.get('ncm_quality', 'exhigh')
        download_mode = ncm_settings.get('download_mode', 'local')
        download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
        musictag_dir = ncm_settings.get('musictag_dir', '')
        organize_dir = ncm_settings.get('organize_dir', '')
        
        download_path = Path(download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        
        # 获取 QQ 音乐 Cookie 用于降级下载
        qq_cookie = get_qq_cookie()
        
        downloader = MusicAutoDownloader(
            ncm_cookie, qq_cookie, str(download_path),
            proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
        )
        
        songs_to_download = []
        
        if data.startswith("dl_song_"):
            # 下载单曲或全部
            search_results = context.user_data.get('search_results', [])
            if not search_results:
                await query.edit_message_text("搜索结果已过期，请重新搜索")
                return
            
            if data == "dl_song_all":
                songs_to_download = search_results
            else:
                idx = int(data.replace("dl_song_", ""))
                if idx < len(search_results):
                    songs_to_download = [search_results[idx]]
        
        elif data.startswith("dl_album_"):
            # 下载专辑
            album_results = context.user_data.get('album_results', [])
            if not album_results:
                await query.edit_message_text("搜索结果已过期，请重新搜索")
                return
            
            idx = int(data.replace("dl_album_", ""))
            if idx < len(album_results):
                album = album_results[idx]
                await query.edit_message_text(f"📥 正在获取专辑 `{album['name']}` 的歌曲列表...", parse_mode='Markdown')
                
                api = NeteaseMusicAPI(ncm_cookie)
                songs_to_download = api.get_album_songs(album['album_id'])
                
                if not songs_to_download:
                    await query.message.reply_text("❌ 获取专辑歌曲失败")
                    return
        
        if not songs_to_download:
            await query.edit_message_text("没有可下载的歌曲")
            return
        
        # 音质显示
        quality_names = {
            'standard': '标准',
            'higher': '较高',
            'exhigh': '极高',
            'lossless': '无损',
            'hires': 'Hi-Res'
        }
        quality_name = quality_names.get(download_quality, download_quality)
        
        await query.edit_message_text(f"🔄 开始下载 {len(songs_to_download)} 首歌曲...\n📊 音质: {quality_name}")
        
        # 进度消息
        progress_msg = await query.message.reply_text(
            make_progress_message("📥 下载中", 0, len(songs_to_download), "准备开始...")
        )
        last_update_time = [0]
        main_loop = asyncio.get_running_loop()
        
        async def update_progress(current, total, song):
            import time as time_module
            now = time_module.time()
            if now - last_update_time[0] < 1.5:
                return
            last_update_time[0] = now
            try:
                song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                await progress_msg.edit_text(
                    make_progress_message("📥 下载中", current, total, song_name),
                    parse_mode='Markdown'
                )
            except:
                pass
        
        def sync_progress_callback(current, total, song, status=None):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        # 开始下载
        # organize 模式：按艺术家/专辑整理
        is_organize_mode = download_mode == 'organize' and organize_dir
        # 搜索下载：不回退到 QQ 音乐，只用网易云下载
        success_results, failed_songs = await asyncio.to_thread(
            downloader.download_missing_songs,
            songs_to_download,
            download_quality,
            sync_progress_callback,
            is_organize_mode,
            organize_dir if is_organize_mode else None,
            True  # fallback_to_qq=True，开启智能跨平台下载
        )
        
        
        # 提取文件列表（兼容字符串列表和字典列表）
        success_files = []
        for r in success_results:
            if isinstance(r, str):
                success_files.append(r)
            elif isinstance(r, dict) and 'file' in r:
                success_files.append(r['file'])
        
        # MusicTag 模式移动文件
        moved_files = []
        if download_mode == 'musictag' and musictag_dir and success_files:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            for i, file_path in enumerate(success_files):
                try:
                    src = Path(file_path)
                    if not src.exists():
                        logger.warning(f"源文件不存在，跳过移动: {file_path}")
                        continue
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                    # 更新 success_results 中的文件路径
                    success_results[i]['file'] = str(dst)
                except Exception as e:
                    logger.error(f"移动文件失败 {file_path}: {e}")
        
        # 删除进度消息
        try:
            await progress_msg.delete()
        except:
            pass
        
        # 保存下载记录（按实际平台）
        save_download_record_v2(success_results, failed_songs, download_quality, user_id)
        
        # 统计平台分布
        ncm_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'NCM')
        qq_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'QQ')
        platform_info = f"\n   • 网易云: {ncm_count}, QQ音乐: {qq_count}" if qq_count > 0 else ""
        
        msg = f"📥 **下载完成** (音质: {quality_name})\n\n"
        msg += f"✅ 成功: {len(success_files)} 首{platform_info}\n"
        msg += f"❌ 失败: {len(failed_songs)} 首\n"
        
        # 显示文件大小
        if success_files:
            total_size = sum(Path(f).stat().st_size for f in success_files if Path(f).exists())
            if total_size > 1024 * 1024:
                size_str = f"{total_size / 1024 / 1024:.1f} MB"
            else:
                size_str = f"{total_size / 1024:.1f} KB"
            msg += f"📦 总大小: {size_str}\n"
            
            if moved_files:
                msg += f"\n📁 已转移到 MusicTag 目录"
            elif is_organize_mode:
                msg += f"\n📁 已整理到: `{organize_dir}`"
            else:
                msg += f"\n📁 已保存到: `{download_dir}`"
        
        # 如果有失败的歌曲，添加重试按钮
        retry_keyboard = None
        if failed_songs:
            # 保存失败歌曲以便重试
            context.user_data['failed_songs_ncm'] = failed_songs
            context.user_data['failed_quality_ncm'] = download_quality
            msg += f"\n\n💡 点击下方按钮重试失败的歌曲"
            retry_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔄 重试 {len(failed_songs)} 首失败歌曲", callback_data="retry_ncm_failed")]
            ])
        
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=retry_keyboard)
        
        # 如果只下载了一首歌，发送音频预览
        if len(songs_to_download) == 1 and success_files:
            audio_path = Path(success_files[0])
            if audio_path.exists() and audio_path.stat().st_size < 50 * 1024 * 1024:  # 小于 50MB
                try:
                    song = songs_to_download[0]
                    with open(str(audio_path), 'rb') as audio_file:
                        await query.message.reply_audio(
                            audio=audio_file,
                            title=song.get('title', audio_path.stem),
                            performer=song.get('artist', 'Unknown'),
                            caption=f"🎵 {song.get('title', '')} - {song.get('artist', '')}"
                        )
                except Exception as e:
                    logger.warning(f"发送音频预览失败: {e}")
        
        # 自动扫库（organize 模式也触发）
        if success_files and (not moved_files or is_organize_mode):
            binding = get_user_binding(user_id)
            if binding:
                try:
                    user_access_token, user_id_emby = authenticate_emby(
                        EMBY_URL, binding['emby_username'], decrypt_password(binding['emby_password'])
                    )
                    if user_access_token:
                        user_auth = {'access_token': user_access_token, 'user_id': user_id_emby}
                        if trigger_emby_library_scan(user_auth):
                            await query.message.reply_text("🔄 已自动触发 Emby 扫库")
                except:
                    pass
        
    except Exception as e:
        logger.exception(f"下载失败: {e}")
        await query.message.reply_text(f"❌ 下载失败: {e}")


async def handle_qq_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 QQ 音乐搜索结果下载回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("仅管理员可使用此功能")
        return
    
    data = query.data
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await query.edit_message_text("❌ 未配置 QQ音乐 Cookie")
        return
    
    try:
        from bot.ncm_downloader import QQMusicAPI
        
        # 获取下载设置
        ncm_settings = get_ncm_settings()
        download_quality = ncm_settings.get('ncm_quality', 'exhigh')
        download_mode = ncm_settings.get('download_mode', 'local')
        download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
        musictag_dir = ncm_settings.get('musictag_dir', '')
        organize_dir = ncm_settings.get('organize_dir', '')
        
        download_path = Path(download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        
        api = QQMusicAPI(qq_cookie, proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY)
        
        songs_to_download = []
        
        if data.startswith("qdl_song_"):
            # 下载单曲或全部
            search_results = context.user_data.get('qq_search_results', [])
            if not search_results:
                await query.edit_message_text("搜索结果已过期，请重新搜索")
                return
            
            if data == "qdl_song_all":
                songs_to_download = search_results
            else:
                idx = int(data.replace("qdl_song_", ""))
                if idx < len(search_results):
                    songs_to_download = [search_results[idx]]
        
        elif data.startswith("qdl_album_"):
            # 下载专辑
            album_results = context.user_data.get('qq_album_results', [])
            if not album_results:
                await query.edit_message_text("搜索结果已过期，请重新搜索")
                return
            
            idx = int(data.replace("qdl_album_", ""))
            if idx < len(album_results):
                album = album_results[idx]
                await query.edit_message_text(f"📥 正在获取 QQ音乐专辑 `{album['name']}` 的歌曲列表...", parse_mode='Markdown')
                
                songs_to_download = api.get_album_songs(album['album_id'])
                
                if not songs_to_download:
                    await query.message.reply_text("❌ 获取专辑歌曲失败")
                    return
        
        if not songs_to_download:
            await query.edit_message_text("没有可下载的歌曲")
            return
        
        # 音质显示
        quality_names = {
            'standard': '标准',
            'higher': '较高',
            'exhigh': '极高',
            'lossless': '无损',
            'hires': 'Hi-Res'
        }
        quality_name = quality_names.get(download_quality, download_quality)
        
        await query.edit_message_text(f"🔄 开始从 QQ音乐 下载 {len(songs_to_download)} 首歌曲...\n📊 音质: {quality_name}")
        
        # 进度消息
        progress_msg = await query.message.reply_text(
            make_progress_message("📥 QQ音乐下载中", 0, len(songs_to_download), "准备开始...")
        )
        last_update_time = [0]
        main_loop = asyncio.get_running_loop()
        
        async def update_progress(current, total, song):
            import time as time_module
            now = time_module.time()
            if now - last_update_time[0] < 1.5:
                return
            last_update_time[0] = now
            try:
                song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
                await progress_msg.edit_text(
                    make_progress_message("📥 QQ音乐下载中", current, total, song_name),
                    parse_mode='Markdown'
                )
            except:
                pass
        
        def sync_progress_callback(current, total, song, status=None):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        # 开始下载
        # organize 模式：按艺术家/专辑整理
        is_organize_mode = download_mode == 'organize' and organize_dir
        success_files, failed_songs = await asyncio.to_thread(
            api.batch_download,
            songs_to_download,
            str(download_path),
            download_quality,
            sync_progress_callback,
            is_organize_mode,
            organize_dir if is_organize_mode else None
        )
        
        # MusicTag 模式移动文件
        moved_files = []
        if download_mode == 'musictag' and musictag_dir and success_files:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            new_success_files = []
            for file_path in success_files:
                try:
                    src = Path(file_path)
                    if not src.exists():
                        logger.warning(f"源文件不存在，跳过移动: {file_path}")
                        new_success_files.append(file_path)  # 保留原路径
                        continue
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                    new_success_files.append(str(dst))  # 使用新路径
                except Exception as e:
                    logger.error(f"移动文件失败 {file_path}: {e}")
                    new_success_files.append(file_path)  # 失败时保留原路径
            success_files = new_success_files  # 更新文件列表用于后续记录
        
        # 删除进度消息
        try:
            await progress_msg.delete()
        except:
            pass
        
        # 保存下载记录
        save_download_record(songs_to_download, success_files, failed_songs, 'QQ', download_quality, user_id)
        
        msg = f"📥 **QQ音乐下载完成** (音质: {quality_name})\n\n"
        msg += f"✅ 成功: {len(success_files)} 首\n"
        msg += f"❌ 失败: {len(failed_songs)} 首\n"
        
        # 显示文件大小
        if success_files:
            total_size = sum(Path(f).stat().st_size for f in success_files if Path(f).exists())
            if total_size > 1024 * 1024:
                size_str = f"{total_size / 1024 / 1024:.1f} MB"
            else:
                size_str = f"{total_size / 1024:.1f} KB"
            msg += f"📦 总大小: {size_str}\n"
            
            if moved_files:
                msg += f"\n📁 已转移到 MusicTag 目录"
            elif is_organize_mode:
                msg += f"\n📁 已整理到: `{organize_dir}`"
            else:
                msg += f"\n📁 已保存到: `{download_dir}`"
        
        # 如果有失败的歌曲，添加重试按钮
        retry_keyboard = None
        if failed_songs:
            # 保存失败歌曲以便重试
            context.user_data['failed_songs_qq'] = failed_songs
            context.user_data['failed_quality_qq'] = download_quality
            msg += f"\n\n💡 点击下方按钮重试失败的歌曲"
            retry_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔄 重试 {len(failed_songs)} 首失败歌曲", callback_data="retry_qq_failed")]
            ])
        
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=retry_keyboard)
        
        # 如果只下载了一首歌，发送音频预览
        if len(songs_to_download) == 1 and success_files:
            audio_path = Path(success_files[0])
            if audio_path.exists() and audio_path.stat().st_size < 50 * 1024 * 1024:  # 小于 50MB
                try:
                    song = songs_to_download[0]
                    with open(str(audio_path), 'rb') as audio_file:
                        await query.message.reply_audio(
                            audio=audio_file,
                            title=song.get('title', audio_path.stem),
                            performer=song.get('artist', 'Unknown'),
                            caption=f"🎵 {song.get('title', '')} - {song.get('artist', '')}"
                        )
                except Exception as e:
                    logger.warning(f"发送音频预览失败: {e}")
        
        # 自动扫库（organize 模式也触发）
        if success_files and (not moved_files or is_organize_mode):
            binding = get_user_binding(user_id)
            if binding:
                try:
                    user_access_token, user_id_emby = authenticate_emby(
                        EMBY_URL, binding['emby_username'], decrypt_password(binding['emby_password'])
                    )
                    if user_access_token:
                        user_auth = {'access_token': user_access_token, 'user_id': user_id_emby}
                        if trigger_emby_library_scan(user_auth):
                            await query.message.reply_text("🔄 已自动触发 Emby 扫库")
                except:
                    pass
        
    except Exception as e:
        logger.exception(f"QQ音乐下载失败: {e}")
        await query.message.reply_text(f"❌ 下载失败: {e}")


# ============================================================
# 菜单回调处理
# ============================================================

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "menu_playlist":
        await query.edit_message_text(
            "📋 **歌单同步**\n\n"
            "直接发送 QQ音乐 或 网易云音乐 的歌单链接即可。\n\n"
            "支持的链接格式：\n"
            "• `https://y.qq.com/n/ryqq/playlist/...`\n"
            "• `https://music.163.com/playlist?id=...`\n"
            "• 短链接也支持",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="menu_back")]])
        )
    
    elif data == "menu_upload":
        await query.edit_message_text(
            "📤 **音乐上传**\n\n"
            "直接发送音频文件即可自动上传到服务器。\n\n"
            "支持格式：MP3, FLAC, M4A, WAV, OGG, AAC\n\n"
            f"📁 保存路径: `{MUSIC_TARGET_DIR}`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="menu_back")]])
        )
    
    elif data == "menu_settings":
        user_id = str(query.from_user.id)
        binding = get_user_binding(user_id)
        
        text = "⚙️ **设置**\n\n"
        if binding:
            text += f"✅ 已绑定 Emby: `{binding['emby_username']}`\n\n"
            text += "使用 /unbind 解除绑定\n"
            text += "使用 /bind <用户名> <密码> 重新绑定"
        else:
            text += "❌ 尚未绑定 Emby 账户\n\n"
            text += "使用 /bind <用户名> <密码> 进行绑定"
        
        await query.edit_message_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="menu_back")]]))
    
    elif data == "menu_status":
        stats = get_stats()
        text = f"""
📊 **状态**

🎵 媒体库: {stats.get('library_songs', 0)} 首
👥 用户: {stats.get('users', 0)}
📋 歌单: {stats.get('playlists', 0)} 个
📤 上传: {stats.get('uploads', 0)} 个
"""
        await query.edit_message_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="menu_back")]]))
    
    elif data == "menu_back":
        await query.edit_message_text("请选择功能：", reply_markup=get_main_menu_keyboard())


async def handle_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理下载失败重试回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("无权执行此操作")
        return
    
    data = query.data
    
    if data == "retry_ncm_failed":
        # 重试网易云失败的歌曲
        failed_songs = context.user_data.get('failed_songs_ncm', [])
        quality = context.user_data.get('failed_quality_ncm', 'exhigh')
        
        if not failed_songs:
            await query.edit_message_text("❌ 没有需要重试的歌曲")
            return
        
        await query.edit_message_text(f"🔄 正在重试 {len(failed_songs)} 首歌曲...")
        
        # 重新设置搜索结果并触发下载
        context.user_data['search_results'] = failed_songs
        context.user_data['failed_songs_ncm'] = []  # 清空
        
        # 构造一个假的 callback data 来复用下载逻辑
        query.data = "dl_song_all"
        await handle_search_download_callback(update, context)
        
    elif data == "retry_qq_failed":
        # 重试 QQ 音乐失败的歌曲
        failed_songs = context.user_data.get('failed_songs_qq', [])
        quality = context.user_data.get('failed_quality_qq', 'exhigh')
        
        if not failed_songs:
            await query.edit_message_text("❌ 没有需要重试的歌曲")
            return
        
        await query.edit_message_text(f"🔄 正在重试 {len(failed_songs)} 首歌曲...")
        
        # 重新设置搜索结果并触发下载
        context.user_data['qq_search_results'] = failed_songs
        context.user_data['failed_songs_qq'] = []  # 清空
        
        # 直接执行下载逻辑
        # 读取下载配置
        from bot.config import QQ_COOKIE, NCM_DOWNLOAD_SETTINGS
        from bot.ncm_downloader import QQMusicAPI
        
        qq_cookie = context.bot_data.get('qq_cookie') or QQ_COOKIE
        ncm_settings = context.bot_data.get('ncm_settings', {})
        download_quality = ncm_settings.get('download_quality', 'exhigh')
        download_dir = ncm_settings.get('download_dir', '/downloads')
        
        api = QQMusicAPI(qq_cookie, proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY)
        
        await query.edit_message_text(f"🔄 正在重试下载 {len(failed_songs)} 首歌曲...")
        
        success_files, new_failed = api.batch_download(
            failed_songs, download_dir, download_quality, None
        )
        
        if success_files:
            await query.message.reply_text(f"✅ 重试完成\n成功: {len(success_files)} 首\n失败: {len(new_failed)} 首")
        else:
            await query.message.reply_text(f"❌ 重试失败，{len(new_failed)} 首歌曲仍无法下载")


# ============================================================
# 文件整理器
# ============================================================

# 全局变量存储 application 实例，用于发送通知
_telegram_app = None


def file_organizer_callback(source_path: str, target_path: str):
    """文件整理完成后的回调 - 日志已在 file_organizer 中记录"""
    pass  # 日志已在 file_organizer.py 中美化输出


async def start_file_organizer_if_enabled(application):
    """如果配置了并启用了文件整理器，则启动它"""
    global _telegram_app
    _telegram_app = application
    
    try:
        if not database_conn:
            return
        
        cursor = database_conn.cursor()
        
        # 检查是否启用 (兼容 auto_organize 和 organize_monitor_enabled)
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('auto_organize',))
        row = cursor.fetchone()
        auto_organize = row and (row[0] if isinstance(row, tuple) else row['value']) == 'true'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_monitor_enabled',))
        row = cursor.fetchone()
        monitor_enabled = row and (row[0] if isinstance(row, tuple) else row['value']) == 'true'
        
        enabled = auto_organize or monitor_enabled
        
        if not enabled:
            logger.info("📁 文件整理器未启用")
            return
        
        # 获取配置 - source_dir 优先用 organize_source_dir，否则用 download_dir
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_source_dir',))
        row = cursor.fetchone()
        source_dir = (row[0] if isinstance(row, tuple) else row['value']) if row else ''
        
        if not source_dir:
            # 回退到下载目录
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('download_dir',))
            row = cursor.fetchone()
            source_dir = (row[0] if isinstance(row, tuple) else row['value']) if row else '/app/uploads'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_target_dir',))
        row = cursor.fetchone()
        target_dir = (row[0] if isinstance(row, tuple) else row['value']) if row else ''
        
        # 如果没有设置 organize_target_dir，尝试用 organize_dir
        if not target_dir:
            cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_dir',))
            row = cursor.fetchone()
            target_dir = (row[0] if isinstance(row, tuple) else row['value']) if row else ''
        

        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_template',))
        row = cursor.fetchone()
        template = (row[0] if isinstance(row, tuple) else row['value']) if row else '{album_artist}/{album}'
        
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('organize_on_conflict',))
        row = cursor.fetchone()
        on_conflict = (row[0] if isinstance(row, tuple) else row['value']) if row else 'skip'
        
        if not source_dir or not target_dir:
            logger.info("📁 文件整理器未配置源目录或目标目录")
            return
        
        # 启动监控
        from bot.file_organizer import start_watcher
        watcher = start_watcher(
            source_dir, target_dir, template, on_conflict,
            callback=file_organizer_callback
        )
        
        # 发送 Telegram 通知
        if ADMIN_USER_ID:
            try:
                msg = (
                    "📁 *文件整理器已启动*\n\n"
                    f"📂 监控目录: `{source_dir}`\n"
                    f"🎵 整理目录: `{target_dir}`\n"
                    f"📋 整理模板: `{template}`\n"
                    f"⚙️ 冲突处理: `{on_conflict}`"
                )
                await application.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=msg,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.debug(f"发送整理器启动通知失败: {e}")
        
    except Exception as e:
        logger.error(f"启动文件整理器失败: {e}")


# ============================================================
# 定时任务
# ============================================================



async def refresh_qq_cookie_task(application):
    """定时刷新 QQ 音乐 Cookie 保活"""
    logger.info("启动 QQ 音乐 Cookie 保活任务...")
    
    while True:
        try:
            await asyncio.sleep(60)  # 等待应用完全启动
            
            # 从数据库读取当前 Cookie
            conn = sqlite3.connect(str(DATABASE_FILE))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'qq_cookie'")
            row = cursor.fetchone()
            current_cookie = row['value'] if row else None
            
            if current_cookie:
                logger.info("正在尝试刷新 QQ 音乐 Cookie...")
                from bot.ncm_downloader import QQMusicAPI
                api = QQMusicAPI(current_cookie)
                success, data = api.refresh_cookie()
                
                if success:
                    new_musickey = data.get('musickey')
                    if new_musickey:
                        logger.info(f"QQ Cookie 刷新成功，获取到新 musickey: {new_musickey[:10]}...")
                        
                        # 更新 Cookie 字符串
                        new_cookie = current_cookie
                        import re
                        if 'qqmusic_key=' in new_cookie:
                            new_cookie = re.sub(r'qqmusic_key=[^;]*', f'qqmusic_key={new_musickey}', new_cookie)
                        if 'qm_keyst=' in new_cookie:
                            new_cookie = re.sub(r'qm_keyst=[^;]*', f'qm_keyst={new_musickey}', new_cookie)
                            
                        # 保存回数据库
                        cursor.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                                      ('qq_cookie', new_cookie))
                        conn.commit()
                        logger.info("QQ Cookie 已更新到数据库")
                    else:
                        logger.info("QQ Cookie 刷新成功，但未检测到 musickey 变化")
                else:
                    logger.warning(f"QQ Cookie 刷新失败: {data.get('error')}")
            else:
                logger.debug("未配置 QQ Cookie，跳过刷新")
                
            conn.close()
            
        except Exception as e:
            logger.error(f"QQ Cookie 保活任务异常: {e}")
            
        # 每 6 小时运行一次 (21600 秒)
        await asyncio.sleep(21600)

async def radar_push_job(application):
    """定时生成并推送私人雷达歌单"""
    from datetime import datetime
    
    logger.info("启动私人雷达定时任务...")
    
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            
            now = datetime.now()
            current_time = now.strftime('%H:%M')
            
            # 读取配置
            conn = sqlite3.connect(str(DATABASE_FILE))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'radar_%'")
            settings = {row['key'].replace('radar_', ''): row['value'] for row in cursor.fetchall()}
            
            radar_enabled = settings.get('push_enabled', '0') == '1'
            radar_time = settings.get('push_time', '09:00')
            
            if not radar_enabled or current_time != radar_time:
                conn.close()
                continue
            
            logger.info("[Radar] 开始生成私人雷达...")
            
            # 获取所有已绑定 Emby 的用户
            cursor.execute("SELECT telegram_id, emby_user_id, emby_token FROM user_bindings WHERE emby_user_id IS NOT NULL")
            bindings = cursor.fetchall()
            conn.close()
            
            if not bindings:
                logger.info("[Radar] 没有已绑定的用户")
                continue
            
            # 导入雷达模块
            from bot.services.radar import generate_user_radar
            from bot.services.emby import (
                get_user_playback_history, 
                get_library_songs_with_genres,
                find_playlist_by_name,
                create_private_playlist,
                update_playlist_items
            )
            
            # 获取带流派的媒体库（共用）
            library_songs = await asyncio.to_thread(get_library_songs_with_genres)
            if not library_songs:
                logger.warning("[Radar] 无法获取媒体库")
                continue
            
            today_str = now.strftime('%Y-%m-%d')
            playlist_name = f"私人雷达 · {today_str}"
            
            success_count = 0
            for binding in bindings:
                try:
                    telegram_id = binding['telegram_id']
                    emby_user_id = binding['emby_user_id']
                    emby_token = binding['emby_token']
                    
                    if not emby_user_id or not emby_token:
                        continue
                    
                    user_auth = {'user_id': emby_user_id, 'access_token': emby_token}
                    
                    # 获取用户播放历史
                    playback_history = await asyncio.to_thread(
                        get_user_playback_history, emby_user_id, None, user_auth
                    )
                    
                    if not playback_history:
                        logger.info(f"[Radar] 用户 {telegram_id} 无播放历史，跳过")
                        continue
                    
                    # 生成推荐
                    recommended_songs = generate_user_radar(
                        emby_user_id, playback_history, library_songs, 30
                    )
                    
                    if not recommended_songs:
                        continue
                    
                    song_ids = [str(s.get('Id') or s.get('id')) for s in recommended_songs]
                    
                    # 查找或创建歌单
                    existing_playlist_id = await asyncio.to_thread(
                        find_playlist_by_name, playlist_name, user_auth
                    )
                    
                    if existing_playlist_id:
                        # 更新现有歌单
                        await asyncio.to_thread(
                            update_playlist_items, existing_playlist_id, song_ids, user_auth
                        )
                        playlist_id = existing_playlist_id
                    else:
                        # 创建新歌单
                        playlist_id = await asyncio.to_thread(
                            create_private_playlist, playlist_name, song_ids, user_auth
                        )
                    
                    if playlist_id:
                        # 发送通知
                        try:
                            emby_url = os.environ.get('EMBY_SERVER_URL', '') or os.environ.get('EMBY_URL', '')
                            playlist_url = f"{emby_url.rstrip('/')}/web/index.html#!/itemdetails.html?id={playlist_id}"
                            
                            msg = f"🎯 **今日私人雷达已更新！**\n\n"
                            msg += f"📅 {today_str}\n"
                            msg += f"🎵 30 首为你精选的歌曲\n\n"
                            msg += f"[📱 打开歌单]({playlist_url})"
                            
                            await application.bot.send_message(
                                chat_id=int(telegram_id),
                                text=msg,
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )
                            success_count += 1
                            logger.info(f"[Radar] 用户 {telegram_id} 推送成功")
                        except Exception as e:
                            logger.warning(f"[Radar] 用户 {telegram_id} 通知发送失败: {e}")
                    
                except Exception as e:
                    logger.error(f"[Radar] 处理用户失败: {e}")
            
            logger.info(f"[Radar] 今日推送完成，成功 {success_count}/{len(bindings)} 用户")
            
            # 等待到第二天再检查
            await asyncio.sleep(60 * 60)  # 1小时后再继续
            
        except Exception as e:
            logger.error(f"[Radar] 任务异常: {e}")
            await asyncio.sleep(300)


async def scheduled_ranking_job(application):
    """定时发送排行榜到指定群组/频道"""
    import os
    from datetime import datetime, time as dtime
    from io import BytesIO
    
    logger.info("启动定时排行榜任务...")
    
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            
            now = datetime.now()
            current_time = now.strftime('%H:%M')
            weekday = now.weekday()  # 0=周一, 6=周日
            day = now.day
            
            # 从数据库读取配置
            conn = sqlite3.connect(str(DATABASE_FILE))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 获取设置
            cursor.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'ranking_%'")
            settings = {row['key']: row['value'] for row in cursor.fetchall()}
            conn.close()
            
            target_chat = settings.get('ranking_target_chat', '')
            if not target_chat:
                continue
            
            daily_time = settings.get('ranking_daily_time', '08:00')
            weekly_time = settings.get('ranking_weekly_time', '10:00')
            weekly_day = int(settings.get('ranking_weekly_day', '6'))  # 6=周日
            monthly_time = settings.get('ranking_monthly_time', '09:00')
            
            # 检查是否需要发送
            from bot.services.playback_stats import get_playback_stats
            from bot.utils.ranking_image import generate_ranking_image, generate_daily_ranking_image
            
            stats = get_playback_stats()
            emby_url = os.environ.get('EMBY_SERVER_URL', '') or os.environ.get('EMBY_URL', '')
            emby_token = os.environ.get('EMBY_API_KEY', '')
            
            # 日榜 - 使用漂亮的每日榜样式
            if current_time == daily_time:
                try:
                    # Get Config from DB or Env
                    from bot.config import DAILY_RANKING_TITLE, DAILY_RANKING_SUBTITLE
                    ranking_title = settings.get('ranking_daily_title')
                    if not ranking_title: ranking_title = DAILY_RANKING_TITLE
                    
                    ranking_subtitle = settings.get('ranking_daily_subtitle')
                    if not ranking_subtitle: ranking_subtitle = DAILY_RANKING_SUBTITLE
                
                    data = stats.get_global_daily_stats()
                    if data and data.get('leaderboard'):
                        # Run in executor to avoid blocking scheduler
                        # import asyncio # Removed as it shadows global import
                        from functools import partial
                        loop = asyncio.get_running_loop()
                        
                        img = await loop.run_in_executor(
                            None,
                            partial(
                                generate_daily_ranking_image, 
                                data, 
                                emby_url=emby_url, 
                                emby_token=emby_token, 
                                title=ranking_title
                            )
                        )
                        
                        if img:
                            # Generate Text Caption for Scheduled Task
                            from bot.config import EMBY_URL
                            
                            caption_lines = [
                                f"【{ranking_subtitle} 播放日榜】\n",
                                "▎热门歌曲：\n"
                            ]
                            
                            top_songs = data.get('top_songs', [])[:10]
                            for i, song in enumerate(top_songs):
                                title = song.get('title', 'Unknown')
                                artist = song.get('artist', 'Unknown')
                                count = song.get('count', 0)
                                sid = song.get('id', '')
                                
                                import html
                                
                                if EMBY_URL:
                                    link = f"{EMBY_URL.rstrip('/')}/web/index.html#!/item?id={sid}"
                                else:
                                    link = ""
                                
                                # Escape special characters for HTML parsing
                                safe_title = html.escape(title)
                                safe_artist = html.escape(artist)
                                
                                if link:
                                    line = f'{i+1}. <a href="{link}">{safe_title}</a>'
                                else:
                                    line = f"{i+1}. {safe_title}"
                                    
                                caption_lines.append(line)
                                caption_lines.append(f"歌手: {safe_artist}")
                                caption_lines.append(f"播放次数: {count}")
                                caption_lines.append("") 
                            
                            caption_lines.append(f"\n#DayRanks  {data.get('date', now.strftime('%Y-%m-%d'))}")
                            caption = "\n".join(caption_lines)
                            
                            if len(caption) > 1024:
                                caption = caption[:1020] + "..."

                            await application.bot.send_photo(
                                chat_id=target_chat, 
                                photo=BytesIO(img), 
                                caption=caption,
                                parse_mode='HTML'
                            )
                        else:
                            msg = f"🏆 **每日播放榜** ({now.strftime('%Y-%m-%d')})\\n\\n"
                            for i, user in enumerate(data['leaderboard'], 1):
                                msg += f"{i}. {user['name']} ({user['minutes']}分钟)\\n"
                            await application.bot.send_message(chat_id=target_chat, text=msg, parse_mode='Markdown')
                        logger.info("已发送日榜")
                    else:
                        logger.info("日榜无数据，跳过发送")
                except Exception as e:
                    logger.error(f"发送日榜失败: {e}")
            
            # 周榜 (指定星期)
            if current_time == weekly_time and weekday == weekly_day:
                try:
                    # Get Config for titles
                    ranking_title = settings.get('ranking_weekly_title', '🏆 本周音乐热曲榜')
                    
                    data = stats.get_global_weekly_stats()
                    if data and data.get('leaderboard'):
                        # import asyncio # Removed as it shadows global import
                        from functools import partial
                        loop = asyncio.get_running_loop()
                        
                        img = await loop.run_in_executor(
                            None,
                            partial(
                                generate_daily_ranking_image, 
                                data, 
                                emby_url=emby_url, 
                                emby_token=emby_token, 
                                title=ranking_title
                            )
                        )
                        
                        if img:
                            from bot.config import EMBY_URL
                            caption_lines = [
                                f"【TGmusicbot 播放周榜】\n",
                                "▎本周热门歌曲：\n"
                            ]
                            
                            top_songs = data.get('top_songs', [])[:10]
                            for i, song in enumerate(top_songs):
                                title = song.get('title', 'Unknown')
                                artist = song.get('artist', 'Unknown')
                                count = song.get('count', 0)
                                sid = song.get('id', '')
                                
                                line = f"{i+1}. {title}"
                                caption_lines.append(line)
                                if artist and artist != 'Unknown':
                                    caption_lines.append(f"歌手: {artist}")
                                caption_lines.append(f"播放次数: {count}")
                                caption_lines.append("") 
                            
                            caption_lines.append(f"\n#WeekRanks  {data.get('date', '')}")
                            caption = "\n".join(caption_lines)
                            
                            if len(caption) > 1024:
                                caption = caption[:1020] + "..."

                            await application.bot.send_photo(chat_id=target_chat, photo=BytesIO(img), caption=caption)
                            logger.info("已发送周榜")
                        else:
                            logger.error("生成周榜图片失败")
                    else:
                        logger.info("周榜无数据")
                except Exception as e:
                    logger.error(f"发送周榜失败: {e}")
            
            # 月榜 (每月1号)
            if current_time == monthly_time and day == 1:
                ranking = stats.get_ranking('month', 10)
                if ranking:
                    last_month = (now.replace(day=1) - timedelta(days=1)).strftime('%Y年%m月')
                    img = generate_ranking_image(ranking, "🏆 每月播放榜", last_month, emby_base_url=emby_url)
                    if img:
                        await application.bot.send_photo(chat_id=target_chat, photo=BytesIO(img),
                                                        caption=f"🏆 每月播放榜 ({last_month})")
                    logger.info("已发送月榜")
                    
        except Exception as e:
            logger.error(f"定时排行榜任务异常: {e}")
            await asyncio.sleep(60)


async def scheduled_sync_job(application):
    """定时检查订阅歌单更新"""
    poll_interval = PLAYLIST_SYNC_POLL_INTERVAL_SECONDS
    initial_delay = PLAYLIST_SYNC_INITIAL_DELAY_SECONDS
    
    if initial_delay:
        logger.info(f"歌单同步任务将在 {initial_delay} 秒后开始首次检查")
        await asyncio.sleep(initial_delay)
    
    logger.info(f"歌单同步任务已启动 (轮询间隔 {poll_interval} 秒)")
    
    while True:
        try:
            logger.info("开始定时歌单同步检查...")
            await check_playlist_updates(application)
        except asyncio.CancelledError:
            logger.info("歌单同步任务已取消")
            break
        except Exception as e:
            logger.error(f"定时同步任务出错: {e}")
        
        await asyncio.sleep(poll_interval)


async def scheduled_emby_scan_job(application):
    """定时扫描 Emby 媒体库"""
    await asyncio.sleep(600)  # 启动后 10 分钟开始
    
    while True:
        try:
            # 获取扫描间隔设置
            scan_interval = EMBY_SCAN_INTERVAL
            if database_conn:
                cursor = database_conn.cursor()
                cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
                row = cursor.fetchone()
                if row:
                    scan_interval = int(row[0] if isinstance(row, tuple) else row['value'])
            
            if scan_interval <= 0:
                await asyncio.sleep(3600)  # 未启用时，每小时检查配置
                continue
            
            logger.info(f"开始定时 Emby 媒体库扫描 (间隔: {scan_interval} 小时)...")
            
            # 扫描并更新缓存
            if emby_auth.get('access_token') and emby_auth.get('user_id'):
                # 这是一个同步函数，直接调用
                scan_emby_library()
                logger.info("Emby 媒体库扫描完成")
            
        except Exception as e:
            logger.error(f"定时扫描任务出错: {e}")
        
        # 等待下一次扫描
        interval_hours = scan_interval if scan_interval > 0 else 1
        await asyncio.sleep(interval_hours * 3600)






async def process_batch_download(songs_to_download, download_quality, user_id, update):
    """处理批量下载逻辑（通用）"""
    message = update.effective_message
    
    # 获取下载设置
    ncm_settings = get_ncm_settings()
    download_mode = ncm_settings.get('download_mode', 'local')
    download_dir = ncm_settings.get('download_dir', str(MUSIC_TARGET_DIR))
    musictag_dir = ncm_settings.get('musictag_dir', '')
    organize_dir = ncm_settings.get('organize_dir', '')
    
    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    
    # 获取 Cookie
    ncm_cookie = get_ncm_cookie()
    qq_cookie = get_qq_cookie()
    
    from bot.ncm_downloader import MusicAutoDownloader
    downloader = MusicAutoDownloader(
        ncm_cookie, qq_cookie, str(download_path),
        proxy_url=MUSIC_PROXY_URL, proxy_key=MUSIC_PROXY_KEY
    )
    
    # 进度消息
    quality_names = {'standard': '标准', 'higher': '较高', 'exhigh': '极高', 'lossless': '无损', 'hires': 'Hi-Res'}
    quality_name = quality_names.get(download_quality, download_quality)
    
    progress_msg = await message.reply_text(
        make_progress_message("📥 下载中", 0, len(songs_to_download), "准备开始...")
    )
    
    last_update_time = [0]
    main_loop = asyncio.get_running_loop()
    
    async def update_progress(current, total, song):
        import time as time_module
        now = time_module.time()
        if now - last_update_time[0] < 1.5:
            return
        last_update_time[0] = now
        try:
            song_name = f"{song.get('title', '')} - {song.get('artist', '')}"
            await progress_msg.edit_text(
                make_progress_message("📥 下载中", current, total, song_name),
                parse_mode='Markdown'
            )
        except:
            pass
    
    def sync_progress_callback(current, total, song, status=None):
        main_loop.call_soon_threadsafe(
            lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
        )
    
    # 开始下载
    is_organize_mode = download_mode == 'organize' and organize_dir
    success_results, failed_songs = await asyncio.to_thread(
        downloader.download_missing_songs,
        songs_to_download,
        download_quality,
        sync_progress_callback,
        is_organize_mode,
        organize_dir if is_organize_mode else None,
        False 
    )
    
    # 如果是 musictag 模式，移动文件
    success_files = []
    for r in success_results:
        if isinstance(r, str):
            success_files.append(r)
        elif isinstance(r, dict) and 'file' in r:
            success_files.append(r['file'])
            
    if download_mode == 'musictag' and musictag_dir and success_files:
        musictag_path = Path(musictag_dir)
        musictag_path.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(success_results):
            fpath = r if isinstance(r, str) else r.get('file')
            if fpath and os.path.exists(fpath):
                try:
                    src = Path(fpath)
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    if isinstance(r, dict):
                        success_results[i]['file'] = str(dst)
                    else:
                        success_results[i] = str(dst)
                except Exception as e:
                    logger.error(f"移动文件失败: {e}")

    try:
        await progress_msg.delete()
    except:
        pass
    
    # 保存记录
    save_download_record_v2(success_results, failed_songs, download_quality, user_id)
    
    # 发送报告
    ncm_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'NCM')
    qq_count = sum(1 for r in success_results if isinstance(r, dict) and r.get('platform') == 'QQ')
    platform_info = f"\n   • 网易云: {ncm_count}, QQ音乐: {qq_count}" if qq_count > 0 else ""
    
    msg = f"📥 **下载完成** (音质: {quality_name})\n\n"
    msg += f"✅ 成功: {len(success_files)} 首{platform_info}\n"
    msg += f"❌ 失败: {len(failed_songs)} 首\n"
    
    if success_files:
        total_size = sum(Path(f).stat().st_size for f in success_files if isinstance(f, str) and Path(f).exists())
        if total_size > 1024 * 1024:
            size_str = f"{total_size / 1024 / 1024:.1f} MB"
        else:
            size_str = f"{total_size / 1024:.1f} KB"
        msg += f"📦 总大小: {size_str}\n"
        
        target_path = organize_dir if is_organize_mode else (musictag_dir if download_mode == 'musictag' else download_dir)
        msg += f"\n📂 已保存到: `{target_path}`"

    await message.reply_text(msg, parse_mode='Markdown')
    
    # 触发 Emby 扫库
    if success_files:
        await message.reply_text("🔄 已自动触发 Emby 扫库")
        asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))


async def handle_playlist_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理歌单操作回调（下载/订阅）"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    # 格式: pl_action_{action}_{platform}_{playlist_id}
    # action: download, subscribe
    try:
        parts = data.split('_')
        action = parts[2]
        platform = parts[3]
        playlist_id = parts[4]
    except IndexError:
        await query.edit_message_text("❌ 无效的回调数据")
        return

    ncm_cookie = get_ncm_cookie()
    if platform == 'netease' and not ncm_cookie:
        await query.edit_message_text("❌ 未配置网易云 Cookie，无法操作")
        return

    if action == 'download':
        # 使用现有的 process_playlist() 流程：先同步到 Emby，再下载缺失歌曲
        await query.edit_message_text(f"🔄 正在同步歌单到 Emby...")
        
        try:
            # 构造歌单链接
            if platform == 'netease':
                playlist_url = f"https://music.163.com/playlist?id={playlist_id}"
            elif platform == 'qq':
                playlist_url = f"https://y.qq.com/n/ryqq/playlist/{playlist_id}"
            else:
                await query.message.reply_text("❌ 不支持的平台")
                return
            
            user_id = str(query.from_user.id)
            
            # 调用现有的 process_playlist 函数同步到 Emby
            result, error = await asyncio.to_thread(
                process_playlist, playlist_url, user_id
            )
            
            if error:
                await query.message.reply_text(f"❌ 同步失败: {error}")
                return
            
            # 报告同步结果
            msg = f"✅ **歌单已同步到 Emby**\n\n"
            msg += f"📋 歌单: `{result['name']}`\n"
            msg += f"📊 总计: {result['total']} 首\n"
            msg += f"✅ 已匹配: {result['matched']} 首\n"
            msg += f"❌ 未匹配: {result['unmatched']} 首\n"
            
            await query.message.reply_text(msg, parse_mode='Markdown')
            
            # 如果有未匹配的歌曲，提供下载选项
            unmatched_songs = result.get('all_unmatched', [])
            if unmatched_songs:
                # 显示未匹配歌曲列表
                unmatched_msg = f"📥 **以下 {len(unmatched_songs)} 首需要下载**:\n\n"
                for i, s in enumerate(unmatched_songs[:10]):
                    unmatched_msg += f"• {s.get('title', '')} - {s.get('artist', '')}\n"
                if len(unmatched_songs) > 10:
                    unmatched_msg += f"... 还有 {len(unmatched_songs) - 10} 首\n"
                
                # 提供下载按钮
                keyboard = [[
                    InlineKeyboardButton("📥 下载缺失歌曲", callback_data=f"sync_dl_pending_{playlist_id}"),
                    InlineKeyboardButton("⏭ 跳过", callback_data="menu_close")
                ]]
                await query.message.reply_text(
                    unmatched_msg, 
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                # 保存未匹配歌曲到 context 供后续下载使用
                context.user_data['pending_download_songs'] = unmatched_songs
            else:
                await query.message.reply_text("🎉 所有歌曲都已在库中！")
            
            # 触发 Emby 扫库
            asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))
                
        except Exception as e:
            logger.exception(f"同步失败: {e}")
            await query.message.reply_text(f"❌ 同步失败: {e}")

    elif action == 'subscribe':
        # 订阅同步
        logger.info(f"[订阅] 用户 {query.from_user.id} 订阅歌单 platform={platform} id={playlist_id}")
        try:
            # 获取歌单详情以保存名字
            name = "未知歌单"
            if platform == 'netease':
                logger.info(f"[订阅] 获取网易云歌单详情...")
                name, songs = get_ncm_playlist_details(playlist_id)
                playlist_url = f"https://music.163.com/playlist?id={playlist_id}"
            elif platform == 'qq':
                logger.info(f"[订阅] 获取QQ音乐歌单详情...")
                name, songs = get_qq_playlist_details(playlist_id)
                playlist_url = f"https://y.qq.com/n/ryqq/playlist/{playlist_id}"
            else:
                await query.edit_message_text("❌ 暂不支持该平台")
                return
            
            logger.info(f"[订阅] 歌单名称: {name}, 歌曲数: {len(songs) if songs else 0}")
            
            user_id = str(query.from_user.id)
            
            if not database_conn:
                await query.edit_message_text("❌ 数据库连接失败")
                return
            
            # 保存订阅
            logger.info(f"[订阅] 保存订阅到数据库...")
            cursor = database_conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO scheduled_playlists 
                (telegram_id, playlist_url, playlist_name, platform, sync_interval, is_active)
                VALUES (?, ?, ?, ?, NULL, 1)
            ''', (user_id, playlist_url, name, platform))
            database_conn.commit()
            logger.info(f"[订阅] 订阅已保存")
            
            await query.edit_message_text(
                f"✅ 已订阅歌单: **{name}**\n"
                f"📊 共 {len(songs) if songs else 0} 首歌曲\n"
                f"🔄 同步间隔: **跟随系统设置**\n\n"
                f"⏳ 正在同步到 Emby...",
                parse_mode='Markdown'
            )
            
            # 立即同步到 Emby（带进度反馈）
            try:
                logger.info(f"[订阅] 开始同步到 Emby...")
                result, error = await asyncio.to_thread(
                    process_playlist, playlist_url, user_id
                )
                
                if error:
                    logger.warning(f"[订阅] 同步到 Emby 失败: {error}")
                    await query.message.reply_text(f"⚠️ 同步到 Emby 失败: {error}\nBot 会在后台定期重试。")
                else:
                    logger.info(f"[订阅] 同步完成: 匹配 {result['matched']}/{result['total']}, 未匹配 {result['unmatched']}")
                    msg = f"✅ **同步完成**\n\n"
                    msg += f"📋 歌单: `{result['name']}`\n"
                    msg += f"✅ 已匹配: {result['matched']}/{result['total']} 首\n"
                    
                    if result.get('unmatched', 0) > 0:
                        msg += f"❌ 未匹配: {result['unmatched']} 首（库中缺失）\n"
                        # msg += "\n💡 可使用 `/dlstatus` 查看后续下载进度"
                        
                        await query.message.reply_text(msg, parse_mode='Markdown')
                        
                        # 提供下载选项
                        unmatched_songs = result.get('all_unmatched', [])
                        if unmatched_songs:
                            logger.info(f"[订阅] 发现 {len(unmatched_songs)} 首缺失歌曲，提示用户下载")
                            # 显示未匹配歌曲列表（前10首）
                            unmatched_msg = f"📥 **发现 {len(unmatched_songs)} 首缺失歌曲**，是否现在下载？\n\n"
                            for i, s in enumerate(unmatched_songs[:5]):
                                unmatched_msg += f"• {s.get('title', '')} - {s.get('artist', '')}\n"
                            if len(unmatched_songs) > 5:
                                unmatched_msg += f"... 还有 {len(unmatched_songs) - 5} 首\n"
                            
                            keyboard = [[
                                InlineKeyboardButton("📥 立即下载缺失歌曲", callback_data=f"sync_dl_pending_{playlist_id}"),
                                InlineKeyboardButton("❌ 暂不下载", callback_data="menu_close")
                            ]]
                            await query.message.reply_text(
                                unmatched_msg,
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            # 保存未匹配歌曲到 context 供后续下载使用
                            context.user_data['pending_download_songs'] = unmatched_songs
                    else:
                        msg += "\n🎉 所有歌曲都已在库中！"
                        await query.message.reply_text(msg, parse_mode='Markdown')
                    
                    # 保存歌曲 ID 用于后续增量检查
                    if songs:
                        logger.info(f"[订阅] 保存歌曲 ID 用于增量检查...")
                        song_ids = [str(s.get('source_id') or s.get('id') or s.get('title', '')) for s in songs]
                        cursor.execute(
                            'UPDATE scheduled_playlists SET last_song_ids = ?, last_sync_at = CURRENT_TIMESTAMP WHERE playlist_url = ?',
                            (json.dumps(song_ids), playlist_url)
                        )
                        database_conn.commit()
                    
                    # 触发 Emby 扫库
                    logger.info(f"[订阅] 触发 Emby 扫库...")
                    asyncio.create_task(asyncio.to_thread(trigger_emby_library_scan))
                    
            except Exception as e:
                logger.error(f"[订阅] 立即同步失败: {e}")
                await query.message.reply_text(f"⚠️ 立即同步失败: {e}\nBot 会在后台定期重试。")
                
        except Exception as e:
            logger.error(f"[订阅] 订阅失败: {e}")
            await query.edit_message_text(f"❌ 订阅失败: {e}")


# ----------------------------------------------------------------------------------------------------------------------
# 手动元数据修复功能 / Fix Tags Feature
# ----------------------------------------------------------------------------------------------------------------------

def search_local_files(keyword: str) -> List[Path]:
    """
    在下载目录和上传目录搜索音频文件
    """
    files = []
    settings = get_ncm_settings()
    download_dir = Path(settings.get('download_dir', '') or settings.get('download_path', ''))
    
    # 搜索下载目录
    if download_dir.exists():
        try:
            files.extend(download_dir.rglob(f"*{keyword}*"))
        except Exception:
            pass
    
    # 搜索目标目录
    if MUSIC_TARGET_DIR.exists():
        try:
            files.extend(MUSIC_TARGET_DIR.rglob(f"*{keyword}*"))
        except Exception:
            pass

    # 搜索整理目录 (如 /music)
    organize_dir_str = settings.get('organize_dir', '') or settings.get('organize_target_dir', '')
    if organize_dir_str:
        organize_dir = Path(organize_dir_str)
        if organize_dir.exists() and organize_dir != download_dir and organize_dir != MUSIC_TARGET_DIR:
            try:
                files.extend(organize_dir.rglob(f"*{keyword}*"))
            except Exception:
                pass
        
    # 过滤非音频文件
    audio_files = []
    seen = set()
    for f in files:
        if f.suffix.lower() in ALLOWED_AUDIO_EXTENSIONS:
            if str(f) not in seen:
                audio_files.append(f)
                seen.add(str(f))
                
    # 按修改时间排序，最新的在前
    return sorted(audio_files, key=lambda x: x.stat().st_mtime, reverse=True)[:10]

async def cmd_fix_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    手动修复元数据命令
    用法: /fix_tags <文件名关键字>
    """
    user_id = str(update.effective_user.id)
    if str(ADMIN_USER_ID) not in [user_id, str(user_id)]:
        # Support comma separated list
        if user_id not in str(ADMIN_USER_ID).split(','):
            return

    if not context.args:
        await update.message.reply_text("❌ 请输入文件名关键字\n用法: `/fix_tags <关键字>`", parse_mode='Markdown')
        return

    keyword = " ".join(context.args)
    await update.message.reply_text(f"🔍 正在搜索包含 `{keyword}` 的本地文件...", parse_mode='Markdown')

    files = await asyncio.to_thread(search_local_files, keyword)

    if not files:
        await update.message.reply_text("❌ 未找到匹配的文件")
        return

    keyboard = []
    # 保存哈希映射到 context_user_data (简单起见，使用 MD5 hash 作为 key)
    import hashlib
    if 'file_map' not in context.user_data:
        context.user_data['file_map'] = {}
        
    for f in files:
        f_hash = hashlib.md5(str(f).encode()).hexdigest()[:8]
        context.user_data['file_map'][f_hash] = str(f)
        # 按钮显示文件名
        keyboard.append([InlineKeyboardButton(f.name, callback_data=f"fix_sel_{f_hash}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"找到 {len(files)} 个文件，请选择要修复的：", reply_markup=reply_markup)

async def handle_fix_metadata_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理元数据修复相关回调"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("fix_sel_"):
        # 用户选择了文件
        f_hash = data.replace("fix_sel_", "")
        file_path = context.user_data.get('file_map', {}).get(f_hash)
        
        if not file_path:
            await query.edit_message_text("❌ 文件信息已过期，请重新搜索")
            return
            
        context.user_data['fixing_file'] = file_path
        
        # 提取文件名作为默认搜索建议
        default_search = Path(file_path).stem
        # 去掉可能的歌手名
        if ' - ' in default_search:
            default_search = default_search.split(' - ')[-1]

        await query.edit_message_text(
            f"已选择文件：`{Path(file_path).name}`\n\n"
            f"请发送网易云音乐搜索关键词（例如：`{default_search}`）\n"
            f"或者发送 `qq <关键词>` 搜索 QQ 音乐\n"
            f"或者发送 /cancel 取消",
            parse_mode='Markdown'
        )
        
    elif data.startswith("fix_search_qq_"):
        # 用户点击了"搜QQ音乐"按钮
        keyword = data.replace("fix_search_qq_", "")
        await query.edit_message_text(f"🔍 正在 QQ 音乐搜索 `{keyword}`...", parse_mode='Markdown')
        
        from bot.ncm_downloader import MusicAutoDownloader
        settings = get_ncm_settings()
        downloader = MusicAutoDownloader(
            ncm_cookie=settings['cookie'], 
            qq_cookie=get_qq_cookie(),
            download_dir=settings.get('download_dir', settings.get('download_path', '/tmp')),
            proxy_url=settings['proxy_url'], 
            proxy_key=settings['proxy_key']
        )
        
        songs = await asyncio.to_thread(downloader.search_qq, keyword, limit=5)
        
        if not songs:
            await query.edit_message_text("❌ QQ 音乐未找到匹配歌曲，请尝试其他关键词")
            return

        keyboard = []
        for s in songs:
            btn_text = f"{s['title']} - {s['artist']} ({s['album']})"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"fix_apply_qq_{s['source_id']}")])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"QQ 音乐搜索结果 ({keyword})：", reply_markup=reply_markup)

    elif data.startswith("fix_apply_"):
        # 用户选择了匹配项
        is_qq = False
        if data.startswith("fix_apply_qq_"):
            is_qq = True
            song_id = data.replace("fix_apply_qq_", "")
        else:
            song_id = data.replace("fix_apply_", "")
            
        file_path = context.user_data.get('fixing_file')
        
        if not file_path:
            await query.edit_message_text("❌ 会话已过期，请重新开始")
            return
            
        await query.edit_message_text("⏳ 正在下载封面并写入元数据...\n(QQ 源可能需要较长时间获取详情)")
        
        # 初始化下载器
        from bot.ncm_downloader import MusicAutoDownloader
        settings = get_ncm_settings()
        downloader = MusicAutoDownloader(
            ncm_cookie=settings['cookie'], 
            qq_cookie=get_qq_cookie(),
            download_dir=settings.get('download_dir', settings.get('download_path', '/tmp')),
            proxy_url=settings['proxy_url'], 
            proxy_key=settings['proxy_key']
        )
        
        success = await asyncio.to_thread(
            downloader.apply_metadata_to_file, file_path, song_id, source='qq' if is_qq else 'ncm'
        )
        
        if success:
            await query.edit_message_text(f"✅ 元数据修复成功！\n文件：`{Path(file_path).name}`", parse_mode='Markdown')
            context.user_data.pop('fixing_file', None)
        else:
            await query.edit_message_text("❌ 写入失败，请查看日志")



async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息：主要是搜索"""
    text = update.message.text
    if not text:
        return

    # ------------------------------------------------------------------
    # 手动修复元数据：处理用户输入的搜索关键词
    # ------------------------------------------------------------------
    if 'fixing_file' in context.user_data and not text.startswith('/'):
        keyword = text
        is_qq_search = False
        
        if keyword.lower().startswith('qq '):
            keyword = keyword[3:].strip()
            is_qq_search = True
            await update.message.reply_text(f"🔍 正在 QQ 音乐搜索 `{keyword}`...", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"🔍 正在网易云搜索 `{keyword}`...", parse_mode='Markdown')
        
        # 初始化下载器用于搜索
        from bot.ncm_downloader import MusicAutoDownloader
        settings = get_ncm_settings()
        downloader = MusicAutoDownloader(
            ncm_cookie=settings['cookie'], 
            qq_cookie=get_qq_cookie(),
            download_dir=settings.get('download_dir', settings.get('download_path', '/tmp')),
            proxy_url=settings['proxy_url'], 
            proxy_key=settings['proxy_key']
        )
        
        songs = []
        if is_qq_search:
            songs = await asyncio.to_thread(downloader.search_qq, keyword, limit=5)
        else:
            songs = await asyncio.to_thread(downloader.ncm_api.search_song, keyword, limit=5)
        
        if not songs:
            msg = "❌ 未找到匹配歌曲，请尝试其他关键词"
            if not is_qq_search:
                msg += "\n或者尝试发送 `qq <关键词>` 搜索 QQ 音乐"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return

        keyboard = []
        for s in songs:
            # fix_apply_{song_id} or fix_apply_qq_{song_id}
            prefix = "fix_apply_qq_" if is_qq_search else "fix_apply_"
            btn_text = f"{s['title']} - {s['artist']} ({s['album']})"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"{prefix}{s['source_id']}")])
        
        # 如果是 NCM 搜索，添加切换到 QQ 的按钮
        if not is_qq_search:
            keyboard.append([InlineKeyboardButton("Switch to QQ Music Search ➡️", callback_data=f"fix_search_qq_{keyword}")])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("请选择匹配的歌曲：", reply_markup=reply_markup)
        return
    # ------------------------------------------------------------------

    # 忽略命令
    if text.startswith('/'):
        return

    # 检查是否是歌单链接（支持短链接 163cn.tv、c6.y.qq.com 等）
    playlist_type, playlist_id = parse_playlist_input(text)
    if playlist_type and playlist_id:
        if playlist_type == 'netease':
            ncm_cookie = get_ncm_cookie()
            if ncm_cookie:
                try:
                    name, songs = get_ncm_playlist_details(playlist_id)
                    if name:
                        msg = f"🎵 **发现网易云歌单**\n\n"
                        msg += f"📜 **名称**: {name}\n"
                        msg += f"🔢 **歌曲数**: {len(songs)} 首\n\n"
                        msg += "请选择操作："
                        
                        keyboard = [
                            [
                                InlineKeyboardButton("📥 立即下载", callback_data=f"pl_action_download_netease_{playlist_id}"),
                                InlineKeyboardButton("📅 订阅同步", callback_data=f"pl_action_subscribe_netease_{playlist_id}")
                            ]
                        ]
                        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                except Exception as e:
                    logger.error(f"解析歌单失败: {e}")
        elif playlist_type == 'qq':
            try:
                name, songs = get_qq_playlist_details(playlist_id)
                if name:
                    msg = f"🎵 **发现QQ音乐歌单**\n\n"
                    msg += f"📜 **名称**: {name}\n"
                    msg += f"🔢 **歌曲数**: {len(songs)} 首\n\n"
                    msg += "请选择操作："
                    
                    keyboard = [
                        [
                            InlineKeyboardButton("📥 立即下载", callback_data=f"pl_action_download_qq_{playlist_id}"),
                            InlineKeyboardButton("📅 订阅同步", callback_data=f"pl_action_subscribe_qq_{playlist_id}")
                        ]
                    ]
                    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
                    return
            except Exception as e:
                logger.error(f"解析QQ歌单失败: {e}")

    # 搜索（仅私聊且有权限时触发，避免在群里刷屏）
    user_id = str(update.effective_user.id)
    if update.message.chat.type == 'private' and user_id == ADMIN_USER_ID:
        await cmd_search(update, context)
    # 其他情况静默忽略，不回复



async def emby_webhook_notify_job(application):
    """处理 Emby Webhook 通知并发送到 Telegram"""
    from bot.web import get_webhook_notifications, set_webhook_bot
    
    # 设置 Bot 实例引用
    set_webhook_bot(application.bot)
    
    while True:
        try:
            if not EMBY_WEBHOOK_NOTIFY:
                await asyncio.sleep(60)
                continue
            
            # 获取待处理的通知
            notifications = get_webhook_notifications()
            
            if notifications and ADMIN_USER_ID:
                for notif in notifications:
                    try:
                        if notif.get('type') == 'library_new':
                            title = notif.get('title', '未知')
                            artist = notif.get('artist', '')
                            album = notif.get('album', '')
                            item_type = notif.get('item_type', '').lower()
                            
                            if item_type == 'audio':
                                emoji = "🎵"
                                type_name = "歌曲"
                            elif item_type == 'musicalbum':
                                emoji = "💿"
                                type_name = "专辑"
                            elif item_type == 'musicartist':
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
                                msg += f"💿 专辑: {album}"
                            
                            await application.bot.send_message(
                                chat_id=ADMIN_USER_ID,
                                text=msg,
                                parse_mode='Markdown'
                            )
                            
                    except Exception as e:
                        logger.debug(f"发送 Webhook 通知失败: {e}")
            
        except Exception as e:
            logger.error(f"Webhook 通知任务出错: {e}")
        
        await asyncio.sleep(30)  # 每 30 秒检查一次



# ============================================================
# 用户会员命令
# ============================================================

async def cmd_reg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """生成注册链接 /reg"""
    telegram_id = str(update.effective_user.id)
    
    # 查找或创建用户的邀请码
    cursor = database_conn.cursor()
    
    # 检查用户是否已绑定 web 账户
    cursor.execute('SELECT invite_code FROM web_users WHERE telegram_id = ?', (telegram_id,))
    row = cursor.fetchone()
    
    if row and row['invite_code']:
        invite_code = row['invite_code']
    else:
        # 生成新的邀请码
        import secrets
        invite_code = secrets.token_urlsafe(8)
        
        # 如果已有账户，更新邀请码；否则暂存（用户需先注册）
        if row:
            cursor.execute('UPDATE web_users SET invite_code = ? WHERE telegram_id = ?', (invite_code, telegram_id))
            database_conn.commit()
        else:
            # 用户未绑定，提示先绑定 Telegram 到 Web 账户
            await update.message.reply_text(
                "⚠️ 您还未绑定 Web 账户\n\n"
                "请先在 Web 管理端注册账户，然后使用 /bindtg 命令绑定您的 Telegram。\n"
                "绑定后即可生成邀请链接。"
            )
            return
    
    # 获取 Web URL
    web_url = os.environ.get('WEB_BASE_URL', 'http://localhost:8095')
    reg_link = f"{web_url.rstrip('/')}/register?invite={invite_code}"
    
    await update.message.reply_text(
        f"🔗 **您的邀请注册链接**\n\n"
        f"`{reg_link}`\n\n"
        f"将此链接发送给朋友，他们可通过此链接注册账户。",
        parse_mode='Markdown'
    )


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用卡密续期 /card <卡密>"""
    telegram_id = str(update.effective_user.id)
    
    if not context.args:
        await update.message.reply_text(
            "📝 **使用方法**\n\n"
            "`/card <卡密>`\n\n"
            "示例: `/card TGMUSIC-ABCD-1234`",
            parse_mode='Markdown'
        )
        return
    
    card_key = context.args[0].strip().upper()
    
    cursor = database_conn.cursor()
    
    # 查找用户的 Web 账户
    cursor.execute('SELECT id, expire_at FROM web_users WHERE telegram_id = ?', (telegram_id,))
    user_row = cursor.fetchone()
    
    if not user_row:
        await update.message.reply_text(
            "⚠️ 您还未绑定 Web 账户\n\n"
            "请先在 Web 管理端注册账户并绑定您的 Telegram。"
        )
        return
    
    user_id = user_row['id']
    current_expire = user_row['expire_at']
    
    # 查找卡密
    cursor.execute('SELECT * FROM card_keys WHERE card_key = ?', (card_key,))
    card_row = cursor.fetchone()
    
    if not card_row:
        await update.message.reply_text("❌ 卡密不存在，请检查是否输入正确")
        return
    
    if card_row['used_by']:
        await update.message.reply_text("❌ 该卡密已被使用")
        return
    
    duration_days = card_row['duration_days']
    
    # 计算新的到期时间
    now = datetime.now()
    if current_expire:
        try:
            base_date = datetime.fromisoformat(current_expire.replace('Z', '+00:00'))
            if base_date < now:
                base_date = now
        except:
            base_date = now
    else:
        base_date = now
    
    new_expire = base_date + timedelta(days=duration_days)
    
    # 更新卡密状态
    cursor.execute('''
        UPDATE card_keys SET used_by = ?, used_at = CURRENT_TIMESTAMP WHERE id = ?
    ''', (user_id, card_row['id']))
    
    # 更新用户到期时间
    cursor.execute('UPDATE web_users SET expire_at = ? WHERE id = ?', (new_expire.isoformat(), user_id))
    
    # 记录会员日志
    cursor.execute('''
        INSERT INTO membership_log (user_id, duration_days, source, source_detail)
        VALUES (?, ?, 'card', ?)
    ''', (user_id, duration_days, card_key))
    
    database_conn.commit()
    
    await update.message.reply_text(
        f"✅ **卡密兑换成功！**\n\n"
        f"📅 增加天数: {duration_days} 天\n"
        f"📆 新到期时间: {new_expire.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"感谢您的支持！",
        parse_mode='Markdown'
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看会员信息 /info"""
    telegram_id = str(update.effective_user.id)
    
    cursor = database_conn.cursor()
    cursor.execute('''
        SELECT username, emby_username, points, expire_at, created_at 
        FROM web_users WHERE telegram_id = ?
    ''', (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        await update.message.reply_text(
            "⚠️ 您还未绑定 Web 账户\n\n"
            "请在 Web 管理端注册，然后使用 /bindtg 绑定您的 Telegram。"
        )
        return
    
    username = row['username']
    emby_username = row['emby_username'] or '未绑定'
    points = row['points'] or 0
    expire_at = row['expire_at']
    created_at = row['created_at']
    
    # 计算到期信息
    if expire_at:
        try:
            expire_date = datetime.fromisoformat(expire_at.replace('Z', '+00:00'))
            now = datetime.now()
            if expire_date > now:
                days_left = (expire_date - now).days
                expire_text = f"✅ {expire_date.strftime('%Y-%m-%d')} (剩余 {days_left} 天)"
            else:
                expire_text = f"❌ 已过期 ({expire_date.strftime('%Y-%m-%d')})"
        except:
            expire_text = expire_at
    else:
        expire_text = "♾️ 永久会员"
    
    await update.message.reply_text(
        f"👤 **会员信息**\n\n"
        f"📛 用户名: `{username}`\n"
        f"🆔 Telegram: `{telegram_id}`\n"
        f"📺 Emby: {emby_username}\n"
        f"💰 积分: {points}\n"
        f"📅 到期时间: {expire_text}\n"
        f"🕐 注册时间: {created_at[:10] if created_at else '未知'}",
        parse_mode='Markdown'
    )


async def cmd_gencard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员生成卡密 /gencard <天数> <数量>"""
    telegram_id = str(update.effective_user.id)
    
    # 检查是否为管理员
    if telegram_id != str(ADMIN_USER_ID):
        await update.message.reply_text("⛔ 此命令仅限管理员使用")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "📝 **使用方法**\n\n"
            "`/gencard <天数> <数量>`\n\n"
            "示例: `/gencard 30 5` 生成5张30天卡密",
            parse_mode='Markdown'
        )
        return
    
    try:
        duration_days = int(context.args[0])
        count = int(context.args[1])
        
        if count > 50:
            count = 50
            
        if duration_days <= 0 or count <= 0:
            raise ValueError()
            
    except ValueError:
        await update.message.reply_text("❌ 参数格式错误，天数和数量必须是正整数")
        return
    
    import secrets
    cards = []
    cursor = database_conn.cursor()
    
    for _ in range(count):
        # 生成卡密格式: TGMUSIC-XXXX-XXXX
        part1 = secrets.token_hex(2).upper()
        part2 = secrets.token_hex(2).upper()
        card_key = f"TGMUSIC-{part1}-{part2}"
        
        cursor.execute('''
            INSERT INTO card_keys (card_key, duration_days, created_by)
            VALUES (?, ?, ?)
        ''', (card_key, duration_days, telegram_id))
        cards.append(card_key)
    
    database_conn.commit()
    
    cards_text = "\n".join([f"`{c}`" for c in cards])
    
    await update.message.reply_text(
        f"✅ **卡密生成成功**\n\n"
        f"📅 有效天数: {duration_days} 天\n"
        f"📦 生成数量: {count} 张\n\n"
        f"**卡密列表:**\n{cards_text}",
        parse_mode='Markdown'
    )


async def cmd_bindtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """绑定 Web 账户 (同时尝试绑定 Emby) /bweb <用户名> <密码>"""
    telegram_id = str(update.effective_user.id)
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "📝 **绑定 Web 账户**\n\n"
            "`/bweb <用户名> <密码>`\n\n"
            "说明: 此命令将绑定 Web 账户，并尝试使用相同密码绑定 Emby。\n"
            "如果 Emby 密码不同，请使用 `/bemby` 单独绑定。\n\n"
            "示例: `/bweb myuser mypassword`",
            parse_mode='Markdown'
        )
        return
    
    username = context.args[0]
    password = context.args[1]  # 注意：这里是明文密码，用于 Emby 认证
    
    cursor = database_conn.cursor()
    
    # 查找用户
    cursor.execute('''
        SELECT id, password_hash, telegram_id, emby_user_id, emby_username 
        FROM web_users WHERE username = ?
    ''', (username,))
    row = cursor.fetchone()
    
    if not row:
        await update.message.reply_text("❌ 用户名不存在")
        return
    
    # 验证 Web 密码
    import hashlib
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    if row['password_hash'] != password_hash:
        await update.message.reply_text("❌ 密码错误")
        return
    
    if row['telegram_id'] and row['telegram_id'] != telegram_id:
        await update.message.reply_text("❌ 该账户已绑定其他 Telegram")
        return
    
    # 1. 绑定 Telegram 到 Web 账户
    cursor.execute('UPDATE web_users SET telegram_id = ? WHERE id = ?', (telegram_id, row['id']))
    
    # 准备 Emby 信息
    current_emby_uid = row['emby_user_id']
    current_emby_name = row['emby_username']
    dual_bind_msg = ""
    
    # 2. 尝试绑定 Emby (如果尚未绑定)
    if not current_emby_uid:
        try:
            # 尝试使用相同密码登录 Emby
            logger.info(f"[bweb] 尝试自动绑定 Emby: {username}")
            token, emby_uid = authenticate_emby(EMBY_URL, username, password)
            
            if token and emby_uid:
                # 认证成功，更新 Web 用户表
                cursor.execute('UPDATE web_users SET emby_user_id = ?, emby_username = ? WHERE id = ?', 
                              (emby_uid, username, row['id']))
                current_emby_uid = emby_uid
                current_emby_name = username
                dual_bind_msg = "\n✅ Emby 账户同时也已绑定！(密码相同)"
            else:
                dual_bind_msg = "\n⚠️ Emby 自动绑定失败: 认证失败 (密码可能不同，请用 /bemby)"
        except Exception as e:
            dual_bind_msg = f"\n⚠️ Emby 自动绑定异常: {e}"
            logger.warning(f"[bweb] Emby 自动绑定异常: {e}")
    else:
        dual_bind_msg = f"\nℹ️ 此账号已关联 Emby: {current_emby_name}"

    # 3. 同步到 Telegram user_bindings 表 (用于bot功能)
    emby_synced = False
    if current_emby_uid and current_emby_name:
        try:
            # 检查是否已有绑定
            cursor.execute('SELECT telegram_id FROM user_bindings WHERE telegram_id = ?', (telegram_id,))
            existing = cursor.fetchone()
            
            if existing:
                # 更新现有绑定
                cursor.execute('''
                    UPDATE user_bindings 
                    SET emby_username = ?, emby_user_id = ?
                    WHERE telegram_id = ?
                ''', (current_emby_name, current_emby_uid, telegram_id))
            else:
                # 创建新绑定
                cursor.execute('''
                    INSERT INTO user_bindings (telegram_id, emby_username, emby_password, emby_user_id)
                    VALUES (?, ?, '', ?)
                ''', (telegram_id, current_emby_name, current_emby_uid))
            
            emby_synced = True
            logger.info(f"[bweb] 同步 Emby 绑定: TG={telegram_id} -> Emby={current_emby_name}")
        except Exception as e:
            logger.warning(f"[bweb] 同步 Emby 绑定失败: {e}")
            dual_bind_msg += f"\n❌ Bot 内部绑定同步失败"
    
    database_conn.commit()
    
    # 删除消息（包含密码）
    try:
        await update.message.delete()
    except:
        pass
    
    # 构建回复消息
    msg = f"✅ **Web 账户绑定成功！**\nUsername: `{username}`\n"
    msg += dual_bind_msg + "\n\n"
    
    msg += "现在您可以使用:\n"
    msg += "• /info 查看会员信息\n"
    msg += "• /reg 生成邀请链接\n"
    
    if emby_synced:
        msg += "• 直接发送歌单链接同步到 Emby"
    
    await update.effective_chat.send_message(msg, parse_mode='Markdown')


# ============================================================
# 主程序
# ============================================================


def main():
    """主程序入口"""
    # 初始化数据库
    global database_conn
    import sqlite3
    from bot.config import DATABASE_FILE
    database_conn = sqlite3.connect(str(DATABASE_FILE), check_same_thread=False)
    database_conn.row_factory = sqlite3.Row
    
    # 调用完整的数据库初始化函数
    init_database()
    logger.info("数据库已初始化")
    
    # 减少 Telegram 库的日志噪音 (Conflict 报错刷屏)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    logging.getLogger("telegram.ext").setLevel(logging.ERROR)
    
    # 初始化 requests session
    global requests_session
    requests_session = create_requests_session()
    logger.info("HTTP Session 已初始化")
    
    # 初始化下载管理器
    from bot.download_manager import init_download_manager as _init_dm
    global download_manager
    download_manager = _init_dm(str(DATABASE_FILE), max_concurrent=3, max_retries=3, retry_delay=2.0)
    logger.info("下载管理器已初始化")
    
    # 初始化 Emby 认证
    global emby_auth
    if EMBY_URL and EMBY_USERNAME and EMBY_PASSWORD:
        logger.info(f"正在连接 Emby: {EMBY_URL}")
        token, user_id = authenticate_emby(EMBY_URL, EMBY_USERNAME, EMBY_PASSWORD)
        if token and user_id:
            emby_auth['access_token'] = token
            emby_auth['user_id'] = user_id
            logger.info(f"Emby 认证成功，UserId: {user_id}")
        else:
            logger.warning("Emby 认证失败，部分功能可能不可用")
    else:
        logger.warning("未配置 Emby 凭据，歌单同步功能将不可用")
    
    # 启动 Bot
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, InlineQueryHandler, filters
    builder = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30)
    
    # 如果配置了 Local Bot API Server
    if TELEGRAM_API_URL:
        builder = builder.base_url(TELEGRAM_API_URL).base_file_url(TELEGRAM_API_URL.replace('/bot', '/file/bot'))
        logger.info(f"使用 Local Bot API Server: {TELEGRAM_API_URL}")
    
    app = builder.build()
    
    # 注意：大部分命令处理函数已在此文件中定义，无需导入
    # cmd_start, cmd_help, cmd_bind... 均在上方定义
    
    # 统计命令 (在 handlers/stats.py 中)
    from bot.handlers.stats import cmd_mystats, cmd_ranking, cmd_yearreview, cmd_daily
    
    # 注册命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    # app.add_handler(CommandHandler(["bind", "b"], cmd_bind)) # Legacy removed/replaced
    app.add_handler(CommandHandler("unbind", cmd_unbind))
    app.add_handler(CommandHandler(["status", "s"], cmd_status))
    app.add_handler(CommandHandler(["rescan", "scan", "rs"], cmd_rescan))
    app.add_handler(CommandHandler("ncmstatus", cmd_ncm_status))
    app.add_handler(CommandHandler(["search", "ss", "ws"], cmd_search))
    app.add_handler(CommandHandler(["album", "al"], cmd_album))
    app.add_handler(CommandHandler(["qqsearch", "qs"], cmd_qq_search))
    app.add_handler(CommandHandler(["qqalbum", "qa"], cmd_qq_album))
    app.add_handler(CommandHandler(["schedule", "sub"], cmd_schedule))
    app.add_handler(CommandHandler(["syncinterval", "synci"], cmd_syncinterval))
    app.add_handler(CommandHandler(["unschedule", "unsub"], cmd_unschedule))
    app.add_handler(CommandHandler(["scaninterval", "si"], cmd_scaninterval))
    app.add_handler(CommandHandler(["request", "req"], cmd_request))
    app.add_handler(CommandHandler(["myrequests", "mr"], cmd_myrequests))
    app.add_handler(CommandHandler(["dlstatus", "ds"], cmd_download_status))
    app.add_handler(CommandHandler(["dlqueue", "dq"], cmd_download_queue))
    app.add_handler(CommandHandler(["dlhistory", "dh"], cmd_download_history))
    
    # 统计命令
    app.add_handler(CommandHandler(["mystats", "ms"], cmd_mystats))
    app.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    app.add_handler(CommandHandler(["yearreview", "yr"], cmd_yearreview))
    app.add_handler(CommandHandler(["daily", "d"], cmd_daily))
    app.add_handler(CommandHandler(["fix_tags", "ft"], cmd_fix_tags))  # New command
    
    # 用户会员命令
    app.add_handler(CommandHandler("reg", cmd_reg))
    app.add_handler(CommandHandler("card", cmd_card))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("gencard", cmd_gencard))
    app.add_handler(CommandHandler(["bemby", "bemb"], cmd_bind)) # Emby Only
    app.add_handler(CommandHandler("bweb", cmd_bindtg)) # Web + Auto Emby
    
    # 回调 - 使用本地定义的 handlers
    app.add_handler(CallbackQueryHandler(handle_match_callback, pattern='^match_'))
    app.add_handler(CallbackQueryHandler(handle_download_callback, pattern='^download_'))
    app.add_handler(CallbackQueryHandler(handle_unmatched_page_callback, pattern='^unmatched_page_'))
    app.add_handler(CallbackQueryHandler(handle_need_dl_page_callback, pattern='^need_dl_page_'))
    app.add_handler(CallbackQueryHandler(handle_preview_callback, pattern='^preview_'))
    app.add_handler(CallbackQueryHandler(handle_qq_preview_callback, pattern='^qpreview_'))
    app.add_handler(CallbackQueryHandler(handle_search_download_callback, pattern='^dl_'))
    app.add_handler(CallbackQueryHandler(handle_qq_download_callback, pattern='^qdl_'))
    app.add_handler(CallbackQueryHandler(handle_sync_callback, pattern='^sync_'))
    app.add_handler(CallbackQueryHandler(handle_request_callback, pattern='^req_'))
    app.add_handler(CallbackQueryHandler(handle_retry_callback, pattern='^retry_'))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern='^menu_'))
    app.add_handler(CallbackQueryHandler(handle_fix_metadata_callback, pattern='^fix_'))  # New callback
    app.add_handler(CallbackQueryHandler(handle_playlist_action_callback, pattern='^pl_action_'))
    
    # Inline
    app.add_handler(InlineQueryHandler(handle_inline_query))
    
    # 消息
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # 全局错误处理器
    async def error_handler(update, context):
        """处理所有未捕获的异常"""
        import traceback
        error_msg = f"发生错误: {context.error}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        
        # 通知管理员
        if ADMIN_USER_ID:
            try:
                admin_msg = (
                    "⚠️ *Bot 错误报告*\n\n"
                    f"错误类型: `{type(context.error).__name__}`\n"
                    f"错误信息: `{str(context.error)[:200]}`\n"
                )
                if update and update.effective_user:
                    admin_msg += f"用户: `{update.effective_user.id}`\n"
                if update and update.effective_message:
                    admin_msg += f"消息: `{update.effective_message.text[:50] if update.effective_message.text else 'N/A'}`"
                
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=admin_msg,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"发送错误通知失败: {e}")
        
        # 回复用户
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ 操作过程中发生错误，请稍后重试。\n如果问题持续，请联系管理员。"
                )
            except:
                pass
    
    app.add_error_handler(error_handler)
    
    logger.info("Bot 启动成功！")
    from bot.config import NCM_COOKIE
    ncm_cookie = get_ncm_cookie()
    if ncm_cookie:
        logger.info("已配置网易云 Cookie，自动下载功能已启用")
    
    # Post Init
    async def post_init(application):
        from telegram import BotCommand
        commands = [
            BotCommand("start", "🏠 主菜单"),
            BotCommand("ws", "🔍 网易云搜歌"),
            BotCommand("qs", "🔍 QQ音乐搜歌"),
            BotCommand("fix_tags", "🏷️ 修复元数据"),
            BotCommand("ds", "📊 下载状态"),
            BotCommand("dq", "📥 下载队列"),
            BotCommand("sub", "📅 订阅管理"),
            BotCommand("mr", "📋 我的申请"),
            BotCommand("req", "📝 申请歌曲"),
            BotCommand("rank", "🏆 排行榜"),
            BotCommand("ms", "📊 我的统计"),
            BotCommand("s", "📊 系统状态"),
            BotCommand("b", "🔑 绑定Emby"),
            BotCommand("scan", "🔄 扫描Emby"),
            BotCommand("help", "❓ 帮助"),
            BotCommand("al", "💿 网易云专辑"),
            BotCommand("qa", "💿 QQ音乐专辑"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("已注册 Telegram 命令菜单")
        
        if download_manager:
            await download_manager.start()
        
        # 启动任务
        asyncio.create_task(scheduled_sync_job(application))
        asyncio.create_task(scheduled_ranking_job(application))
        asyncio.create_task(radar_push_job(application))
        asyncio.create_task(refresh_qq_cookie_task(application))
        asyncio.create_task(scheduled_emby_scan_job(application))
        asyncio.create_task(daily_stats_job(application))
        asyncio.create_task(check_expired_users_job(application))
        
        # Webhook
        from bot.web import set_webhook_bot
        set_webhook_bot(application.bot)
        asyncio.create_task(emby_webhook_notify_job(application))
        
        # 启动文件整理器（如果配置了自动整理）
        asyncio.create_task(start_file_organizer_if_enabled(application))
        
    app.post_init = post_init
    
    # Pyrogram (Optional)
    if TG_API_ID and TG_API_HASH:
        # Check if pyro.py exists in handlers, assuming check before import
        # Or wrap in try-except
        try:
             from bot.handlers.pyro import start_pyrogram_client
             asyncio.get_event_loop().run_until_complete(start_pyrogram_client())
        except ImportError:
             logger.warning("Pyrogram handler not found, skipping large file support")
    
    app.run_polling()

if __name__ == '__main__':
    main()
