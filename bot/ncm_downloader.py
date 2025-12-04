#!/usr/bin/env python3
"""
网易云音乐下载模块
功能：
1. 使用 Cookie 登录下载 VIP 歌曲
2. NCM 格式解密转换
3. 自动补全缺失歌曲
"""

import os
import json
import time
import struct
import base64
import logging
import hashlib
import requests
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from Crypto.Cipher import AES
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC

logger = logging.getLogger(__name__)

# NCM 解密常量
NCM_CORE_KEY = b'hzHRAmso5kInbaxW'
NCM_META_KEY = b"#14ljk_!\\]&0U<'("
NCM_MAGIC_HEADER = b'CTENFDAM'


class NCMDecryptor:
    """NCM 文件解密器"""
    
    @staticmethod
    def decrypt_file(ncm_path: str, output_dir: str = None) -> Optional[str]:
        """
        解密 NCM 文件
        
        Args:
            ncm_path: NCM 文件路径
            output_dir: 输出目录，默认为 NCM 文件所在目录
            
        Returns:
            解密后的文件路径，失败返回 None
        """
        try:
            ncm_path = Path(ncm_path)
            if not ncm_path.exists():
                logger.error(f"NCM 文件不存在: {ncm_path}")
                return None
            
            output_dir = Path(output_dir) if output_dir else ncm_path.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            
            with open(ncm_path, 'rb') as f:
                # 验证魔术头
                header = f.read(8)
                if header != NCM_MAGIC_HEADER:
                    logger.error(f"不是有效的 NCM 文件: {ncm_path}")
                    return None
                
                # 跳过 2 字节
                f.read(2)
                
                # 读取并解密 key
                key_length = struct.unpack('<I', f.read(4))[0]
                key_data = bytearray(f.read(key_length))
                for i in range(len(key_data)):
                    key_data[i] ^= 0x64
                
                key_data = NCMDecryptor._aes_decrypt(bytes(key_data), NCM_CORE_KEY)
                key_data = key_data[17:]  # 去掉 "neteasecloudmusic"
                
                # 读取并解密元数据
                meta_length = struct.unpack('<I', f.read(4))[0]
                if meta_length > 0:
                    meta_data = bytearray(f.read(meta_length))
                    for i in range(len(meta_data)):
                        meta_data[i] ^= 0x63
                    
                    meta_data = base64.b64decode(bytes(meta_data)[22:])
                    meta_data = NCMDecryptor._aes_decrypt(meta_data, NCM_META_KEY)
                    meta_data = meta_data[6:]  # 去掉 "music:"
                    metadata = json.loads(meta_data.decode('utf-8'))
                else:
                    metadata = {}
                
                # 跳过 CRC 和 5 字节间隔
                f.read(4)
                f.read(5)
                
                # 读取专辑封面
                image_size = struct.unpack('<I', f.read(4))[0]
                image_data = f.read(image_size) if image_size > 0 else None
                
                # 读取音频数据
                audio_data = bytearray(f.read())
                
                # 解密音频
                key_box = NCMDecryptor._create_key_box(key_data)
                for i in range(len(audio_data)):
                    j = (i + 1) & 0xff
                    audio_data[i] ^= key_box[(key_box[j] + key_box[(key_box[j] + j) & 0xff]) & 0xff]
                
                # 确定输出格式
                format_type = metadata.get('format', 'mp3')
                if format_type not in ['mp3', 'flac']:
                    # 通过文件头判断
                    if audio_data[:4] == b'fLaC':
                        format_type = 'flac'
                    else:
                        format_type = 'mp3'
                
                # 生成输出文件名
                music_name = metadata.get('musicName', ncm_path.stem)
                artist_name = '/'.join([a[0] if isinstance(a, list) else a for a in metadata.get('artist', [])])
                if artist_name:
                    output_name = f"{artist_name} - {music_name}.{format_type}"
                else:
                    output_name = f"{music_name}.{format_type}"
                
                # 清理文件名中的非法字符
                output_name = "".join(c for c in output_name if c not in r'<>:"/\|?*')
                output_path = output_dir / output_name
                
                # 写入文件
                with open(output_path, 'wb') as out:
                    out.write(bytes(audio_data))
                
                # 写入元数据和封面
                NCMDecryptor._write_metadata(str(output_path), metadata, image_data, format_type)
                
                logger.info(f"NCM 解密成功: {output_path}")
                return str(output_path)
                
        except Exception as e:
            logger.error(f"NCM 解密失败: {e}")
            return None
    
    @staticmethod
    def _aes_decrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB 解密"""
        cipher = AES.new(key, AES.MODE_ECB)
        decrypted = cipher.decrypt(data)
        # 去除 PKCS7 填充
        pad_len = decrypted[-1]
        return decrypted[:-pad_len]
    
    @staticmethod
    def _create_key_box(key: bytes) -> list:
        """创建密钥盒"""
        key_box = list(range(256))
        j = 0
        for i in range(256):
            j = (key_box[i] + j + key[i % len(key)]) & 0xff
            key_box[i], key_box[j] = key_box[j], key_box[i]
        return key_box
    
    @staticmethod
    def _write_metadata(file_path: str, metadata: dict, image_data: bytes, format_type: str):
        """写入音频元数据"""
        try:
            if format_type == 'mp3':
                audio = MP3(file_path, ID3=ID3)
                try:
                    audio.add_tags()
                except:
                    pass
                
                if metadata.get('musicName'):
                    audio.tags.add(TIT2(encoding=3, text=metadata['musicName']))
                
                artists = metadata.get('artist', [])
                if artists:
                    artist_str = '/'.join([a[0] if isinstance(a, list) else a for a in artists])
                    audio.tags.add(TPE1(encoding=3, text=artist_str))
                
                if metadata.get('album'):
                    audio.tags.add(TALB(encoding=3, text=metadata['album']))
                
                if image_data:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=image_data
                    ))
                
                audio.save()
                
            elif format_type == 'flac':
                audio = FLAC(file_path)
                
                if metadata.get('musicName'):
                    audio['title'] = metadata['musicName']
                
                artists = metadata.get('artist', [])
                if artists:
                    audio['artist'] = '/'.join([a[0] if isinstance(a, list) else a for a in artists])
                
                if metadata.get('album'):
                    audio['album'] = metadata['album']
                
                # FLAC 封面处理略复杂，暂时跳过
                audio.save()
                
        except Exception as e:
            logger.warning(f"写入元数据失败: {e}")


class NeteaseMusicAPI:
    """网易云音乐 API"""
    
    BASE_URL = "https://music.163.com"
    EAPI_URL = "https://interface.music.163.com/eapi"
    
    # 加密参数
    EAPI_KEY = b'e82ckenh8dichen8'
    
    def __init__(self, cookie: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://music.163.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        
        if cookie:
            self.set_cookie(cookie)
        
        self.user_info = None
        self.is_vip = False
    
    def set_cookie(self, cookie: str):
        """设置 Cookie"""
        # 支持字符串格式和字典格式
        if isinstance(cookie, str):
            for item in cookie.split(';'):
                if '=' in item:
                    key, value = item.strip().split('=', 1)
                    self.session.cookies.set(key, value)
        elif isinstance(cookie, dict):
            for key, value in cookie.items():
                self.session.cookies.set(key, value)
    
    def check_login(self) -> Tuple[bool, dict]:
        """检查登录状态"""
        try:
            url = f"{self.BASE_URL}/api/nuser/account/get"
            resp = self.session.get(url, timeout=10)
            data = resp.json()
            
            if data.get('code') == 200 and data.get('account'):
                self.user_info = data.get('profile', {})
                # 检查 VIP 状态
                vip_type = data.get('account', {}).get('vipType', 0)
                self.is_vip = vip_type > 0
                
                return True, {
                    'nickname': self.user_info.get('nickname', '未知'),
                    'user_id': self.user_info.get('userId'),
                    'vip_type': vip_type,
                    'is_vip': self.is_vip
                }
            return False, {}
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")
            return False, {}
    
    def get_song_url(self, song_ids: List[str], quality: str = 'exhigh') -> Dict[str, dict]:
        """
        获取歌曲下载链接
        
        Args:
            song_ids: 歌曲 ID 列表
            quality: 音质 (standard/higher/exhigh/lossless/hires)
            
        Returns:
            {song_id: {'url': ..., 'size': ..., 'type': ...}}
        """
        # 音质对应的 level
        level_map = {
            'standard': 'standard',
            'higher': 'higher', 
            'exhigh': 'exhigh',
            'lossless': 'lossless',
            'hires': 'hires'
        }
        level = level_map.get(quality, 'exhigh')
        
        try:
            # 使用 eapi 接口获取下载链接
            url = f"{self.EAPI_URL}/song/enhance/player/url/v1"
            params = {
                'ids': json.dumps([int(i) for i in song_ids]),
                'level': level,
                'encodeType': 'flac' if level in ['lossless', 'hires'] else 'mp3'
            }
            
            # eapi 加密请求
            data = self._eapi_encrypt('/api/song/enhance/player/url/v1', params)
            resp = self.session.post(url, data=data, timeout=15)
            result = resp.json()
            
            song_urls = {}
            if result.get('code') == 200:
                for item in result.get('data', []):
                    song_id = str(item.get('id'))
                    if item.get('url'):
                        song_urls[song_id] = {
                            'url': item['url'],
                            'size': item.get('size', 0),
                            'type': item.get('type', 'mp3'),
                            'level': item.get('level', level),
                            'br': item.get('br', 0)  # 比特率
                        }
            return song_urls
            
        except Exception as e:
            logger.error(f"获取歌曲链接失败: {e}")
            return {}
    
    def download_song(self, song_id: str, song_info: dict, output_dir: str, 
                      quality: str = 'exhigh') -> Optional[str]:
        """
        下载单首歌曲
        
        Args:
            song_id: 歌曲 ID
            song_info: 歌曲信息 {'title': ..., 'artist': ...}
            output_dir: 输出目录
            quality: 音质
            
        Returns:
            下载的文件路径，失败返回 None
        """
        try:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # 获取下载链接
            urls = self.get_song_url([song_id], quality)
            if song_id not in urls:
                logger.warning(f"无法获取歌曲下载链接: {song_id}")
                return None
            
            url_info = urls[song_id]
            url = url_info['url']
            file_type = url_info.get('type', 'mp3')
            
            # 生成文件名
            title = song_info.get('title', song_id)
            artist = song_info.get('artist', '')
            if artist:
                filename = f"{artist} - {title}.{file_type}"
            else:
                filename = f"{title}.{file_type}"
            
            # 清理非法字符
            filename = "".join(c for c in filename if c not in r'<>:"/\|?*')
            output_path = output_dir / filename
            
            # 下载文件
            logger.info(f"下载歌曲: {filename}")
            resp = self.session.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # 检查是否是 NCM 格式需要解密
            with open(output_path, 'rb') as f:
                header = f.read(8)
            
            if header == NCM_MAGIC_HEADER:
                logger.info(f"检测到 NCM 格式，正在解密...")
                decrypted_path = NCMDecryptor.decrypt_file(str(output_path), str(output_dir))
                output_path.unlink()  # 删除原 NCM 文件
                if decrypted_path:
                    return decrypted_path
                return None
            
            logger.info(f"下载完成: {output_path}")
            return str(output_path)
            
        except Exception as e:
            logger.error(f"下载歌曲失败 {song_id}: {e}")
            return None
    
    def batch_download(self, songs: List[dict], output_dir: str, 
                       quality: str = 'exhigh', 
                       progress_callback=None) -> Tuple[List[str], List[dict]]:
        """
        批量下载歌曲
        
        Args:
            songs: 歌曲列表 [{'source_id': ..., 'title': ..., 'artist': ...}, ...]
            output_dir: 输出目录
            quality: 音质
            progress_callback: 进度回调函数 (current, total, song_info)
            
        Returns:
            (成功下载的文件列表, 失败的歌曲列表)
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        success_files = []
        failed_songs = []
        
        total = len(songs)
        for i, song in enumerate(songs):
            song_id = song.get('source_id')
            if not song_id:
                failed_songs.append(song)
                continue
            
            if progress_callback:
                progress_callback(i + 1, total, song)
            
            result = self.download_song(song_id, song, str(output_dir), quality)
            if result:
                success_files.append(result)
            else:
                failed_songs.append(song)
            
            # 避免请求过快
            time.sleep(0.5)
        
        return success_files, failed_songs
    
    def _eapi_encrypt(self, path: str, params: dict) -> dict:
        """EAPI 请求加密"""
        params_str = json.dumps(params, separators=(',', ':'))
        message = f"nobody{path}use{params_str}md5forencrypt"
        digest = hashlib.md5(message.encode()).hexdigest()
        text = f"{path}-36cd479b6b5-{params_str}-36cd479b6b5-{digest}"
        
        # AES 加密
        pad_len = 16 - len(text.encode()) % 16
        text_padded = text + chr(pad_len) * pad_len
        cipher = AES.new(self.EAPI_KEY, AES.MODE_ECB)
        encrypted = cipher.encrypt(text_padded.encode())
        
        return {'params': encrypted.hex().upper()}
    
    # ==================== 二维码登录相关 ====================
    
    def qr_login_create(self) -> Tuple[bool, dict]:
        """
        创建二维码登录 key
        
        Returns:
            (success, {'unikey': ..., 'qr_url': ..., 'qr_img': ...})
        """
        try:
            # 使用 EAPI 加密接口获取 unikey
            eapi_path = "/api/login/qrcode/unikey"
            params = {'type': 1}
            
            encrypted_params = self._eapi_encrypt(eapi_path, params)
            
            url = f"{self.EAPI_URL}/login/qrcode/unikey"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://music.163.com/',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            
            resp = self.session.post(url, data=encrypted_params, headers=headers, timeout=10)
            data = resp.json()
            
            logger.info(f"[QR Create EAPI] API 响应: code={data.get('code')}, keys={list(data.keys())}")
            
            if data.get('code') == 200:
                unikey = data.get('unikey')
                if not unikey:
                    logger.error("[QR Create] unikey 为空")
                    return False, {'error': 'unikey 为空'}
                
                # 生成二维码 URL - 网易云 App 扫描需要的格式
                qr_url = f"https://music.163.com/login?codekey={unikey}"
                
                # 生成二维码图片
                import urllib.parse
                encoded_url = urllib.parse.quote(qr_url, safe='')
                qr_img_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={encoded_url}"
                
                logger.info(f"[QR Create] 成功, unikey={unikey[:20]}...")
                
                return True, {
                    'unikey': unikey,
                    'qr_url': qr_url,
                    'qr_img': qr_img_url
                }
            else:
                logger.error(f"创建二维码失败: {data}")
                return False, {'error': data.get('message', '创建失败')}
                
        except Exception as e:
            logger.error(f"创建二维码异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False, {'error': str(e)}
    
    def qr_login_check(self, unikey: str) -> Tuple[int, dict]:
        """
        检查二维码扫描状态
        
        Args:
            unikey: 二维码 key
            
        Returns:
            (status_code, data)
            status_code:
                800: 二维码过期
                801: 等待扫描
                802: 已扫描，等待确认
                803: 登录成功
        """
        try:
            # 使用 EAPI 加密接口
            eapi_path = "/api/login/qrcode/client/login"
            params = {
                'key': unikey,
                'type': 1,
            }
            
            # 加密参数
            encrypted_params = self._eapi_encrypt(eapi_path, params)
            
            url = f"{self.EAPI_URL}/login/qrcode/client/login"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://music.163.com/',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            
            resp = self.session.post(url, data=encrypted_params, headers=headers, timeout=10)
            data = resp.json()
            
            code = data.get('code', 0)
            logger.info(f"[QR Check EAPI] API 响应: code={code}, keys={list(data.keys())}")
            
            if code == 803:
                # 登录成功，提取 cookie
                cookies_dict = {}
                
                # 从 response cookies 中获取
                for cookie in resp.cookies:
                    cookies_dict[cookie.name] = cookie.value
                    self.session.cookies.set(cookie.name, cookie.value, domain='.music.163.com')
                    logger.info(f"[QR Check] resp.cookies: {cookie.name}")
                
                # 从 session cookies 获取
                for cookie in self.session.cookies:
                    if cookie.name not in cookies_dict:
                        cookies_dict[cookie.name] = cookie.value
                
                # 检查响应 JSON 中是否有 cookie 字段
                if 'cookie' in data and data['cookie']:
                    resp_cookie = data['cookie']
                    logger.info(f"[QR Check] 响应 JSON 中的 cookie 长度: {len(resp_cookie)}")
                    for item in resp_cookie.split(';'):
                        item = item.strip()
                        if not item or '=' not in item:
                            continue
                        first_part = item.split(',')[0]
                        if '=' in first_part:
                            k, v = first_part.split('=', 1)
                            k = k.strip()
                            v = v.strip()
                            if k.lower() not in ('path', 'domain', 'expires', 'max-age', 'httponly', 'secure', 'samesite', 'version'):
                                cookies_dict[k] = v
                                self.session.cookies.set(k, v, domain='.music.163.com')
                
                has_music_u = 'MUSIC_U' in cookies_dict
                logger.info(f"[QR Check] 最终 cookies: {list(cookies_dict.keys())}, 包含 MUSIC_U: {has_music_u}")
                
                if not has_music_u:
                    logger.warning("登录成功但未获取到 MUSIC_U cookie，尝试请求用户信息...")
                    try:
                        user_resp = self.session.get(f"{self.BASE_URL}/api/nuser/account/get", timeout=5)
                        for cookie in self.session.cookies:
                            if cookie.name not in cookies_dict:
                                cookies_dict[cookie.name] = cookie.value
                        logger.info(f"[QR Check] 请求用户信息后 cookies: {list(cookies_dict.keys())}")
                    except Exception as e:
                        logger.warning(f"请求用户信息失败: {e}")
                
                # 构建 cookie 字符串
                important_keys = ['MUSIC_U', '__csrf', 'NMTID', 'MUSIC_A_T', 'MUSIC_R_T', '__remember_me']
                cookie_parts = []
                for key in important_keys:
                    if key in cookies_dict and cookies_dict[key]:
                        cookie_parts.append(f"{key}={cookies_dict[key]}")
                for key, value in cookies_dict.items():
                    if key not in important_keys and value:
                        cookie_parts.append(f"{key}={value}")
                
                cookie_str = '; '.join(cookie_parts)
                logger.info(f"[QR Check] 最终 cookie 字符串长度: {len(cookie_str)}")
                
                return 803, {
                    'message': '登录成功',
                    'cookie': cookie_str,
                    'cookies_dict': cookies_dict
                }
            elif code == 802:
                return 802, {'message': '已扫描，请在手机上确认登录'}
            elif code == 801:
                return 801, {'message': '等待扫描二维码'}
            elif code == 800:
                return 800, {'message': '二维码已过期，请重新获取'}
            else:
                logger.warning(f"[QR Check] 状态码: {code}, 消息: {data.get('message')}")
                return code, {'message': data.get('message', '未知状态')}
                
        except Exception as e:
            logger.error(f"检查二维码状态异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return -1, {'error': str(e)}
    
    def qr_login_wait(self, unikey: str, timeout: int = 120, 
                      callback=None) -> Tuple[bool, dict]:
        """
        等待二维码登录完成
        
        Args:
            unikey: 二维码 key
            timeout: 超时时间（秒）
            callback: 状态回调函数 (status_code, message)
            
        Returns:
            (success, {'cookie': ..., ...})
        """
        import time
        start_time = time.time()
        last_status = None
        
        while time.time() - start_time < timeout:
            code, data = self.qr_login_check(unikey)
            
            if code != last_status:
                last_status = code
                if callback:
                    callback(code, data.get('message', ''))
            
            if code == 803:
                # 登录成功
                return True, data
            elif code == 800:
                # 二维码过期
                return False, data
            elif code == -1:
                # 发生错误
                return False, data
            
            time.sleep(2)  # 每 2 秒检查一次
        
        return False, {'message': '登录超时'}
    
    def search_song(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索歌曲"""
        try:
            url = f"{self.BASE_URL}/api/search/get"
            params = {
                's': keyword,
                'type': 1,  # 1=单曲
                'offset': 0,
                'limit': limit
            }
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            results = []
            if data.get('code') == 200:
                for song in data.get('result', {}).get('songs', []):
                    results.append({
                        'source_id': str(song['id']),
                        'title': song.get('name', ''),
                        'artist': '/'.join([a.get('name', '') for a in song.get('artists', [])]),
                        'album': song.get('album', {}).get('name', ''),
                        'album_id': str(song.get('album', {}).get('id', '')),
                        'platform': 'NCM'
                    })
            return results
        except Exception as e:
            logger.error(f"搜索歌曲失败: {e}")
            return []
    
    def search_album(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索专辑"""
        try:
            url = f"{self.BASE_URL}/api/search/get"
            params = {
                's': keyword,
                'type': 10,  # 10=专辑
                'offset': 0,
                'limit': limit
            }
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            results = []
            if data.get('code') == 200:
                for album in data.get('result', {}).get('albums', []):
                    results.append({
                        'album_id': str(album['id']),
                        'name': album.get('name', ''),
                        'artist': album.get('artist', {}).get('name', ''),
                        'size': album.get('size', 0),
                        'publish_time': album.get('publishTime', 0),
                        'platform': 'NCM'
                    })
            return results
        except Exception as e:
            logger.error(f"搜索专辑失败: {e}")
            return []
    
    def get_album_songs(self, album_id: str) -> List[dict]:
        """获取专辑所有歌曲"""
        try:
            url = f"{self.BASE_URL}/api/album/{album_id}"
            resp = self.session.get(url, timeout=10)
            data = resp.json()
            
            results = []
            if data.get('code') == 200:
                album_info = data.get('album', {})
                for song in data.get('songs', []):
                    results.append({
                        'source_id': str(song['id']),
                        'title': song.get('name', ''),
                        'artist': '/'.join([a.get('name', '') for a in song.get('artists', [])]),
                        'album': album_info.get('name', ''),
                        'platform': 'NCM'
                    })
            return results
        except Exception as e:
            logger.error(f"获取专辑歌曲失败: {e}")
            return []


class MusicAutoDownloader:
    """自动下载补全模块"""
    
    def __init__(self, ncm_cookie: str = None, download_dir: str = None):
        self.ncm_api = NeteaseMusicAPI(ncm_cookie) if ncm_cookie else None
        self.download_dir = Path(download_dir) if download_dir else Path('/tmp/music_downloads')
        self.download_dir.mkdir(parents=True, exist_ok=True)
    
    def set_ncm_cookie(self, cookie: str):
        """设置网易云 Cookie"""
        if not self.ncm_api:
            self.ncm_api = NeteaseMusicAPI(cookie)
        else:
            self.ncm_api.set_cookie(cookie)
    
    def check_ncm_login(self) -> Tuple[bool, dict]:
        """检查网易云登录状态"""
        if not self.ncm_api:
            return False, {'error': '未配置网易云 Cookie'}
        return self.ncm_api.check_login()
    
    def download_missing_songs(self, missing_songs: List[dict], 
                                quality: str = 'exhigh',
                                progress_callback=None) -> Tuple[List[str], List[dict]]:
        """
        下载缺失的歌曲
        
        Args:
            missing_songs: 缺失歌曲列表（必须是网易云的歌曲）
            quality: 下载音质
            progress_callback: 进度回调
            
        Returns:
            (成功下载的文件列表, 失败的歌曲列表)
        """
        if not self.ncm_api:
            logger.error("未配置网易云 Cookie")
            return [], missing_songs
        
        # 检查登录状态
        logged_in, info = self.ncm_api.check_login()
        if not logged_in:
            logger.error("网易云未登录")
            return [], missing_songs
        
        logger.info(f"网易云登录成功: {info.get('nickname')} (VIP: {info.get('is_vip')})")
        
        # 筛选网易云歌曲
        ncm_songs = [s for s in missing_songs if s.get('platform') == 'NCM']
        if not ncm_songs:
            logger.info("没有需要下载的网易云歌曲")
            return [], missing_songs
        
        logger.info(f"开始下载 {len(ncm_songs)} 首网易云歌曲")
        
        return self.ncm_api.batch_download(
            ncm_songs, 
            str(self.download_dir), 
            quality,
            progress_callback
        )


# 便捷函数
def decrypt_ncm_file(ncm_path: str, output_dir: str = None) -> Optional[str]:
    """解密单个 NCM 文件"""
    return NCMDecryptor.decrypt_file(ncm_path, output_dir)


def batch_decrypt_ncm(ncm_dir: str, output_dir: str = None) -> List[str]:
    """批量解密目录下的所有 NCM 文件"""
    ncm_dir = Path(ncm_dir)
    output_dir = Path(output_dir) if output_dir else ncm_dir
    
    results = []
    for ncm_file in ncm_dir.glob('*.ncm'):
        result = NCMDecryptor.decrypt_file(str(ncm_file), str(output_dir))
        if result:
            results.append(result)
    
    return results


if __name__ == '__main__':
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        ncm_file = sys.argv[1]
        result = decrypt_ncm_file(ncm_file)
        if result:
            print(f"解密成功: {result}")
        else:
            print("解密失败")
