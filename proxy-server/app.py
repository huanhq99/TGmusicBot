#!/usr/bin/env python3
"""
音乐下载代理服务 v1.2.0 - 部署在国内服务器
海外 VPS 通过此代理下载 QQ音乐/网易云音乐

使用方法:
1. 部署到国内服务器
2. 在海外 VPS 的 .env 中配置 MUSIC_PROXY_URL=http://国内IP:8899

功能:
- QQ音乐/网易云下载代理
- 请求缓存（避免重复获取 vkey）
- 限流保护（防止被封禁）
"""

import os
import io
import json
import logging
import hashlib
import time
import threading
from pathlib import Path
from collections import OrderedDict
from flask import Flask, request, jsonify, send_file, Response
import requests

app = Flask(__name__)

# ============================================================
# 缓存管理
# ============================================================

class LRUCache:
    """简单的 LRU 缓存，带过期时间"""
    
    def __init__(self, max_size: int = 100, ttl: int = 300):
        self.max_size = max_size
        self.ttl = ttl  # 缓存有效期（秒）
        self.cache = OrderedDict()
        self.lock = threading.Lock()
    
    def get(self, key: str):
        with self.lock:
            if key not in self.cache:
                return None
            value, timestamp = self.cache[key]
            if time.time() - timestamp > self.ttl:
                del self.cache[key]
                return None
            # 移到最后（最近使用）
            self.cache.move_to_end(key)
            return value
    
    def set(self, key: str, value):
        with self.lock:
            if key in self.cache:
                del self.cache[key]
            elif len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
            self.cache[key] = (value, time.time())
    
    def clear(self):
        with self.lock:
            self.cache.clear()


class RateLimiter:
    """简单的限流器"""
    
    def __init__(self, max_requests: int = 30, window: int = 60):
        self.max_requests = max_requests
        self.window = window  # 时间窗口（秒）
        self.requests = []  # (timestamp,)
        self.lock = threading.Lock()
    
    def is_allowed(self) -> bool:
        with self.lock:
            now = time.time()
            # 清理过期请求
            self.requests = [t for t in self.requests if now - t < self.window]
            
            if len(self.requests) >= self.max_requests:
                return False
            
            self.requests.append(now)
            return True
    
    def get_wait_time(self) -> float:
        """获取需要等待的时间"""
        with self.lock:
            if not self.requests:
                return 0
            oldest = min(self.requests)
            wait = self.window - (time.time() - oldest)
            return max(0, wait)


# 全局缓存和限流器
vkey_cache = LRUCache(max_size=200, ttl=600)  # vkey 缓存 10 分钟
qq_rate_limiter = RateLimiter(max_requests=60, window=60)  # 每分钟最多 60 次请求

# 配置 - 只需要 API Key，Cookie 从请求头传递
API_KEY = os.environ.get('PROXY_API_KEY', 'change_me_to_secure_key')

# 日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Session - 模拟浏览器
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Origin': 'https://y.qq.com',
    'Referer': 'https://y.qq.com/',
})


def verify_api_key():
    """验证 API Key"""
    key = request.headers.get('X-API-Key') or request.args.get('key')
    if key != API_KEY:
        return False
    return True


def get_cookie_from_request(cookie_type: str) -> str:
    """从请求头获取 Cookie"""
    if cookie_type == 'qq':
        return request.headers.get('X-QQ-Cookie', '')
    elif cookie_type == 'ncm':
        return request.headers.get('X-NCM-Cookie', '')
    return ''


# ============================================================
# QQ 音乐 API - 基于 jsososo/QQMusicApi 的实现
# ============================================================

