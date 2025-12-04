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
from datetime import datetime, timedelta
from urllib.parse import urljoin
from pathlib import Path
from cryptography.fernet import Fernet

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rapidfuzz import fuzz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

# --- 全局配置 ---
APP_NAME = "TGmusicbot"
APP_VERSION = "2.1.0"
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
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_API_URL = os.environ.get('TELEGRAM_API_URL', '')  # Local Bot API Server URL, e.g. http://localhost:8081/bot
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')
EMBY_URL = os.environ.get('EMBY_URL')
EMBY_USERNAME = os.environ.get('EMBY_USERNAME')
EMBY_PASSWORD = os.environ.get('EMBY_PASSWORD')
MAKE_PLAYLIST_PUBLIC = os.environ.get('MAKE_PLAYLIST_PUBLIC', 'false').lower() == 'true'

# 网易云下载配置
NCM_COOKIE = os.environ.get('NCM_COOKIE', '')  # 网易云登录 Cookie
NCM_QUALITY = os.environ.get('NCM_QUALITY', 'exhigh')  # 下载音质: standard/higher/exhigh/lossless/hires
AUTO_DOWNLOAD = os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'  # 是否自动下载缺失歌曲

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
EMBY_PLAYLIST_ADD_BATCH_SIZE = 200

# --- 全局状态 ---
emby_library_data = []
emby_auth = {'access_token': None, 'user_id': None}
database_conn = None
requests_session = None
ncm_downloader = None  # 网易云下载器实例


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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
    return fernet.decrypt(encrypted_password.encode()).decode()

