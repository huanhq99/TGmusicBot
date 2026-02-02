#!/usr/bin/env python3
"""
Emby 服务模块
处理所有与 Emby 服务器的交互
"""

import logging
import json
import html
from urllib.parse import urljoin
from typing import Optional, Dict, List, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bot.config import (
    EMBY_URL, EMBY_USERNAME, EMBY_PASSWORD,
    APP_VERSION, EMBY_CLIENT_NAME, DEVICE_ID,
    EMBY_SCAN_PAGE_SIZE, LIBRARY_CACHE_FILE
)

logger = logging.getLogger(__name__)

# Emby 认证状态
emby_auth = {'access_token': None, 'user_id': None}

# Emby 媒体库缓存
emby_library_data: List[Dict] = []

# HTTP Session
_requests_session: Optional[requests.Session] = None


def get_requests_session() -> requests.Session:
    """获取 HTTP Session"""
    global _requests_session
    if _requests_session is None:
        _requests_session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "DELETE"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        _requests_session.mount("http://", adapter)
        _requests_session.mount("https://", adapter)
    return _requests_session


def authenticate_emby(base_url: str = None, username: str = None, password: str = None) -> tuple:
    """
    Emby 认证
    
    Returns:
        (access_token, user_id) 或 (None, None)
    """
    global emby_auth
    
    base_url = base_url or EMBY_URL
    username = username or EMBY_USERNAME
    password = password or EMBY_PASSWORD
    
    if not base_url or not username:
        return None, None
    
    api_url = urljoin(base_url, "/emby/Users/AuthenticateByName")
    auth_header = f'Emby Client="{EMBY_CLIENT_NAME}", Device="Docker", DeviceId="{DEVICE_ID}", Version="{APP_VERSION}"'
    headers = {
        'X-Emby-Authorization': auth_header,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    try:
        session = get_requests_session()
        response = session.post(
            api_url, 
            params={'format': 'json'},
            json={"Username": username, "Pw": password or ""},
            headers=headers, 
            timeout=(10, 20)
        )
        response.raise_for_status()
        data = response.json()
        
        if data and 'AccessToken' in data and 'User' in data:
            emby_auth['access_token'] = data['AccessToken']
            emby_auth['user_id'] = data['User']['Id']
            logger.info(f"Emby 认证成功: {username}")
            return data['AccessToken'], data['User']['Id']
    except requests.RequestException as e:
        logger.error(f"Emby 认证失败: {e}")
    
    return None, None


def call_emby_api(endpoint: str, params: dict = None, method: str = 'GET', 
                  data: dict = None, user_auth: dict = None, timeout: tuple = (15, 60)) -> Optional[dict]:
    """
    调用 Emby API
    
    Args:
        endpoint: API 端点
        params: 查询参数
        method: HTTP 方法
        data: POST 数据
        user_auth: 用户认证信息（可选）
        timeout: 超时设置
        
    Returns:
        API 响应或 None
    """
    auth = user_auth or emby_auth
    access_token = auth.get('access_token')
    user_id = auth.get('user_id')
    
    if not access_token or not user_id:
        return None
    
    api_url = urljoin(EMBY_URL, f"/emby/{endpoint.lstrip('/')}")
    auth_header = (
        f'Emby UserId="{user_id}", Client="{EMBY_CLIENT_NAME}", '
        f'Device="Docker", DeviceId="{DEVICE_ID}", '
        f'Version="{APP_VERSION}", Token="{access_token}"'
    )
    headers = {
        'X-Emby-Authorization': auth_header,
        'X-Emby-Token': access_token,
        'Accept': 'application/json'
    }
    query_params = {'format': 'json', **(params or {})}
    
    try:
        session = get_requests_session()
        
        if method.upper() == 'GET':
            response = session.get(api_url, params=query_params, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            headers['Content-Type'] = 'application/json'
            response = session.post(api_url, params=params, json=data, headers=headers, timeout=timeout)
        elif method.upper() == 'DELETE':
            response = session.delete(api_url, params=params, headers=headers, timeout=timeout)
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


def trigger_emby_library_scan(user_auth: dict = None) -> bool:
    """触发 Emby 媒体库扫描"""
    try:
        result = call_emby_api("Library/Refresh", method='POST', user_auth=user_auth)
        if result:
            logger.info("已触发 Emby 媒体库扫描")
            return True
        return False
    except Exception as e:
        logger.error(f"触发 Emby 扫库失败: {e}")
        return False


def scan_emby_library(save_to_cache: bool = True, user_id: str = None, 
                      access_token: str = None) -> List[Dict]:
    """
    扫描 Emby 媒体库
    
    Args:
        save_to_cache: 是否保存到缓存
        user_id: 用户 ID
        access_token: 访问令牌
        
    Returns:
        歌曲列表
    """
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
            'IncludeItemTypes': 'Audio',
            'Recursive': 'true',
            'Limit': EMBY_SCAN_PAGE_SIZE,
            'StartIndex': start_index,
            'Fields': 'Id,Name,ArtistItems,Album,AlbumArtist'
        }
        response = call_emby_api(
            f"Users/{scan_user_id}/Items", 
            params, 
            user_auth=temp_auth, 
            timeout=(15, 180)
        )
        
        if response and 'Items' in response:
            items = response['Items']
            if not items:
                break
            
            for item in items:
                artists = "/".join([a.get('Name', '') for a in item.get('ArtistItems', [])])
                album = item.get('Album', '') or item.get('AlbumArtist', '')
                scanned_songs.append({
                    'id': str(item.get('Id')),
                    'title': html.unescape(item.get('Name', '')),
                    'artist': html.unescape(artists),
                    'album': html.unescape(album) if album else ''
                })
            
            logger.info(f"已扫描 {len(scanned_songs)} 首歌曲...")
            
            if len(items) < EMBY_SCAN_PAGE_SIZE:
                break
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


def load_library_cache() -> List[Dict]:
    """加载媒体库缓存"""
    global emby_library_data
    
    try:
        if LIBRARY_CACHE_FILE.exists():
            with open(LIBRARY_CACHE_FILE, 'r', encoding='utf-8') as f:
                cached_data = json.load(f)
            
            if isinstance(cached_data, list):
                emby_library_data = cached_data
                logger.info(f"从缓存加载 {len(emby_library_data)} 首歌曲")
                return emby_library_data
    except Exception as e:
        logger.warning(f"加载缓存失败: {e}")
    
    return []


def get_user_emby_playlists(user_auth: dict) -> List[Dict]:
    """获取用户的 Emby 歌单列表"""
    if not user_auth:
        return []
    
    params = {
        'IncludeItemTypes': 'Playlist',
        'Recursive': 'true',
        'Fields': 'Id,Name'
    }
    response = call_emby_api(
        f"Users/{user_auth['user_id']}/Items", 
        params, 
        user_auth=user_auth
    )
    
    if response and 'Items' in response:
        return [{'id': p.get('Id'), 'name': p.get('Name')} for p in response['Items']]
    return []


def delete_emby_playlist(playlist_id: str, user_auth: dict) -> bool:
    """删除 Emby 歌单"""
    result = call_emby_api(f"Items/{playlist_id}", {}, method='DELETE', user_auth=user_auth)
    return result is not None


def create_emby_playlist(name: str, song_ids: List[str], user_auth: dict, 
                         is_public: bool = False) -> Optional[str]:
    """
    创建 Emby 歌单
    
    Returns:
        歌单 ID 或 None
    """
    if not user_auth or not song_ids:
        return None
    
    # 创建歌单
    create_params = {
        'Name': name,
        'Ids': ','.join(song_ids[:1]),  # 先添加一首
        'UserId': user_auth['user_id'],
        'MediaType': 'Audio'
    }
    
    result = call_emby_api("Playlists", create_params, method='POST', user_auth=user_auth)
    
    if result and 'Id' in result:
        playlist_id = result['Id']
        logger.info(f"创建歌单成功: {name} (ID: {playlist_id})")
        
        # 添加剩余歌曲（分批）
        if len(song_ids) > 1:
            from bot.config import EMBY_PLAYLIST_ADD_BATCH_SIZE
            remaining = song_ids[1:]
            for i in range(0, len(remaining), EMBY_PLAYLIST_ADD_BATCH_SIZE):
                batch = remaining[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
                add_params = {'Ids': ','.join(batch), 'UserId': user_auth['user_id']}
                call_emby_api(f"Playlists/{playlist_id}/Items", add_params, method='POST', user_auth=user_auth)
        
        # 设置可见性 (新增)
        if is_public:
            set_playlist_visibility(playlist_id, True, user_auth)
            
        return playlist_id
    
    return None


def get_library_data() -> List[Dict]:
    """获取当前媒体库数据"""
    return emby_library_data


def get_auth() -> Dict:
    """获取当前认证信息"""
    return emby_auth

def get_all_users() -> List[Dict]:
    """获取所有 Emby 用户"""
    import os
    try:
        # 优先使用 API Key
        api_key = os.environ.get('EMBY_API_KEY', '')
        emby_url = os.environ.get('EMBY_URL', '') or os.environ.get('EMBY_SERVER_URL', '') or EMBY_URL
        
        users_resp = None
        
        # 方法 1: 尝试使用 API Key
        if api_key and emby_url:
            try:
                session = get_requests_session()
                url = f"{emby_url.rstrip('/')}/emby/Users?api_key={api_key}"
                resp = session.get(url, timeout=10)
                if resp.status_code == 200:
                    users_resp = resp.json()
                elif resp.status_code == 401:
                    logger.warning(f"[get_all_users] API Key 无效 (401)，尝试 Session 认证")
            except Exception as e:
                logger.warning(f"[get_all_users] API Key 请求部分失败: {e}")

        # 方法 2: 如果 API Key 失败，尝试 Session 认证
        if users_resp is None:
            if not emby_auth.get('access_token'):
                # 尝试重新登录
                authenticate_emby()
            
            if emby_auth.get('access_token'):
                # 再次调用
                result = call_emby_api("Users", {}, method='GET')
                if isinstance(result, list):
                    users_resp = result
                elif isinstance(result, dict) and 'Items' in result: # 部分版本可能返回 Items
                     users_resp = result['Items']
            else:
                 logger.error("[get_all_users] 无有效 API Key 且 Session 认证失败")
        
        if users_resp and isinstance(users_resp, list):
            users = [{'id': u['Id'], 'name': u['Name']} for u in users_resp]
            logger.info(f"[get_all_users] Found {len(users)} users")
            return users
        return []
    except Exception as e:
        logger.error(f"获取所有用户失败: {e}")
        return []


def get_user_playback_history(user_id: str, limit: int = None, user_auth: dict = None) -> List[Dict]:
    """
    获取用户的播放历史
    
    Args:
        user_id: Emby 用户 ID
        limit: 限制数量（None = 全部）
        user_auth: 用户认证信息
        
    Returns:
        播放过的歌曲列表（带流派信息）
    """
    all_items = []
    start_index = 0
    page_size = 500
    
    while True:
        params = {
            'IncludeItemTypes': 'Audio',
            'Recursive': 'true',
            'Filters': 'IsPlayed',
            'Fields': 'Id,Name,ArtistItems,Album,Genres,UserData',
            'SortBy': 'PlayCount',
            'SortOrder': 'Descending',
            'Limit': page_size,
            'StartIndex': start_index
        }
        
        response = call_emby_api(
            f"Users/{user_id}/Items", 
            params, 
            user_auth=user_auth,
            timeout=(15, 120)
        )
        
        if response and 'Items' in response:
            items = response['Items']
            if not items:
                break
            all_items.extend(items)
            
            if limit and len(all_items) >= limit:
                all_items = all_items[:limit]
                break
            
            if len(items) < page_size:
                break
            start_index += page_size
        else:
            break
    
    logger.info(f"[Emby] 获取用户 {user_id} 播放历史: {len(all_items)} 首")
    return all_items


def get_library_songs_with_genres(user_auth: dict = None) -> List[Dict]:
    """
    获取带流派信息的完整媒体库
    
    Returns:
        歌曲列表（包含 Id, Name, ArtistItems, Album, Genres）
    """
    all_items = []
    start_index = 0
    page_size = 500
    
    scan_user_id = emby_auth.get('user_id')
    if user_auth:
        scan_user_id = user_auth.get('user_id')
    
    if not scan_user_id:
        return []
    
    while True:
        params = {
            'IncludeItemTypes': 'Audio',
            'Recursive': 'true',
            'Fields': 'Id,Name,ArtistItems,Album,Genres',
            'Limit': page_size,
            'StartIndex': start_index
        }
        
        response = call_emby_api(
            f"Users/{scan_user_id}/Items", 
            params, 
            user_auth=user_auth,
            timeout=(15, 180)
        )
        
        if response and 'Items' in response:
            items = response['Items']
            if not items:
                break
            all_items.extend(items)
            
            if len(items) < page_size:
                break
            start_index += page_size
        else:
            break
    
    logger.info(f"[Emby] 获取带流派媒体库: {len(all_items)} 首")
    return all_items


def create_private_playlist(name: str, song_ids: List[str], user_auth: dict) -> Optional[str]:
    """
    创建私有歌单（仅用户自己可见）
    
    Args:
        name: 歌单名称
        song_ids: 歌曲 ID 列表
        user_auth: 用户认证信息
        
    Returns:
        歌单 ID 或 None
    """
    if not user_auth or not song_ids:
        return None
    
    user_id = user_auth.get('user_id')
    
    # 创建歌单（使用用户自己的 ID，确保私有）
    create_params = {
        'Name': name,
        'Ids': ','.join(song_ids[:1]),
        'UserId': user_id,
        'MediaType': 'Audio'
    }
    
    result = call_emby_api("Playlists", create_params, method='POST', user_auth=user_auth)
    
    if result and 'Id' in result:
        playlist_id = result['Id']
        logger.info(f"[Emby] 创建私有歌单成功: {name} (ID: {playlist_id})")
        
        # 添加剩余歌曲
        if len(song_ids) > 1:
            from bot.config import EMBY_PLAYLIST_ADD_BATCH_SIZE
            remaining = song_ids[1:]
            for i in range(0, len(remaining), EMBY_PLAYLIST_ADD_BATCH_SIZE):
                batch = remaining[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
                add_params = {'Ids': ','.join(batch), 'UserId': user_id}
                call_emby_api(f"Playlists/{playlist_id}/Items", add_params, method='POST', user_auth=user_auth)
        
        return playlist_id
    
    return None


def find_playlist_by_name(name: str, user_auth: dict) -> Optional[str]:
    """
    根据名称查找歌单
    
    Returns:
        歌单 ID 或 None
    """
    playlists = get_user_emby_playlists(user_auth)
    for p in playlists:
        if p.get('name') == name:
            return p.get('id')
    return None


def update_playlist_items(playlist_id: str, song_ids: List[str], user_auth: dict) -> bool:
    """
    更新歌单内容（清空后重新添加）
    
    Args:
        playlist_id: 歌单 ID
        song_ids: 新的歌曲 ID 列表
        user_auth: 用户认证
        
    Returns:
        是否成功
    """
    user_id = user_auth.get('user_id')
    
    # 获取现有歌曲
    params = {'Fields': 'Id'}
    response = call_emby_api(f"Playlists/{playlist_id}/Items", params, user_auth=user_auth)
    
    if response and 'Items' in response:
        # 删除现有歌曲
        existing_ids = [item['Id'] for item in response['Items']]
        if existing_ids:
            delete_params = {
                'EntryIds': ','.join(existing_ids)
            }
            call_emby_api(f"Playlists/{playlist_id}/Items", delete_params, method='DELETE', user_auth=user_auth)
    
    # 添加新歌曲
    from bot.config import EMBY_PLAYLIST_ADD_BATCH_SIZE
    for i in range(0, len(song_ids), EMBY_PLAYLIST_ADD_BATCH_SIZE):
        batch = song_ids[i:i + EMBY_PLAYLIST_ADD_BATCH_SIZE]
        add_params = {'Ids': ','.join(batch), 'UserId': user_id}
        call_emby_api(f"Playlists/{playlist_id}/Items", add_params, method='POST', user_auth=user_auth)
    
    logger.info(f"[Emby] 更新歌单 {playlist_id}: {len(song_ids)} 首歌曲")
    return True


def set_playlist_visibility(playlist_id: str, is_public: bool, user_auth: dict) -> bool:
    """
    设置歌单可见性
    
    Args:
        playlist_id: 歌单 ID
        is_public: 是否公开
        user_auth: 用户认证信息
        
    Returns:
        是否成功
    """
    if not playlist_id or not user_auth:
        return False
        
    try:
        user_id = user_auth.get('user_id')
        
        # Emby 逻辑：通过设置 Shares 来控制访问
        # 如果公开，需将所有用户添加到 Shares
        # 如果私有，清空 Shares (或只保留所有者，通常 Shares 列表不包含所有者)
        
        shares = []
        if is_public:
            all_users = get_all_users()
            # 过滤掉所有者自己
            shares = [{'UserId': u['id']} for u in all_users if u['id'] != user_id]
        
        # 调用 API 更新 Shares
        # 注意：Emby API 可能是 POST /Items/{Id}/Shares 或 /Users/{UserId}/Items/{Id}/Shares
        # 尝试标准方法：更新 Item 的 Shares 属性，但通常需要专门的 endpoint
        # 尝试: POST /Items/{Id}/Shares body: [{"UserId": "..."}]
        
        # 很多 Emby 版本使用这个 Endpoint
        # url = f"Items/{playlist_id}/Shares"
        # 实际更通用的做法可能是 UpdateItem，但 Shares 是特殊的
        
        # 尝试方案 A: /Items/{Id}/Shares (Jellyfin/Emby common)
        share_payload = shares
        
        # 如果是 Emby，可能需要包含 CanEdit 等字段
        # share_payload = [{'UserId': u['id'], 'CanEdit': False} for u in all_users if u['id'] != user_id]
        
        logger.info(f"[Emby] 设置歌单 {playlist_id} 可见性: {'公开' if is_public else '私有'} (分享给 {len(shares)} 人)")
        
        # 先尝试 Items/Id/Shares
        try:
            # 方案 A: /Items/{Id}/Shares
            result = call_emby_api(f"Items/{playlist_id}/Shares", share_payload, method='POST', user_auth=user_auth)
            if result is not None: 
                return True
        except Exception as e:
            logger.debug(f"[Emby] Items/Shares 接口失败: {e}，尝试备用方案")
            
        # 备用方案 B: /Users/{UserId}/Items/{Id}/Shares
        try:
             url = f"Users/{user_id}/Items/{playlist_id}/Shares"
             result = call_emby_api(url, share_payload, method='POST', user_auth=user_auth)
             if result is not None:
                logger.info(f"[Emby] 备用方案 Users/Items/Shares 成功")
                return True
        except Exception as e:
            logger.error(f"[Emby] 备用方案 Users/Items/Shares 失败: {e}")

        # 备用方案 C: MakePublic (仅适用于 owner)
        if is_public:
            try:
                call_emby_api(f"Items/{playlist_id}/MakePublic", {}, method='POST', user_auth=user_auth)
                logger.info("[Emby] 尝试使用 MakePublic 接口")
                return True
            except:
                pass

        return False
            
    except Exception as e:
        logger.error(f"[Emby] 设置可见性失败: {e}")
        return False


# ============================================================
# 用户管理功能 (用于会员系统)
# ============================================================

def create_emby_user(username: str, password: str) -> Dict:
    """
    创建 Emby 用户
    
    Args:
        username: 用户名
        password: 密码
        
    Returns:
        {'success': True, 'user_id': str} 或 {'success': False, 'error': str}
    """
    import os
    try:
        api_key = os.environ.get('EMBY_API_KEY', '')
        emby_url = os.environ.get('EMBY_URL', '') or EMBY_URL
        
        if not api_key or not emby_url:
            return {'success': False, 'error': 'EMBY_API_KEY 未配置'}
        
        session = get_requests_session()
        
        # 创建用户
        url = f"{emby_url.rstrip('/')}/emby/Users/New?api_key={api_key}"
        data = {
            'Name': username
        }
        
        resp = session.post(url, json=data, timeout=15)
        
        if resp.status_code in [200, 201]:
            user_data = resp.json()
            user_id = user_data.get('Id')
            
            if user_id:
                # 设置密码
                password_url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}/Password?api_key={api_key}"
                password_data = {
                    'NewPw': password,
                    'ResetPassword': False
                }
                pwd_resp = session.post(password_url, json=password_data, timeout=15)
                
                if pwd_resp.status_code in [200, 204]:
                    logger.info(f"[Emby] 创建用户成功: {username} (ID: {user_id})")
                    return {'success': True, 'user_id': user_id}
                else:
                    logger.warning(f"[Emby] 密码设置失败: {pwd_resp.status_code}")
                    return {'success': True, 'user_id': user_id, 'warning': '密码设置失败'}
            
            return {'success': False, 'error': '创建用户响应无效'}
        else:
            error_msg = resp.text[:200] if resp.text else f"状态码: {resp.status_code}"
            logger.error(f"[Emby] 创建用户失败: {error_msg}")
            return {'success': False, 'error': error_msg}
            
    except Exception as e:
        logger.error(f"[Emby] 创建用户异常: {e}")
        return {'success': False, 'error': str(e)}


def disable_emby_user(user_id: str) -> Dict:
    """
    禁用 Emby 用户
    
    Args:
        user_id: Emby 用户 ID
        
    Returns:
        {'success': True} 或 {'success': False, 'error': str}
    """
    import os
    try:
        api_key = os.environ.get('EMBY_API_KEY', '')
        emby_url = os.environ.get('EMBY_URL', '') or EMBY_URL
        
        if not api_key or not emby_url:
            return {'success': False, 'error': 'EMBY_API_KEY 未配置'}
        
        session = get_requests_session()
        
        # 获取用户信息
        url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}?api_key={api_key}"
        resp = session.get(url, timeout=10)
        
        if resp.status_code != 200:
            return {'success': False, 'error': f'获取用户失败: {resp.status_code}'}
        
        user_data = resp.json()
        
        # 禁用用户
        user_data['Policy']['IsDisabled'] = True
        
        policy_url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}/Policy?api_key={api_key}"
        policy_resp = session.post(policy_url, json=user_data['Policy'], timeout=15)
        
        if policy_resp.status_code in [200, 204]:
            logger.info(f"[Emby] 禁用用户成功: {user_id}")
            return {'success': True}
        else:
            return {'success': False, 'error': f'禁用失败: {policy_resp.status_code}'}
            
    except Exception as e:
        logger.error(f"[Emby] 禁用用户异常: {e}")
        return {'success': False, 'error': str(e)}