def parse_qq_cookie(qq_cookie: str) -> dict:
    """解析 QQ 音乐 Cookie，提取 uin 和 qqmusic_key"""
    uin = '0'
    qqmusic_key = ''
    wxuin = ''
    
    for item in qq_cookie.split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            continue
        key, value = item.split('=', 1)
        key = key.strip()
        value = value.strip()
        
        if key == 'uin':
            # uin 可能是 o1234567890 格式，去掉前缀 o
            uin = value.lstrip('o')
        elif key == 'wxuin':
            # 微信登录的 uin
            wxuin = value
        elif key in ('qm_keyst', 'qqmusic_key'):
            # qm_keyst 或 qqmusic_key 都是登录凭证
            qqmusic_key = value
    
    # 如果 uin 是 0 但有 wxuin，使用 wxuin
    if uin == '0' and wxuin:
        uin = wxuin
        logger.info(f"使用 wxuin 作为 uin: {uin}")
    
    logger.info(f"解析Cookie: uin={uin}, qqmusic_key长度={len(qqmusic_key)}")
    return {'uin': uin, 'qqmusic_key': qqmusic_key}


def get_qq_vkey(song_mid: str, qq_cookie: str, quality: str = 'exhigh') -> dict:
    """
    获取 QQ 音乐下载链接 - 基于 jsososo/QQMusicApi 的实现
    
    关键点:
    1. 文件名格式: {prefix}{songmid}{songmid}{ext} (mediaId 默认等于 songmid)
    2. comm 参数中需要 authst: qqmusic_key
    3. ct: 19 (不是 24)
    4. 需要重试机制
    5. 使用缓存避免重复请求
    6. 限流保护防止被封禁
    7. 自动音质降级（当高音质不可用时尝试低音质）
    """
    # 检查缓存
    cache_key = f"{song_mid}:{quality}"
    cached = vkey_cache.get(cache_key)
    if cached:
        logger.info(f"缓存命中: {song_mid} ({quality})")
        return cached
    
    # 检查限流
    if not qq_rate_limiter.is_allowed():
        wait_time = qq_rate_limiter.get_wait_time()
        logger.warning(f"请求过于频繁，需等待 {wait_time:.1f} 秒")
        return {'success': False, 'error': f'Rate limited, wait {wait_time:.1f}s'}
    
    # 音质映射 - 与 jsososo 一致
    quality_map = {
        'standard': {'prefix': 'M500', 'ext': '.mp3', 'type': '128'},
        'higher': {'prefix': 'M800', 'ext': '.mp3', 'type': '320'},
        'exhigh': {'prefix': 'M800', 'ext': '.mp3', 'type': '320'},
        'lossless': {'prefix': 'F000', 'ext': '.flac', 'type': 'flac'},
        'hires': {'prefix': 'RS01', 'ext': '.flac', 'type': 'hires'},  # Hi-Res 使用 RS01 前缀
        'm4a': {'prefix': 'C400', 'ext': '.m4a', 'type': 'm4a'},
        'ape': {'prefix': 'A000', 'ext': '.ape', 'type': 'ape'},
    }
    
    # 音质降级顺序
    quality_fallback_order = ['hires', 'lossless', 'exhigh', 'higher', 'standard']
    try:
        start_idx = quality_fallback_order.index(quality)
    except ValueError:
        start_idx = 2  # 默认从 exhigh 开始
    qualities_to_try = quality_fallback_order[start_idx:]
    
    # 解析 Cookie
    cookie_data = parse_qq_cookie(qq_cookie)
    uin = cookie_data['uin']
    qqmusic_key = cookie_data['qqmusic_key']
    
    if not qqmusic_key:
        logger.error("Cookie 中没有找到 qm_keyst 或 qqmusic_key")
        return {'success': False, 'error': 'Missing qqmusic_key in cookie'}
    
    # 生成 guid
    import random
    guid = str(int(random.random() * 10000000))
    
    # 设置 Cookie 到 session
    session.cookies.clear()
    for item in qq_cookie.split(';'):
        if '=' in item:
            key, value = item.strip().split('=', 1)
            session.cookies.set(key.strip(), value.strip(), domain='.qq.com')
    
    # 尝试不同音质
    for try_quality in qualities_to_try:
        q = quality_map.get(try_quality, quality_map['exhigh'])
        
        # 文件名格式: {prefix}{songmid}{mediaId}{ext}
        # mediaId 默认与 songmid 相同
        filename = f"{q['prefix']}{song_mid}{song_mid}{q['ext']}"
        
        logger.info(f"尝试获取 {song_mid} 音质={try_quality}, 文件名={filename}")
        
        # 重试获取 purl（每个音质最多 3 次）
        purl = ''
        domain = ''
        max_retries = 2  # 减少重试次数，因为我们会尝试多种 API
        
        # 尝试不同的 API 方法
        api_methods = [
            ('GET', _request_vkey),
            ('POST', _request_vkey_post),
        ]
        
        for api_name, api_func in api_methods:
            if purl:  # 如果已经获取到 purl，跳出循环
                break
                
            for attempt in range(max_retries):
                try:
                    result = api_func(song_mid, filename, guid, uin, qqmusic_key)
                    
                    if not result:
                        logger.warning(f"尝试 {api_name} {attempt + 1}/{max_retries} ({try_quality}): API 请求失败")
                        continue
                    
                    req_data = result.get('req_0', {}).get('data', {})
                    
                    if not req_data:
                        logger.warning(f"尝试 {api_name} {attempt + 1}/{max_retries} ({try_quality}): req_0.data 为空")
                        continue
                    
                    # 获取 sip (CDN 域名)
                    sip_list = req_data.get('sip', [])
                    if sip_list and not domain:
                        # 优先选择非 ws 开头的域名
                        domain = next((s for s in sip_list if not s.startswith('http://ws')), sip_list[0])
                    
                    # 获取 purl
                    midurlinfo = req_data.get('midurlinfo', [])
                    if midurlinfo:
                        info = midurlinfo[0]
                        purl = info.get('purl', '')
                        result_code = info.get('result', 0)
                        
                        # 检查 result 错误码
                        if result_code != 0:
                            error_msg = {
                                104003: 'Cookie无效或已过期，请重新获取Cookie',
                                104001: '歌曲不存在',
                                104002: '需要VIP权限',
                                104004: '地区版权限制',
                                104005: '请求过于频繁',
                            }.get(result_code, f'未知错误({result_code})')
                            logger.warning(f"QQ音乐返回错误: result={result_code}, 含义: {error_msg}")
                        
                        if purl:
                            logger.info(f"成功获取 purl ({api_name}, {try_quality}), 长度={len(purl)}")
                            break
                        else:
                            errtype = info.get('errtype', '')
                            # 详细记录失败信息
                            logger.warning(f"尝试 {api_name} {attempt + 1}/{max_retries} ({try_quality}): purl 为空, "
                                         f"result={result_code}, errtype={errtype}, "
                                         f"songmid={info.get('songmid', '')}")
                    else:
                        logger.warning(f"尝试 {api_name} {attempt + 1}/{max_retries} ({try_quality}): midurlinfo 为空")
                    
                except Exception as e:
                    logger.error(f"尝试 {api_name} {attempt + 1}/{max_retries} ({try_quality}) 异常: {e}")
        
        # 如果这个音质获取到了 purl，返回结果
        if purl:
            if not domain:
                domain = 'https://isure.stream.qqmusic.qq.com/'
            
            download_url = f"{domain}{purl}"
            logger.info(f"最终下载链接 ({try_quality}): {download_url[:100]}...")
            
            result = {
                'success': True,
                'url': download_url,
                'type': q['ext'].lstrip('.'),
                'quality': try_quality
            }
            
            # 缓存成功结果
            vkey_cache.set(cache_key, result)
            
            return result
        else:
            if try_quality != qualities_to_try[-1]:
                logger.info(f"音质 {try_quality} 不可用，尝试降级...")
    
    logger.error(f"所有音质都无法获取 purl: {song_mid}")
    logger.error(f"  └─ result=104003 表示: Cookie无效或已过期，请重新登录QQ音乐网页版获取新Cookie")
    logger.error(f"  └─ 请使用 /qq/diagnose 接口检查 Cookie 状态")
    return {
        'success': False, 
        'error': 'Cookie无效或已过期(result=104003)',
        'hint': '请重新登录 y.qq.com 获取新的Cookie，确保包含 qm_keyst 或 qqmusic_key'
    }