def _normalize_artists(artist_str: str) -> set:
    if not isinstance(artist_str, str): return set()
    s = artist_str.lower()
    s = re.sub(r'\s*[\(（].*?[\)）]', '', s)
    s = re.sub(r'\s*[\[【].*?[\]】]', '', s)
    s = re.sub(r'\s+(feat|ft|with|vs|presents|pres\.|starring)\.?\s+', '/', s)
    s = re.sub(r'\s*&\s*', '/', s)
    return {artist.strip() for artist in re.split(r'\s*[/•,、]\s*', s) if artist.strip()}

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
            response = requests_session.post(api_url, params=params, json=data, headers=headers, timeout=timeout)
        elif method.upper() == 'DELETE':
            response = requests_session.delete(api_url, params=params, headers=headers, timeout=timeout)
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
            'Fields': 'Id,Name,ArtistItems'
        }
        response = call_emby_api(f"Users/{scan_user_id}/Items", params, user_auth=temp_auth, timeout=(15, 180))
        
        if response and 'Items' in response:
            items = response['Items']
            if not items: break
            for item in items:
                artists = "/".join([a.get('Name', '') for a in item.get('ArtistItems', [])])
                scanned_songs.append({
                    'id': str(item.get('Id')),
                    'title': html.unescape(item.get('Name', '')),
                    'artist': html.unescape(artists)
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
    
    return None, None


def extract_playlist_id(playlist_url: str, platform: str) -> str:
    """从歌单 URL 中提取 ID"""
    playlist_type, playlist_id = parse_playlist_input(playlist_url)
    if playlist_type == platform or (platform == 'netease' and playlist_type == 'ncm'):
        return playlist_id
    return None

def get_qq_playlist_details(playlist_id):
    params = {'type': 1, 'utf8': 1, 'disstid': playlist_id, 'loginUin': 0}
    headers = {'Referer': 'https://y.qq.com/', 'User-Agent': 'Mozilla/5.0'}
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
                songs.append({
                    'source_id': str(s.get('songid') or s.get('id')),
                    'title': html.unescape(s.get('songname') or s.get('title', '')),
                    'artist': html.unescape(artists),
                    'platform': 'QQ'
                })
        return name, songs
    except Exception as e:
        logger.error(f"获取 QQ 歌单失败: {e}")
        return None, []

def get_ncm_playlist_details(playlist_id):
    headers = {'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests_session.get(NCM_API_PLAYLIST_DETAIL_URL, 
                                        params={'id': playlist_id, 'n': 100000},
                                        headers=headers, timeout=(10, 20))
        response.raise_for_status()
        data = response.json()
        playlist = data.get('playlist')
        if not playlist: return None, []
        
        name = html.unescape(playlist.get('name', f"网易云歌单{playlist_id}"))
        track_ids = [str(t['id']) for t in playlist.get('trackIds', [])]
        
        songs = []
        for i in range(0, len(track_ids), 200):
            batch_ids = track_ids[i:i + 200]
            detail_response = requests_session.get(NCM_API_SONG_DETAIL_URL,
                                                   params={'ids': f"[{','.join(batch_ids)}]"},
                                                   headers=headers, timeout=(10, 15))
            detail_response.raise_for_status()
            for s in detail_response.json().get('songs', []):
                artist_list = s.get('ar') or s.get('artists') or []
                artists = "/".join([a.get('name', '') for a in artist_list])
                songs.append({
                    'source_id': str(s.get('id')),
                    'title': html.unescape(s.get('name', '')),
                    'artist': html.unescape(artists),
                    'platform': 'NCM'
                })
        return name, songs
    except Exception as e:
        logger.error(f"获取网易云歌单失败: {e}")
        return None, []


# ============================================================
# 匹配逻辑
# ============================================================

def find_best_match(source_track, candidates, match_mode):
    if not candidates: return None
    source_title = source_track.get('title', '').strip()
    source_artist = source_track.get('artist', '').strip()
    
    if match_mode == "完全匹配":
        source_artists_norm = sorted(list(_normalize_artists(source_artist)))
        for track in candidates:
            if source_title == track.get('title', '').strip():
                track_artists_norm = sorted(list(_normalize_artists(track.get('artist', ''))))
                if source_artists_norm == track_artists_norm:
                    return track
        return None
    
    # 模糊匹配
    best_match, best_score = None, -1
    source_title_lower = source_title.lower()
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
        
        score = title_pts + artist_pts
        if score > best_score:
            best_match, best_score = track, score
    
    return best_match if best_score >= MATCH_THRESHOLD else None


def process_playlist(playlist_url, user_id=None, force_public=False, user_binding=None, match_mode="模糊匹配"):
    playlist_type, playlist_id = parse_playlist_input(playlist_url)
    if not playlist_type:
        return None, "无法识别的歌单链接"
    
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
        if match:
            matched_ids.append(match['id'])
        else:
            unmatched.append(source_track)
    
    logger.info(f"匹配完成: {len(matched_ids)} 成功, {len(unmatched)} 失败")
    
    if not matched_ids:
        return None, f"歌单 '{source_name}' 未匹配到任何歌曲"
    
    # 删除同名歌单
    user_api_id = temp_auth['user_id'] if temp_auth else emby_auth['user_id']
    for p in get_user_emby_playlists(temp_auth or emby_auth):
        if p.get('name') == source_name:
            call_emby_api(f"Items/{p['id']}", {}, method='DELETE', user_auth=temp_auth)
            break
    
    # 创建歌单
    is_public = force_public or (MAKE_PLAYLIST_PUBLIC and user_id == ADMIN_USER_ID)
    create_response = call_emby_api("Playlists", 
                                   {'Name': source_name, 'MediaType': 'Audio', 'UserId': user_api_id},
                                   method='POST', data={'Name': source_name, 'MediaType': 'Audio'},
                                   user_auth=temp_auth)
    
    if not create_response or 'Id' not in create_response:
        return None, "创建歌单失败"
    
    new_playlist_id = create_response['Id']
    if is_public:
        call_emby_api(f"Items/{new_playlist_id}/MakePublic", {}, method='POST', user_auth=temp_auth)
    
    # 添加歌曲
    unique_ids = list(dict.fromkeys(matched_ids))
    for i in range(0, len(unique_ids), EMBY_PLAYLIST_ADD_BATCH_SIZE):
        batch = unique_ids[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
        call_emby_api(f"Playlists/{new_playlist_id}/Items",
                     {'Ids': ",".join(batch), 'UserId': user_api_id},
                     method='POST', user_auth=temp_auth)
        time.sleep(0.3)
    
    # 记录到数据库
    save_playlist_record(user_id, source_name, playlist_type, len(source_songs), len(matched_ids))
    
    result = {
        'name': source_name,
        'total': len(source_songs),
        'matched': len(matched_ids),
        'unmatched': len(unmatched),
        'unmatched_songs': unmatched[:15],  # 显示前15首
        'all_unmatched': unmatched,  # 保存所有未匹配歌曲用于下载
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, playlist_url)
        )
    ''')
    
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


# ============================================================
# 定时同步歌单
# ============================================================

def add_scheduled_playlist(telegram_id: str, playlist_url: str, playlist_name: str, platform: str, song_ids: list):
    """添加定时同步歌单"""
    if not database_conn:
        return False
    try:
        cursor = database_conn.cursor()
        song_ids_json = json.dumps(song_ids)
        cursor.execute('''
            INSERT OR REPLACE INTO scheduled_playlists 
            (telegram_id, playlist_url, playlist_name, platform, last_song_ids, last_sync_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (str(telegram_id), playlist_url, playlist_name, platform, song_ids_json))
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
        cursor = database_conn.cursor()
        if telegram_id:
            cursor.execute('''
                SELECT id, telegram_id, playlist_url, playlist_name, platform, last_song_ids, last_sync_at
                FROM scheduled_playlists WHERE telegram_id = ? ORDER BY created_at DESC
            ''', (str(telegram_id),))
        else:
            cursor.execute('''
                SELECT id, telegram_id, playlist_url, playlist_name, platform, last_song_ids, last_sync_at
                FROM scheduled_playlists ORDER BY created_at DESC
            ''')
        rows = cursor.fetchall()
        return [
            {
                'id': row[0],
                'telegram_id': row[1],
                'playlist_url': row[2],
                'playlist_name': row[3],
                'platform': row[4],
                'last_song_ids': json.loads(row[5]) if row[5] else [],
                'last_sync_at': row[6]
            }
            for row in rows
        ]
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

def update_scheduled_playlist_songs(playlist_id: int, song_ids: list):
    """更新歌单的歌曲列表"""
    if not database_conn:
        return False
    try:
        cursor = database_conn.cursor()
        song_ids_json = json.dumps(song_ids)
        cursor.execute('''
            UPDATE scheduled_playlists SET last_song_ids = ?, last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (song_ids_json, playlist_id))
        database_conn.commit()
        return True
    except:
        return False


async def check_playlist_updates(app):
    """检查歌单更新并同步新歌曲"""
    logger.info("开始检查歌单更新...")
    
    playlists = get_scheduled_playlists()
    if not playlists:
        return
    
    for playlist in playlists:
        try:
            telegram_id = playlist['telegram_id']
            playlist_url = playlist['playlist_url']
            platform = playlist['platform']
            old_song_ids = set(playlist['last_song_ids'])
            
            # 获取歌单最新歌曲
            if platform == 'netease':
                playlist_id = extract_playlist_id(playlist_url, 'netease')
                if not playlist_id:
                    continue
                playlist_name, songs = get_ncm_playlist_details(playlist_id)
            elif platform == 'qq':
                playlist_id = extract_playlist_id(playlist_url, 'qq')
                if not playlist_id:
                    continue
                playlist_name, songs = get_qq_playlist_details(playlist_id)
            else:
                continue
            
            if not songs:
                continue
            
            # 计算新增歌曲
            current_song_ids = [str(s.get('id', s.get('title', ''))) for s in songs]
            new_songs = [s for s in songs if str(s.get('id', s.get('title', ''))) not in old_song_ids]
            
            if new_songs:
                logger.info(f"歌单 '{playlist['playlist_name']}' 发现 {len(new_songs)} 首新歌曲")
                
                # 发送通知
                try:
                    message = f"🔔 **歌单更新通知**\n\n"
                    message += f"📋 歌单: `{playlist['playlist_name']}`\n"
                    message += f"🆕 新增: {len(new_songs)} 首歌曲\n\n"
                    for i, s in enumerate(new_songs[:5]):
                        message += f"{i+1}. {s['title']} - {s['artist']}\n"
                    if len(new_songs) > 5:
                        message += f"... 还有 {len(new_songs) - 5} 首\n"
                    
                    # 添加下载按钮
                    keyboard = [
                        [
                            InlineKeyboardButton("📥 下载新歌", callback_data=f"sync_dl_{playlist['id']}"),
                            InlineKeyboardButton("🔄 同步到Emby", callback_data=f"sync_emby_{playlist['id']}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"发送歌单更新通知失败: {e}")
            
            # 更新歌曲列表
            update_scheduled_playlist_songs(playlist['id'], current_song_ids)
            
        except Exception as e:
            logger.error(f"检查歌单 '{playlist.get('playlist_name', '')}' 更新失败: {e}")
    
    logger.info("歌单更新检查完成")


async def scheduled_sync_job(app):
    """定时同步任务"""
    while True:
        try:
            # 每 6 小时检查一次
            await asyncio.sleep(6 * 60 * 60)
            await check_playlist_updates(app)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"定时同步任务出错: {e}")
            await asyncio.sleep(60)  # 出错后等待 1 分钟重试


async def scheduled_emby_scan_job(app):
    """定时扫描 Emby 媒体库"""
    # 获取扫描间隔（优先数据库配置）
    def get_scan_interval():
        try:
            if database_conn:
                cursor = database_conn.cursor()
                cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
                row = cursor.fetchone()
                if row:
                    return int(row[0] if isinstance(row, tuple) else row['value'])
        except:
            pass
        return EMBY_SCAN_INTERVAL
    
    while True:
        try:
            interval = get_scan_interval()
            if interval <= 0:
                # 禁用定时扫描，每小时检查一次配置是否变化
                await asyncio.sleep(60 * 60)
                continue
            
            # 等待指定时间
            await asyncio.sleep(interval * 60 * 60)
            
            # 执行扫描
            logger.info("开始定时扫描 Emby 媒体库...")
            if emby_auth.get('access_token'):
                old_count = len(emby_library_data)
                scan_emby_library(True, emby_auth['user_id'], emby_auth['access_token'])
                new_count = len(emby_library_data)
                
                if new_count != old_count:
                    logger.info(f"Emby 媒体库更新: {old_count} -> {new_count} 首")
                    # 通知管理员
                    if ADMIN_USER_ID:
                        try:
                            await app.bot.send_message(
                                chat_id=ADMIN_USER_ID,
                                text=f"🔄 Emby 媒体库已自动更新\n\n"
                                     f"📊 歌曲数量: {old_count} → {new_count}\n"
                                     f"📈 变化: {'+' if new_count > old_count else ''}{new_count - old_count}"
                            )
                        except:
                            pass
                else:
                    logger.info(f"Emby 媒体库无变化: {new_count} 首")
            else:
                logger.warning("Emby 未认证，跳过定时扫描")
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"定时扫描 Emby 出错: {e}")
            await asyncio.sleep(60)

def get_ncm_settings():
    """获取网易云下载设置（优先从数据库读取，否则从环境变量）"""
    default_settings = {
        'ncm_quality': os.environ.get('NCM_QUALITY', 'exhigh'),
        'auto_download': os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true',
        'download_mode': 'local',
        'download_dir': str(MUSIC_TARGET_DIR),
        'musictag_dir': ''
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
        
        return {
            'ncm_quality': ncm_quality,
            'auto_download': auto_download,
            'download_mode': download_mode,
            'download_dir': download_dir,
            'musictag_dir': musictag_dir
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

**歌单同步功能：**
直接发送 QQ音乐/网易云音乐 歌单链接

**音乐上传功能：**
直接发送音频文件或文档

**搜索下载功能：**
/search <关键词> - 搜索歌曲并下载
/album <专辑名> - 搜索专辑并下载

**定时同步：**
/schedule - 查看已订阅的歌单
/unschedule <序号> - 取消订阅

**命令列表：**
/start - 主菜单
/help - 帮助信息
/bind <用户名> <密码> - 绑定 Emby
/unbind - 解除绑定
/status - 查看状态
/search <关键词> - 搜索歌曲
/album <专辑名> - 搜索专辑
/schedule - 查看订阅歌单
/unschedule <序号> - 取消订阅
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
    await query.edit_message_text(f"正在处理... (模式: {match_mode})")
    
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
                # 获取歌曲 ID 列表用于后续比较
                song_ids = [str(s.get('id', s.get('title', ''))) for s in result.get('all_unmatched', []) + result.get('unmatched_songs', [])]
                # 从原始歌单获取
                if playlist_type == "netease":
                    _, songs = get_ncm_playlist_details(extract_playlist_id(playlist_url, 'netease'))
                else:
                    _, songs = get_qq_playlist_details(extract_playlist_id(playlist_url, 'qq'))
                if songs:
                    song_ids = [str(s.get('id', s.get('title', ''))) for s in songs]
                add_scheduled_playlist(user_id, playlist_url, result['name'], playlist_type, song_ids)
            
            msg = f"✅ **歌单同步完成**\n\n"
            msg += f"📋 歌单: `{result['name']}`\n"
            msg += f"🎯 模式: `{result['mode']}`\n"
            msg += f"📊 总数: {result['total']} 首\n"
            msg += f"✅ 匹配: {result['matched']} 首\n"
            msg += f"❌ 未匹配: {result['unmatched']} 首\n"
            msg += f"📅 已添加到定时同步\n"
            
            if result['unmatched_songs']:
                msg += "\n**未匹配歌曲：**\n"
                for i, s in enumerate(result['unmatched_songs'][:10]):
                    msg += f"`{i+1}. {s['title']} - {s['artist']}`\n"
                if result['unmatched'] > 10:
                    msg += f"...还有 {result['unmatched'] - 10} 首\n"
            
            # 检查是否可以自动下载（网易云歌单且有未匹配歌曲时）
            ncm_unmatched = [s for s in result.get('all_unmatched', result.get('unmatched_songs', [])) if s.get('platform') == 'NCM']
            keyboard = None
            if ncm_unmatched and user_id == ADMIN_USER_ID:
                # 保存未匹配歌曲到用户数据
                context.user_data['unmatched_ncm_songs'] = ncm_unmatched
                msg += f"\n💡 检测到 {len(ncm_unmatched)} 首网易云歌曲可自动下载"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 自动下载缺失歌曲", callback_data="download_missing")]
                ])
            
            await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
    except Exception as e:
        logger.exception(f"处理歌单失败: {e}")
        await query.message.reply_text(f"处理失败: {e}")


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
        
        downloader = MusicAutoDownloader(ncm_cookie, str(download_path))
        
        # 检查登录状态
        logged_in, info = downloader.check_ncm_login()
        if not logged_in:
            await query.message.reply_text("❌ 网易云 Cookie 已失效，请更新")
            return
        
        await query.message.reply_text(f"🎵 网易云登录成功: {info.get('nickname')} (VIP: {'是' if info.get('is_vip') else '否'})")
        
        # 创建进度消息
        progress_msg = await query.message.reply_text(f"📥 正在下载 0/{len(ncm_songs)}...")
        last_update_time = [0]  # 用列表来允许在闭包中修改
        main_loop = asyncio.get_running_loop()  # 在主线程获取 loop
        
        async def update_progress(current, total, song):
            """更新下载进度"""
            import time as time_module
            now = time_module.time()
            # 限制更新频率，避免 Telegram API 限流
            if now - last_update_time[0] < 2:
                return
            last_update_time[0] = now
            try:
                await progress_msg.edit_text(
                    f"📥 正在下载 {current}/{total}\n"
                    f"🎵 `{song.get('title', '')} - {song.get('artist', '')}`",
                    parse_mode='Markdown'
                )
            except:
                pass
        
        # 包装同步回调为异步
        def sync_progress_callback(current, total, song):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        # 开始下载
        success_files, failed_songs = await asyncio.to_thread(
            downloader.download_missing_songs,
            ncm_songs,
            download_quality,
            sync_progress_callback
        )
        
        # 如果设置了 MusicTag 模式，移动文件到 MusicTag 目录
        moved_files = []
        if download_mode == 'musictag' and musictag_dir and success_files:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            
            for file_path in success_files:
                try:
                    src = Path(file_path)
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                    logger.info(f"已移动文件到 MusicTag: {src.name}")
                except Exception as e:
                    logger.error(f"移动文件失败 {file_path}: {e}")
        
        msg = f"📥 **下载完成**\n\n"
        msg += f"🎵 音质: `{download_quality}`\n"
        msg += f"✅ 成功: {len(success_files)} 首\n"
        msg += f"❌ 失败: {len(failed_songs)} 首\n"
        
        if success_files:
            if moved_files:
                msg += f"\n📁 文件已转移到 MusicTag: `{musictag_dir}`\n"
                msg += "💡 等待 MusicTag 刮削整理后，Emby 扫库即可\n"
            else:
                msg += f"\n📁 文件已保存到: `{download_dir}`\n"
            
            msg += "\n**下载成功的歌曲：**\n"
            for i, f in enumerate(success_files[:10]):
                msg += f"`{i+1}. {Path(f).name}`\n"
            if len(success_files) > 10:
                msg += f"...还有 {len(success_files) - 10} 首\n"
        
        if failed_songs:
            msg += "\n**下载失败的歌曲：**\n"
            for i, s in enumerate(failed_songs[:5]):
                msg += f"`{i+1}. {s['title']} - {s['artist']}`\n"
        
        await query.message.reply_text(msg, parse_mode='Markdown')
        
        # 删除进度消息
        try:
            await progress_msg.delete()
        except:
            pass
        
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
                    logger.error(f"自动扫库失败: {e}")
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
        await update.message.reply_text("格式: /bind <用户名> <密码>")
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
    
    await update.message.reply_text(f"🔍 正在搜索: {keyword}...")
    
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        results = api.search_song(keyword, limit=10)
        
        if not results:
            await update.message.reply_text("未找到相关歌曲")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['search_results'] = results
        
        msg = f"🎵 **搜索结果** ({len(results)} 首)\n\n"
        keyboard_buttons = []
        
        for i, song in enumerate(results):
            msg += f"`{i+1}.` {song['title']} - {song['artist']}\n"
            msg += f"    📀 {song.get('album', '未知专辑')}\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {i+1}. {song['title'][:20]}", callback_data=f"dl_song_{i}")
            ])
        
        keyboard_buttons.append([InlineKeyboardButton("📥 全部下载", callback_data="dl_song_all")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
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
        
        msg = f"💿 **专辑搜索结果** ({len(results)} 张)\n\n"
        keyboard_buttons = []
        
        for i, album in enumerate(results):
            msg += f"`{i+1}.` {album['name']}\n"
            msg += f"    🎤 {album['artist']} · {album['size']} 首歌\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {album['name'][:25]}", callback_data=f"dl_album_{i}")
            ])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"搜索专辑失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


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
    
    msg = "📅 **定时同步歌单**\n\n"
    for i, p in enumerate(playlists, 1):
        platform_icon = "🔴" if p['platform'] == 'netease' else "🟢"
        last_sync = p['last_sync_at'][:16] if p['last_sync_at'] else "未同步"
        msg += f"`{i}.` {platform_icon} {p['playlist_name']}\n"
        msg += f"    📊 {len(p['last_song_ids'])} 首 · 最后同步: {last_sync}\n\n"
    
    msg += f"💡 使用 `/unschedule <序号>` 取消订阅"
    await update.message.reply_text(msg, parse_mode='Markdown')


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
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("无权执行此操作")
        return
    
    data = query.data
    
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
            old_song_ids = set(playlist['last_song_ids'])
            
            if platform == 'netease':
                playlist_id_str = extract_playlist_id(playlist_url, 'netease')
                _, songs = get_ncm_playlist_details(playlist_id_str)
            elif platform == 'qq':
                playlist_id_str = extract_playlist_id(playlist_url, 'qq')
                _, songs = get_qq_playlist_details(playlist_id_str)
            else:
                await query.message.reply_text("❌ 不支持的平台")
                return
            
            new_songs = [s for s in songs if str(s.get('id', s.get('title', ''))) not in old_song_ids]
            
            if not new_songs:
                await query.message.reply_text("没有新歌曲需要下载")
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
            
            downloader = MusicAutoDownloader(ncm_cookie, str(download_path))
            
            progress_msg = await query.message.reply_text(f"📥 下载中 0/{len(new_songs)}...")
            main_loop = asyncio.get_running_loop()
            
            async def update_progress(current, total, song):
                try:
                    await progress_msg.edit_text(
                        f"📥 下载中 {current}/{total}\n"
                        f"🎵 `{song.get('title', '')} - {song.get('artist', '')}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
            
            def sync_progress_callback(current, total, song):
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
                )
            
            success_files, failed = await asyncio.to_thread(
                downloader.download_missing_songs,
                new_songs,
                download_quality,
                sync_progress_callback
            )
            
            try:
                await progress_msg.delete()
            except:
                pass
            
            # 更新歌曲列表
            current_song_ids = [str(s.get('id', s.get('title', ''))) for s in songs]
            update_scheduled_playlist_songs(playlist['id'], current_song_ids)
            
            await query.message.reply_text(
                f"✅ 下载完成\n成功: {len(success_files)} 首\n失败: {len(failed)} 首"
            )
            
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


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """申请补全歌曲"""
    user_id = str(update.effective_user.id)
    
    # 检查申请权限
    if not check_user_permission(user_id, 'request'):
        await update.message.reply_text("❌ 你没有申请权限，请联系管理员")
        return
    
    args = ' '.join(context.args) if context.args else ''
    
    if not args:
        await update.message.reply_text(
            "📝 **申请补全歌曲**\n\n"
            "格式: `/request 歌曲名 - 歌手`\n\n"
            "示例:\n"
            "`/request 晴天 - 周杰伦`\n"
            "`/request 七里香 - 周杰伦 - 专辑:七里香`\n\n"
            "你也可以附带歌曲链接:\n"
            "`/request 晴天 - 周杰伦 https://music.163.com/song?id=xxx`",
            parse_mode='Markdown'
        )
        return
    
    # 解析歌曲信息
    import re
    url_match = re.search(r'https?://\S+', args)
    source_url = url_match.group(0) if url_match else None
    song_info = args.replace(source_url, '').strip() if source_url else args
    
    parts = [p.strip() for p in song_info.split('-')]
    song_name = parts[0] if parts else song_info
    artist = parts[1] if len(parts) > 1 else None
    album = None
    
    # 检查是否有专辑信息
    for part in parts[2:]:
        if part.startswith('专辑:') or part.startswith('专辑：'):
            album = part.split(':', 1)[-1].split('：', 1)[-1].strip()
            break
    
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('''
                INSERT INTO song_requests (telegram_id, song_name, artist, album, source_url)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, song_name, artist, album, source_url))
            database_conn.commit()
            request_id = cursor.lastrowid
            
            await update.message.reply_text(
                f"✅ 申请已提交\n\n"
                f"🎵 歌曲: {song_name}\n"
                f"👤 歌手: {artist or '未知'}\n"
                f"💿 专辑: {album or '未知'}\n\n"
                f"管理员审核后会通知你结果"
            )
            
            # 通知管理员
            if ADMIN_USER_ID:
                user = update.effective_user
                user_info = f"@{user.username}" if user.username else f"{user.first_name} ({user_id})"
                
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ 批准", callback_data=f"req_approve_{request_id}"),
                        InlineKeyboardButton("❌ 拒绝", callback_data=f"req_reject_{request_id}")
                    ],
                    [
                        InlineKeyboardButton("🔍 搜索下载", callback_data=f"req_search_{request_id}")
                    ]
                ])
                
                admin_msg = (
                    f"📝 **新歌曲申请**\n\n"
                    f"👤 用户: {user_info}\n"
                    f"🎵 歌曲: {song_name}\n"
                    f"👤 歌手: {artist or '未知'}\n"
                    f"💿 专辑: {album or '未知'}\n"
                )
                if source_url:
                    admin_msg += f"🔗 链接: {source_url}\n"
                
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
        logger.error(f"提交歌曲申请失败: {e}")
        await update.message.reply_text(f"❌ 提交失败: {e}")


