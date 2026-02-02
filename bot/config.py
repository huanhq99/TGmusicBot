#!/usr/bin/env python3
"""
TGmusicbot - 配置模块
集中管理所有配置项和全局变量
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# --- 应用信息 ---
APP_NAME = "TGmusicbot"
APP_VERSION = "1.11.0"  # 重构后版本
EMBY_CLIENT_NAME = "TGmusicbot"
DEVICE_ID = "TGmusicbot_Device_v2"

# --- 路径配置 ---
SCRIPT_DIR = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/tmp/tgmusicbot_uploads'))
MUSIC_TARGET_DIR = Path(os.environ.get('MUSIC_TARGET_DIR', SCRIPT_DIR / 'uploads'))

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_TARGET_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_FILE = DATA_DIR / 'bot.db'
LIBRARY_CACHE_FILE = DATA_DIR / 'library_cache.json'
LOG_FILE = DATA_DIR / f'bot_{datetime.now().strftime("%Y%m%d")}.log'

# --- Telegram 配置 ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_API_URL = os.environ.get('TELEGRAM_API_URL', '')  # Local Bot API Server URL
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')

# Pyrogram 配置（大文件上传支持）
TG_API_ID = os.environ.get('TG_API_ID', '')
TG_API_HASH = os.environ.get('TG_API_HASH', '')

# --- Emby 配置 ---
EMBY_URL = os.environ.get('EMBY_URL')
EMBY_USERNAME = os.environ.get('EMBY_USERNAME')
EMBY_PASSWORD = os.environ.get('EMBY_PASSWORD')
EMBY_WEBHOOK_NOTIFY = os.environ.get('EMBY_WEBHOOK_NOTIFY', 'true').lower() == 'true'
MAKE_PLAYLIST_PUBLIC = os.environ.get('MAKE_PLAYLIST_PUBLIC', 'false').lower() == 'true'
EMBY_SCAN_INTERVAL = int(os.environ.get('EMBY_SCAN_INTERVAL', '0'))

# Emby API 参数
EMBY_SCAN_PAGE_SIZE = 2000
EMBY_PLAYLIST_ADD_BATCH_SIZE = 200

# --- 音乐下载配置 ---
NCM_COOKIE = os.environ.get('NCM_COOKIE', '')
QQ_COOKIE = os.environ.get('QQ_COOKIE', '')
NCM_QUALITY = os.environ.get('NCM_QUALITY', 'exhigh')  # standard/higher/exhigh/lossless/hires
AUTO_DOWNLOAD = os.environ.get('AUTO_DOWNLOAD', 'false').lower() == 'true'

# Daily Ranking Config
DAILY_RANKING_TITLE = os.environ.get('DAILY_RANKING_TITLE', '每日音乐热曲榜')
DAILY_RANKING_SUBTITLE = os.environ.get('DAILY_RANKING_SUBTITLE', 'TGmusicbot')

# 国内代理服务配置
MUSIC_PROXY_URL = os.environ.get('MUSIC_PROXY_URL', '')
MUSIC_PROXY_KEY = os.environ.get('MUSIC_PROXY_KEY', '')

# 允许上传的音频格式
ALLOWED_AUDIO_EXTENSIONS = (
    '.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', 
    '.ape', '.wma', '.alac', '.aiff', '.dsd', '.dsf', '.dff'
)

# --- 加密配置 ---
ENCRYPTION_KEY = os.environ.get('PLAYLIST_BOT_KEY')
if not ENCRYPTION_KEY:
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    print(f"警告：未设置 PLAYLIST_BOT_KEY，已生成新密钥：{ENCRYPTION_KEY}")

fernet = Fernet(ENCRYPTION_KEY.encode())

# --- API 端点 ---
QQ_API_GET_PLAYLIST_URL = "http://i.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
NCM_API_PLAYLIST_DETAIL_URL = "https://music.163.com/api/v3/playlist/detail"
NCM_API_SONG_DETAIL_URL = "https://music.163.com/api/song/detail/"

# --- 匹配参数 ---
MATCH_THRESHOLD = 9

# --- 歌单同步调度配置 ---
DEFAULT_PLAYLIST_SYNC_INTERVAL_MINUTES = max(
    1,
    int(os.environ.get('PLAYLIST_SYNC_INTERVAL', 
        os.environ.get('PLAYLIST_SYNC_INTERVAL_MINUTES', '360')))
)
MIN_PLAYLIST_SYNC_INTERVAL_MINUTES = max(1, int(os.environ.get('PLAYLIST_SYNC_MIN_INTERVAL', '1')))
PLAYLIST_SYNC_POLL_INTERVAL_SECONDS = max(30, int(os.environ.get('PLAYLIST_SYNC_POLL_INTERVAL', '60')))
PLAYLIST_SYNC_INITIAL_DELAY_SECONDS = max(0, int(os.environ.get('PLAYLIST_SYNC_INITIAL_DELAY', '10')))

# --- 搜索缓存配置 ---
SEARCH_CACHE_TTL = 180  # 3分钟

# --- Web 配置 ---
WEB_USERNAME = os.environ.get('WEB_USERNAME', 'admin')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '')


def setup_logging():
    """配置日志"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger('TGmusicbot')

logger = setup_logging()