def _request_vkey(song_mid: str, filename: str, guid: str, uin: str, qqmusic_key: str) -> dict:
    """
    请求 QQ 音乐 vkey API
    
    关键参数:
    - authst: qqmusic_key (Cookie 中的 qm_keyst)
    - ct: 19 (客户端类型)
    - cv: 0
    """
    url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
    
    # 构建请求数据 - 严格按照 jsososo 的格式
    request_data = {
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'filename': [filename],
                'guid': guid,
                'songmid': [song_mid],
                'songtype': [0],
                'uin': uin,
                'loginflag': 1,
                'platform': '20',
            }
        },
        'comm': {
            'uin': uin,
            'format': 'json',
            'ct': 19,
            'cv': 0,
            'authst': qqmusic_key,
        }
    }
    
    # 使用 GET 请求（与 jsososo 一致）
    params = {
        '-': 'getplaysongvkey',
        'g_tk': 5381,
        'loginUin': uin,
        'hostUin': 0,
        'format': 'json',
        'inCharset': 'utf8',
        'outCharset': 'utf-8',
        'notice': 0,
        'platform': 'yqq.json',
        'needNewCode': 0,
        'data': json.dumps(request_data, separators=(',', ':'))
    }
    
    try:
        resp = session.get(url, params=params, timeout=15)
        result = resp.json()
        
        code = result.get('code', -1)
        req_code = result.get('req_0', {}).get('code', -1)
        logger.info(f"API响应: code={code}, req_0.code={req_code}")
        
        # 详细调试信息 - 改为 INFO 级别以便追踪问题
        req_data = result.get('req_0', {}).get('data', {})
        midurlinfo = req_data.get('midurlinfo', [])
        if midurlinfo:
            info = midurlinfo[0]
            purl_preview = info.get('purl', '')[:50] if info.get('purl') else '空'
            vkey_preview = info.get('vkey', '')[:20] if info.get('vkey') else '空'
            wifiurl_preview = info.get('wifiurl', '')[:50] if info.get('wifiurl') else '空'
            logger.info(f"  └─ midurlinfo: purl={purl_preview}, errtype={info.get('errtype', '')}, vkey={vkey_preview}")
            # 如果 purl 为空，打印完整的 midurlinfo 用于调试
            if not info.get('purl'):
                logger.info(f"  └─ 完整 midurlinfo: {json.dumps(info, ensure_ascii=False)[:500]}")
        else:
            logger.warning(f"  └─ midurlinfo 列表为空")
        
        return result
        
    except Exception as e:
        logger.error(f"请求 vkey API 失败: {e}")
        return None