def enable_emby_user(user_id: str) -> Dict:
    """
    启用 Emby 用户
    
    Args:
        user_id: Emby 用户 ID
        
    Returns:
        {'success': True} 或 {'success': False, 'error': str}
    """
    import os
    try:
        api_key = os.environ.get('EMBY_API_KEY', '')
        emby_url = os.environ.get('EMBY_URL', '') or EMBY_URL
        
        if not api_key or not emby_url:
            return {'success': False, 'error': 'EMBY_API_KEY 未配置'}
        
        session = get_requests_session()
        
        # 获取用户信息
        url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}?api_key={api_key}"
        resp = session.get(url, timeout=10)
        
        if resp.status_code != 200:
            return {'success': False, 'error': f'获取用户失败: {resp.status_code}'}
        
        user_data = resp.json()
        
        # 启用用户
        user_data['Policy']['IsDisabled'] = False
        
        policy_url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}/Policy?api_key={api_key}"
        policy_resp = session.post(policy_url, json=user_data['Policy'], timeout=15)
        
        if policy_resp.status_code in [200, 204]:
            logger.info(f"[Emby] 启用用户成功: {user_id}")
            return {'success': True}
        else:
            return {'success': False, 'error': f'启用失败: {policy_resp.status_code}'}
            
    except Exception as e:
        logger.error(f"[Emby] 启用用户异常: {e}")
        return {'success': False, 'error': str(e)}