async def cmd_myrequests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看我的申请"""
    user_id = str(update.effective_user.id)
    
    try:
        if database_conn:
            cursor = database_conn.cursor()
            cursor.execute('''
                SELECT * FROM song_requests 
                WHERE telegram_id = ? 
                ORDER BY created_at DESC 
                LIMIT 10
            ''', (user_id,))
            rows = cursor.fetchall()
            
            if not rows:
                await update.message.reply_text("📝 你还没有提交过申请")
                return
            
            msg = "📝 **我的歌曲申请**\n\n"
            for row in rows:
                status_emoji = {'pending': '⏳', 'approved': '✅', 'rejected': '❌'}.get(row['status'], '❓')
                msg += f"{status_emoji} {row['song_name']}"
                if row['artist']:
                    msg += f" - {row['artist']}"
                msg += f"\n   状态: {row['status']}"
                if row['admin_note']:
                    msg += f"\n   备注: {row['admin_note']}"
                msg += "\n\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ 查询失败: {e}")


async def handle_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理歌曲申请审核回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if user_id != ADMIN_USER_ID:
        await query.answer("仅管理员可操作", show_alert=True)
        return
    
    data = query.data
    
    if data.startswith("req_approve_"):
        request_id = int(data.replace("req_approve_", ""))
        await process_song_request(query, context, request_id, 'approved')
        
    elif data.startswith("req_reject_"):
        request_id = int(data.replace("req_reject_", ""))
        await process_song_request(query, context, request_id, 'rejected')
        
    elif data.startswith("req_search_"):
        request_id = int(data.replace("req_search_", ""))
        # 获取申请信息并搜索
        try:
            cursor = database_conn.cursor()
            cursor.execute('SELECT * FROM song_requests WHERE id = ?', (request_id,))
            row = cursor.fetchone()
            if row:
                song_name = row['song_name'] if isinstance(row, dict) else row[2]
                artist = row['artist'] if isinstance(row, dict) else row[3]
                search_query = f"{song_name} {artist}" if artist else song_name
                
                # 触发搜索
                context.args = [search_query]
                await cmd_search(update, context)
        except Exception as e:
            await query.message.reply_text(f"❌ 搜索失败: {e}")


async def process_song_request(query, context, request_id: int, status: str):
    """处理歌曲申请（批准/拒绝）"""
    try:
        cursor = database_conn.cursor()
        
        # 获取申请信息
        cursor.execute('SELECT * FROM song_requests WHERE id = ?', (request_id,))
        row = cursor.fetchone()
        if not row:
            await query.edit_message_text("❌ 申请不存在")
            return
        
        telegram_id = row['telegram_id'] if isinstance(row, dict) else row[1]
        song_name = row['song_name'] if isinstance(row, dict) else row[2]
        artist = row['artist'] if isinstance(row, dict) else row[3]
        
        # 更新状态
        from datetime import datetime
        cursor.execute('''
            UPDATE song_requests 
            SET status = ?, processed_at = ? 
            WHERE id = ?
        ''', (status, datetime.now().isoformat(), request_id))
        database_conn.commit()
        
        status_text = "✅ 已批准" if status == 'approved' else "❌ 已拒绝"
        await query.edit_message_text(
            query.message.text + f"\n\n{status_text}",
            parse_mode='Markdown'
        )
        
        # 通知用户
        try:
            user_msg = f"📝 你的歌曲申请已处理\n\n🎵 {song_name}"
            if artist:
                user_msg += f" - {artist}"
            user_msg += f"\n\n状态: {status_text}"
            
            await context.bot.send_message(
                chat_id=telegram_id,
                text=user_msg
            )
        except Exception as e:
            logger.error(f"通知用户失败: {e}")
            
    except Exception as e:
        logger.error(f"处理申请失败: {e}")
        await query.message.reply_text(f"❌ 处理失败: {e}")


async def handle_search_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理搜索结果下载回调"""
    query = update.callback_query
    await query.answer()
    
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
        
        download_path = Path(download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        
        downloader = MusicAutoDownloader(ncm_cookie, str(download_path))
        
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
        
        await query.edit_message_text(f"🔄 正在下载 {len(songs_to_download)} 首歌曲...")
        
        # 进度消息
        progress_msg = await query.message.reply_text(f"📥 正在下载 0/{len(songs_to_download)}...")
        last_update_time = [0]
        main_loop = asyncio.get_running_loop()
        
        async def update_progress(current, total, song):
            import time as time_module
            now = time_module.time()
            if now - last_update_time[0] < 2:
                return
            last_update_time[0] = now
            try:
                await progress_msg.edit_text(
                    f"📥 正在下载 {current}/{total}\n"
                    f"🎵 `{song.get('title', '')} - {song.get('artist', '')}`",
                    parse_mode='Markdown'
                )
            except:
                pass
        
        def sync_progress_callback(current, total, song):
            main_loop.call_soon_threadsafe(
                lambda: asyncio.run_coroutine_threadsafe(update_progress(current, total, song), main_loop)
            )
        
        # 开始下载
        success_files, failed_songs = await asyncio.to_thread(
            downloader.download_missing_songs,
            songs_to_download,
            download_quality,
            sync_progress_callback
        )
        
        # MusicTag 模式移动文件
        moved_files = []
        if download_mode == 'musictag' and musictag_dir and success_files:
            musictag_path = Path(musictag_dir)
            musictag_path.mkdir(parents=True, exist_ok=True)
            for file_path in success_files:
                try:
                    src = Path(file_path)
                    dst = musictag_path / src.name
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                except:
                    pass
        
        # 删除进度消息
        try:
            await progress_msg.delete()
        except:
            pass
        
        msg = f"📥 **下载完成**\n\n"
        msg += f"✅ 成功: {len(success_files)} 首\n"
        msg += f"❌ 失败: {len(failed_songs)} 首\n"
        
        if success_files:
            if moved_files:
                msg += f"\n📁 已转移到 MusicTag 目录"
            else:
                msg += f"\n📁 已保存到: `{download_dir}`"
        
        await query.message.reply_text(msg, parse_mode='Markdown')
        
        # 自动扫库
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
                            await query.message.reply_text("🔄 已自动触发 Emby 扫库")
                except:
                    pass
        
    except Exception as e:
        logger.exception(f"下载失败: {e}")
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


# ============================================================
# 消息处理
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # 处理音频/文档上传
    if message.audio or message.document:
        handled = await handle_audio_upload(update, context)
        if handled:
            return
    
    # 处理文本消息（歌单链接）
    if message.text:
        handled = await handle_playlist_url(update, context)
        if handled:
            return


# ============================================================
# 主程序
# ============================================================

def main():
    global requests_session
    
    if not TELEGRAM_TOKEN:
        logger.critical("缺少 TELEGRAM_TOKEN！")
        return
    if not EMBY_URL:
        logger.critical("缺少 EMBY_URL！")
        return
    
    requests_session = create_requests_session()
    init_database()
    
    # Emby 认证
    if EMBY_USERNAME and EMBY_PASSWORD:
        token, user_id = authenticate_emby(EMBY_URL, EMBY_USERNAME, EMBY_PASSWORD)
        if token:
            emby_auth['access_token'] = token
            emby_auth['user_id'] = user_id
    
    # 加载媒体库缓存
    global emby_library_data
    if LIBRARY_CACHE_FILE.exists():
        try:
            with open(LIBRARY_CACHE_FILE, 'r', encoding='utf-8') as f:
                emby_library_data = json.load(f)
            logger.info(f"从缓存加载 {len(emby_library_data)} 首歌曲")
        except:
            if emby_auth['access_token']:
                scan_emby_library(True, emby_auth['user_id'], emby_auth['access_token'])
    else:
        if emby_auth['access_token']:
            scan_emby_library(True, emby_auth['user_id'], emby_auth['access_token'])
    
    # 启动 Bot
    builder = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30)
    
    # 如果配置了 Local Bot API Server
    if TELEGRAM_API_URL:
        builder = builder.base_url(TELEGRAM_API_URL).base_file_url(TELEGRAM_API_URL.replace('/bot', '/file/bot'))
        logger.info(f"使用 Local Bot API Server: {TELEGRAM_API_URL}")
    
    app = builder.build()
    
    # 命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("bind", cmd_bind))
    app.add_handler(CommandHandler("unbind", cmd_unbind))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rescan", cmd_rescan))
    app.add_handler(CommandHandler("ncmstatus", cmd_ncm_status))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("album", cmd_album))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("scaninterval", cmd_scaninterval))
    app.add_handler(CommandHandler("request", cmd_request))
    app.add_handler(CommandHandler("myrequests", cmd_myrequests))
    
    # 回调
    app.add_handler(CallbackQueryHandler(handle_match_callback, pattern='^match_'))
    app.add_handler(CallbackQueryHandler(handle_download_callback, pattern='^download_'))
    app.add_handler(CallbackQueryHandler(handle_search_download_callback, pattern='^dl_'))
    app.add_handler(CallbackQueryHandler(handle_sync_callback, pattern='^sync_'))
    app.add_handler(CallbackQueryHandler(handle_request_callback, pattern='^req_'))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern='^menu_'))
    
    # 消息
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    logger.info("Bot 启动成功！")
    ncm_cookie = get_ncm_cookie()
    if ncm_cookie:
        logger.info("已配置网易云 Cookie，自动下载功能已启用")
    
    # 启动定时同步任务 & 注册命令菜单
    async def post_init(application):
        # 注册命令菜单（用户输入 / 时显示）
        from telegram import BotCommand
        commands = [
            BotCommand("start", "主菜单"),
            BotCommand("help", "使用帮助"),
            BotCommand("bind", "绑定 Emby 账户"),
            BotCommand("unbind", "解除绑定"),
            BotCommand("status", "查看状态"),
            BotCommand("search", "搜索并下载歌曲"),
            BotCommand("album", "搜索并下载专辑"),
            BotCommand("request", "申请补全歌曲"),
            BotCommand("myrequests", "查看我的申请"),
            BotCommand("schedule", "查看订阅歌单"),
            BotCommand("unschedule", "取消订阅歌单"),
            BotCommand("scaninterval", "设置媒体库扫描间隔"),
            BotCommand("rescan", "重新扫描 Emby 库"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("已注册 Telegram 命令菜单")
        
        # 启动定时同步任务
        asyncio.create_task(scheduled_sync_job(application))
        logger.info("定时同步任务已启动 (每6小时检查一次)")
        
        # 启动定时扫描 Emby 媒体库任务
        asyncio.create_task(scheduled_emby_scan_job(application))
        scan_interval = EMBY_SCAN_INTERVAL
        try:
            if database_conn:
                cursor = database_conn.cursor()
                cursor.execute('SELECT value FROM bot_settings WHERE key = ?', ('emby_scan_interval',))
                row = cursor.fetchone()
                if row:
                    scan_interval = int(row[0] if isinstance(row, tuple) else row['value'])
        except:
            pass
        if scan_interval > 0:
            logger.info(f"Emby 媒体库自动扫描已启动 (每 {scan_interval} 小时)")
        else:
            logger.info("Emby 媒体库自动扫描未启用")
    
    app.post_init = post_init
    
    # 如果配置了 Pyrogram，启动大文件接收功能
    if TG_API_ID and TG_API_HASH:
        asyncio.get_event_loop().run_until_complete(start_pyrogram_client())
    
    app.run_polling()


if __name__ == '__main__':
    main()