def _request_vkey_v2(song_mid: str, filename: str, guid: str, uin: str, qqmusic_key: str) -> dict:
    """
    请求 QQ 音乐 vkey API - 备用方案 v2
    使用不同的 API 端点和参数
    """
    url = 'https://u.y.qq.com/cgi-bin/musics.fcg'
    
    # 尝试使用 musicUniformGetUrl 接口
    request_data = {
        'req_1': {
            'module': 'music.vkey.GetEDownUrl',
            'method': 'CgiGetEDownUrl',
            'param': {
                'uin': uin,
                'songmid': [song_mid],
                'songtype': [0],
                'sip': [],
                'guid': guid,
            }
        },
        'comm': {
            'uin': uin,
            'format': 'json',
            'ct': 24,
            'cv': 0,
            'authst': qqmusic_key,
            'tmeLoginType': 2,
        }
    }
    
    params = {
        '-': 'getEdownUrl',
        'format': 'json',
        'data': json.dumps(request_data, separators=(',', ':'))
    }
    
    try:
        resp = session.get(url, params=params, timeout=15)
        result = resp.json()
        
        code = result.get('code', -1)
        req_code = result.get('req_1', {}).get('code', -1)
        logger.info(f"API v2 响应: code={code}, req_1.code={req_code}")
        
        return result
        
    except Exception as e:
        logger.error(f"请求 vkey API v2 失败: {e}")
        return None