def authenticate_emby_user(username: str, password: str) -> Dict:
    """
    验证 Emby 用户凭证
    
    Args:
        username: 用户名
        password: 密码
        
    Returns:
        {'success': True, 'user_id': str, 'access_token': str} 或 {'success': False, 'error': str}
    """
    import os
    try:
        emby_url = os.environ.get('EMBY_URL', '') or EMBY_URL
        
        if not emby_url:
            return {'success': False, 'error': 'EMBY_URL 未配置'}
        
        api_url = urljoin(emby_url, "/emby/Users/AuthenticateByName")
        auth_header = f'Emby Client="{EMBY_CLIENT_NAME}", Device="Web", DeviceId="WebAuth", Version="{APP_VERSION}"'
        headers = {
            'X-Emby-Authorization': auth_header,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        session = get_requests_session()
        response = session.post(
            api_url, 
            params={'format': 'json'},
            json={"Username": username, "Pw": password or ""},
            headers=headers, 
            timeout=(10, 20)
        )
        
        if response.status_code == 200:
            data = response.json()
            if data and 'AccessToken' in data and 'User' in data:
                logger.info(f"[Emby] 用户验证成功: {username}")
                return {
                    'success': True,
                    'user_id': data['User']['Id'],
                    'access_token': data['AccessToken']
                }
        
        logger.warning(f"[Emby] 用户验证失败: {username}")
        return {'success': False, 'error': '用户名或密码错误'}
        
    except Exception as e:
        logger.error(f"[Emby] 用户验证异常: {e}")
        return {'success': False, 'error': str(e)}


def update_emby_password(user_id: str, new_password: str) -> Dict:
    """
    更新 Emby 用户密码
    
    Args:
        user_id: Emby 用户 ID
        new_password: 新密码
        
    Returns:
        {'success': True} 或 {'success': False, 'error': str}
    """
    import os
    try:
        api_key = os.environ.get('EMBY_API_KEY', '')
        emby_url = os.environ.get('EMBY_URL', '') or EMBY_URL
        
        if not api_key or not emby_url:
            return {'success': False, 'error': 'EMBY_API_KEY 未配置'}
        
        session = get_requests_session()
        
        password_url = f"{emby_url.rstrip('/')}/emby/Users/{user_id}/Password?api_key={api_key}"
        password_data = {
            'NewPw': new_password,
            'ResetPassword': False
        }
        
        resp = session.post(password_url, json=password_data, timeout=15)
        
        if resp.status_code in [200, 204]:
            logger.info(f"[Emby] 密码更新成功: {user_id}")
            return {'success': True}
        else:
            return {'success': False, 'error': f'密码更新失败: {resp.status_code}'}
            
    except Exception as e:
        logger.error(f"[Emby] 密码更新异常: {e}")
        return {'success': False, 'error': str(e)}