def _request_vkey_post(song_mid: str, filename: str, guid: str, uin: str, qqmusic_key: str) -> dict:
    """
    请求 QQ 音乐 vkey API - 使用 POST 请求
    有时候 QQ 音乐需要 POST 请求才能正常返回
    """
    url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
    
    request_data = {
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'filename': [filename],
                'guid': guid,
                'songmid': [song_mid],
                'songtype': [0],
                'uin': uin,
                'loginflag': 1,
                'platform': '20',
            }
        },
        'comm': {
            'uin': uin,
            'format': 'json',
            'ct': 24,  # 使用 24 而不是 19
            'cv': 0,
            'authst': qqmusic_key,
            'tmeLoginType': 2,
        }
    }
    
    try:
        # 使用 POST 请求
        resp = session.post(
            url, 
            json={'data': json.dumps(request_data, separators=(',', ':'))},
            timeout=15
        )
        result = resp.json()
        
        code = result.get('code', -1)
        req_code = result.get('req_0', {}).get('code', -1)
        logger.info(f"POST API响应: code={code}, req_0.code={req_code}")
        
        return result
        
    except Exception as e:
        logger.error(f"请求 POST vkey API 失败: {e}")
        return None


def _get_vkey_old_api(song_mid: str, filename: str, guid: str, uin: str, q: dict) -> dict:
    """老 API - 备用方案，使用 fcg_music_express_mobile3"""
    url = "https://c.y.qq.com/base/fcgi-bin/fcg_music_express_mobile3.fcg"
    params = {
        "format": "json",
        "platform": "yqq",
        "cid": "205361747",
        "songmid": song_mid,
        "filename": filename,
        "guid": guid
    }
    
    try:
        resp = session.get(url, params=params, timeout=10)
        result = resp.json()
        
        vkey = result.get('data', {}).get('items', [{}])[0].get('vkey', '')
        if vkey:
            download_url = f"http://dl.stream.qqmusic.qq.com/{filename}?vkey={vkey}&guid={guid}&uin={uin}&fromtag=66"
            logger.info(f"老API获取链接: {download_url[:80]}...")
            return {
                'success': True,
                'url': download_url,
                'type': q['ext'].lstrip('.'),
                'quality': q.get('quality', 'exhigh')
            }
        
        logger.warning(f"老API未获取到 vkey")
        return {'success': False}
        
    except Exception as e:
        logger.error(f"老API失败: {e}")
        return {'success': False}


def download_qq_song(song_mid: str, qq_cookie: str, quality: str = 'exhigh') -> tuple:
    """下载 QQ 音乐并返回文件内容"""
    # 音质降级列表
    quality_fallback = {
        'hires': ['hires', 'lossless', 'exhigh', 'standard'],
        'lossless': ['lossless', 'exhigh', 'standard'],
        'exhigh': ['exhigh', 'standard'],
        'standard': ['standard'],
    }
    
    fallback_list = quality_fallback.get(quality, ['exhigh', 'standard'])
    
    for try_quality in fallback_list:
        result = get_qq_vkey(song_mid, qq_cookie, try_quality)
        if not result.get('success'):
            continue
        
        download_url = result['url']
        file_type = result['type']
        
        try:
            headers = {
                'Referer': 'https://y.qq.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Cookie': qq_cookie,
            }
            resp = session.get(download_url, headers=headers, timeout=60, stream=True)
            
            if resp.status_code == 200:
                content_length = int(resp.headers.get('Content-Length', 0))
                if content_length > 100000:  # 大于 100KB 才算有效
                    logger.info(f"下载成功: {song_mid}.{file_type} ({content_length} bytes)")
                    return resp.content, file_type, try_quality
                else:
                    logger.warning(f"文件太小 {try_quality}: {content_length} bytes")
            else:
                error_code = resp.headers.get('Error', 'unknown')
                logger.warning(f"下载失败 {try_quality}: HTTP {resp.status_code}, Error: {error_code}")
            
        except Exception as e:
            logger.error(f"下载失败 {try_quality}: {e}")
    
    return None, None, None


# ============================================================
# 网易云音乐 API
# ============================================================

def get_ncm_url(song_id: str, ncm_cookie: str, quality: str = 'exhigh') -> dict:
    """获取网易云音乐下载链接"""
    quality_map = {
        'standard': 128000,
        'higher': 192000,
        'exhigh': 320000,
        'lossless': 999000,
        'hires': 999000,
    }
    br = quality_map.get(quality, 320000)
    
    url = 'https://interface3.music.163.com/eapi/song/enhance/player/url'
    
    # 简化的加密（实际可能需要更复杂的加密）
    params = {
        'ids': [int(song_id)],
        'br': br,
    }
    
    headers = {
        'Referer': 'https://music.163.com/',
        'Cookie': ncm_cookie,
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    
    try:
        # 使用简单的 API
        simple_url = f'https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br={br}'
        headers_simple = {
            'Referer': 'https://music.163.com/',
            'Cookie': ncm_cookie,
        }
        resp = session.get(simple_url, headers=headers_simple, timeout=15)
        result = resp.json()
        
        if result.get('code') == 200 and result.get('data'):
            data = result['data'][0]
            if data.get('url'):
                return {
                    'success': True,
                    'url': data['url'],
                    'type': data.get('type', 'mp3'),
                    'quality': quality
                }
        
        return {'success': False, 'error': 'No download URL'}
        
    except Exception as e:
        logger.error(f"获取 NCM URL 失败: {e}")
        return {'success': False, 'error': str(e)}


def download_ncm_song(song_id: str, ncm_cookie: str, quality: str = 'exhigh') -> tuple:
    """下载网易云音乐并返回文件内容"""
    quality_fallback = {
        'hires': ['hires', 'lossless', 'exhigh', 'standard'],
        'lossless': ['lossless', 'exhigh', 'standard'],
        'exhigh': ['exhigh', 'standard'],
        'standard': ['standard'],
    }
    
    fallback_list = quality_fallback.get(quality, ['exhigh', 'standard'])
    
    for try_quality in fallback_list:
        result = get_ncm_url(song_id, ncm_cookie, try_quality)
        if not result.get('success'):
            continue
        
        download_url = result['url']
        file_type = result['type']
        
        try:
            resp = session.get(download_url, timeout=60, stream=True)
            
            if resp.status_code == 200:
                content_length = int(resp.headers.get('Content-Length', 0))
                if content_length > 100000:
                    logger.info(f"下载成功: {song_id}.{file_type} ({content_length} bytes)")
                    return resp.content, file_type, try_quality
            
        except Exception as e:
            logger.error(f"NCM 下载失败 {try_quality}: {e}")
    
    return None, None, None


# ============================================================
# API 路由
# ============================================================

@app.route('/health')
def health():
    """健康检查"""
    return jsonify({
        'status': 'ok',
        'message': 'Cookie 由请求头传递',
    })


@app.route('/qq/diagnose')
def qq_diagnose():
    """诊断 QQ 音乐 Cookie 和账号状态"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    qq_cookie = get_cookie_from_request('qq')
    if not qq_cookie:
        return jsonify({'error': 'Missing X-QQ-Cookie header'}), 400
    
    # 解析 Cookie
    cookie_data = parse_qq_cookie(qq_cookie)
    uin = cookie_data['uin']
    qqmusic_key = cookie_data['qqmusic_key']
    
    result = {
        'cookie_valid': bool(qqmusic_key),
        'uin': uin,
        'qqmusic_key_length': len(qqmusic_key),
        'qqmusic_key_preview': qqmusic_key[:20] + '...' if len(qqmusic_key) > 20 else qqmusic_key,
    }
    
    # 尝试获取用户信息
    try:
        # 设置 Cookie 到 session
        session.cookies.clear()
        for item in qq_cookie.split(';'):
            if '=' in item:
                key, value = item.strip().split('=', 1)
                session.cookies.set(key.strip(), value.strip(), domain='.qq.com')
        
        # 请求用户信息
        user_url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
        user_data = {
            'req_0': {
                'module': 'userInfo.BaseUserInfoServer',
                'method': 'get_user_baseinfo_v2',
                'param': {
                    'vec_uin': [uin]
                }
            },
            'comm': {
                'uin': uin,
                'format': 'json',
                'ct': 19,
                'cv': 0,
                'authst': qqmusic_key,
            }
        }
        
        params = {
            'format': 'json',
            'data': json.dumps(user_data, separators=(',', ':'))
        }
        
        resp = session.get(user_url, params=params, timeout=10)
        user_result = resp.json()
        
        result['user_api_code'] = user_result.get('code', -1)
        result['user_api_req0_code'] = user_result.get('req_0', {}).get('code', -1)
        
        # 检查 VIP 状态
        vip_data = {
            'req_0': {
                'module': 'music.vip.VipCenter',
                'method': 'GetVipInfo',
                'param': {
                    'uin': uin
                }
            },
            'comm': {
                'uin': uin,
                'format': 'json',
                'ct': 19,
                'cv': 0,
                'authst': qqmusic_key,
            }
        }
        
        params = {
            'format': 'json',
            'data': json.dumps(vip_data, separators=(',', ':'))
        }
        
        resp = session.get(user_url, params=params, timeout=10)
        vip_result = resp.json()
        
        result['vip_api_code'] = vip_result.get('code', -1)
        vip_info = vip_result.get('req_0', {}).get('data', {})
        result['is_vip'] = vip_info.get('is_vip', 0) == 1
        result['vip_type'] = vip_info.get('vip_type', 0)
        result['green_vip'] = vip_info.get('green_vip', 0) == 1
        result['music_pack_vip'] = vip_info.get('music_pack_vip', 0) == 1
        
        # 测试一首免费歌曲的下载
        test_mid = '001yS0W30qiSLm'  # 一首可能免费的歌曲
        import random
        guid = str(int(random.random() * 10000000))
        
        test_filename = f"M500{test_mid}{test_mid}.mp3"
        vkey_result = _request_vkey(test_mid, test_filename, guid, uin, qqmusic_key)
        
        if vkey_result:
            result['test_vkey_api_code'] = vkey_result.get('code', -1)
            test_data = vkey_result.get('req_0', {}).get('data', {})
            test_midurl = test_data.get('midurlinfo', [{}])[0]
            result['test_purl_available'] = bool(test_midurl.get('purl'))
            result['test_errtype'] = test_midurl.get('errtype', '')
        
    except Exception as e:
        result['error'] = str(e)
    
    logger.info(f"QQ 诊断结果: {result}")
    return jsonify(result)


@app.route('/qq/url/<song_mid>')
def qq_get_url(song_mid):
    """获取 QQ 音乐下载链接"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    qq_cookie = get_cookie_from_request('qq')
    if not qq_cookie:
        return jsonify({'error': 'Missing X-QQ-Cookie header'}), 400
    
    quality = request.args.get('quality', 'exhigh')
    result = get_qq_vkey(song_mid, qq_cookie, quality)
    return jsonify(result)


@app.route('/qq/download/<song_mid>')
def qq_download(song_mid):
    """下载 QQ 音乐（返回文件流）"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    qq_cookie = get_cookie_from_request('qq')
    logger.info(f"收到 QQ 下载请求: {song_mid}, Cookie长度: {len(qq_cookie) if qq_cookie else 0}")
    
    if not qq_cookie:
        return jsonify({'error': 'Missing X-QQ-Cookie header'}), 400
    
    quality = request.args.get('quality', 'exhigh')
    content, file_type, actual_quality = download_qq_song(song_mid, qq_cookie, quality)
    
    if content:
        logger.info(f"QQ下载成功: {song_mid}, 大小: {len(content)}, 音质: {actual_quality}")
        return Response(
            content,
            mimetype='audio/mpeg' if file_type == 'mp3' else 'audio/flac',
            headers={
                'Content-Disposition': f'attachment; filename="{song_mid}.{file_type}"',
                'X-Quality': actual_quality,
                'X-File-Type': file_type,
            }
        )
    
    logger.error(f"QQ下载失败: {song_mid}")
    
    return jsonify({'error': 'Download failed'}), 404


@app.route('/ncm/url/<song_id>')
def ncm_get_url(song_id):
    """获取网易云音乐下载链接"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    ncm_cookie = get_cookie_from_request('ncm')
    if not ncm_cookie:
        return jsonify({'error': 'Missing X-NCM-Cookie header'}), 400
    
    quality = request.args.get('quality', 'exhigh')
    result = get_ncm_url(song_id, ncm_cookie, quality)
    return jsonify(result)


@app.route('/ncm/download/<song_id>')
def ncm_download(song_id):
    """下载网易云音乐（返回文件流）"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    ncm_cookie = get_cookie_from_request('ncm')
    if not ncm_cookie:
        return jsonify({'error': 'Missing X-NCM-Cookie header'}), 400
    
    quality = request.args.get('quality', 'exhigh')
    content, file_type, actual_quality = download_ncm_song(song_id, ncm_cookie, quality)
    
    if content:
        return Response(
            content,
            mimetype='audio/mpeg' if file_type == 'mp3' else 'audio/flac',
            headers={
                'Content-Disposition': f'attachment; filename="{song_id}.{file_type}"',
                'X-Quality': actual_quality,
                'X-File-Type': file_type,
            }
        )
    
    return jsonify({'error': 'Download failed'}), 404


@app.route('/search/qq')
def search_qq():
    """搜索 QQ 音乐"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    keyword = request.args.get('keyword', '')
    limit = int(request.args.get('limit', 10))
    
    if not keyword:
        return jsonify({'error': 'Missing keyword'}), 400
    
    try:
        url = 'https://c.y.qq.com/soso/fcgi-bin/client_search_cp'
        params = {
            'w': keyword,
            'format': 'json',
            'p': 1,
            'n': limit,
        }
        headers = {'Referer': 'https://y.qq.com/'}
        resp = session.get(url, params=params, headers=headers, timeout=10)
        data = resp.json()
        
        songs = []
        for item in data.get('data', {}).get('song', {}).get('list', []):
            songs.append({
                'mid': item.get('songmid'),
                'id': item.get('songid'),
                'title': item.get('songname'),
                'artist': '/'.join([s.get('name', '') for s in item.get('singer', [])]),
                'album': item.get('albumname'),
            })
        
        return jsonify({'success': True, 'songs': songs})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/search/ncm')
def search_ncm():
    """搜索网易云音乐"""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    keyword = request.args.get('keyword', '')
    limit = int(request.args.get('limit', 10))
    
    if not keyword:
        return jsonify({'error': 'Missing keyword'}), 400
    
    try:
        url = 'https://music.163.com/api/search/get'
        params = {
            's': keyword,
            'type': 1,
            'limit': limit,
            'offset': 0,
        }
        headers = {'Referer': 'https://music.163.com/'}
        resp = session.post(url, data=params, headers=headers, timeout=10)
        data = resp.json()
        
        songs = []
        for item in data.get('result', {}).get('songs', []):
            songs.append({
                'id': str(item.get('id')),
                'title': item.get('name'),
                'artist': '/'.join([a.get('name', '') for a in item.get('artists', [])]),
                'album': item.get('album', {}).get('name', ''),
            })
        
        return jsonify({'success': True, 'songs': songs})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8899))
    logger.info(f"音乐下载代理服务启动在端口 {port}")
    logger.info("Cookie 将从请求头传递 (X-QQ-Cookie, X-NCM-Cookie)")
    app.run(host='0.0.0.0', port=port, debug=False)
