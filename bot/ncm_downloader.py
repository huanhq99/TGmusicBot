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
                output_name = output_name.replace('/', ', ').replace('\\', ', ')
                output_name = "".join(c for c in output_name if c not in r'<>:"|?*')
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
        print(f"[WriteMetadata] 开始写入: {file_path}, 格式: {format_type}", flush=True)
        print(f"[WriteMetadata] 元数据: title={metadata.get('musicName')}, album={metadata.get('album')}, artist={metadata.get('artist')}", flush=True)
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
                    logger.info(f"[WriteMetadata] MP3 artist_str: {artist_str}")
                
                if metadata.get('album'):
                    audio.tags.add(TALB(encoding=3, text=metadata['album']))
                
                # Album Artist (TPE2)
                if metadata.get('albumartist'):
                    from mutagen.id3 import TPE2
                    audio.tags.add(TPE2(encoding=3, text=metadata['albumartist']))
                elif artists:
                    from mutagen.id3 import TPE2
                    audio.tags.add(TPE2(encoding=3, text=artist_str))
                
                # Year (TDRC)
                if metadata.get('year'):
                    from mutagen.id3 import TDRC
                    audio.tags.add(TDRC(encoding=3, text=metadata['year']))
                
                # Track Number (TRCK)
                if metadata.get('track'):
                    from mutagen.id3 import TRCK
                    audio.tags.add(TRCK(encoding=3, text=metadata['track']))
                
                if image_data:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=image_data
                    ))
                
                # 内嵌歌词 (USLT)
                if metadata.get('lyrics'):
                    from mutagen.id3 import USLT
                    audio.tags.add(USLT(encoding=3, lang='zho', desc='', text=metadata['lyrics']))
                    print(f"[WriteMetadata] MP3 内嵌歌词已添加", flush=True)
                
                audio.save()
                logger.info(f"[WriteMetadata] MP3 保存成功: {file_path}")
                
            elif format_type == 'flac':
                audio = FLAC(file_path)
                
                if metadata.get('musicName'):
                    audio['title'] = metadata['musicName']
                
                artists = metadata.get('artist', [])
                if artists:
                    artist_str = '/'.join([a[0] if isinstance(a, list) else a for a in artists])
                    audio['artist'] = artist_str
                    logger.info(f"[WriteMetadata] FLAC artist_str: {artist_str}")
                
                if metadata.get('album'):
                    audio['album'] = metadata['album']
                
                # 写入 albumartist
                if metadata.get('albumartist'):
                    audio['albumartist'] = metadata['albumartist']
                elif audio.get('artist'):
                    audio['albumartist'] = audio['artist']
                
                # 写入年份
                if metadata.get('year'):
                    audio['date'] = metadata['year']
                
                # 写入音轨号
                if metadata.get('track'):
                    audio['tracknumber'] = metadata['track']
                
                # FLAC 封面处理
                if image_data:
                    try:
                        from mutagen.flac import Picture
                        picture = Picture()
                        picture.type = 3  # Front cover
                        picture.mime = 'image/jpeg'
                        picture.desc = 'Cover'
                        picture.data = image_data
                        audio.add_picture(picture)
                    except Exception as e:
                        logger.warning(f"添加 FLAC 封面失败: {e}")
                
                # 内嵌歌词
                if metadata.get('lyrics'):
                    audio['lyrics'] = metadata['lyrics']
                    print(f"[WriteMetadata] FLAC 内嵌歌词已添加", flush=True)
                
                audio.save()
                logger.info(f"[WriteMetadata] FLAC 保存成功: {file_path}")
                
        except Exception as e:
            logger.error(f"[WriteMetadata] 写入元数据失败: {e}")
            import traceback
            logger.error(traceback.format_exc())


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
    
    def get_playlist_detail(self, playlist_id: str) -> Optional[dict]:
        """
        获取歌单详情 (使用 EAPI 接口，更完整稳定)
        
        Args:
            playlist_id: 歌单 ID
            
        Returns:
            歌单详情字典 ({'playlist': ..., 'privileges': ...})
        """
        try:
            url = f"{self.EAPI_URL}/v6/playlist/detail"
            params = {
                'id': playlist_id,
                'n': 100000,
                's': 8,
                'p': 112
            }
            
            # EAPI 加密请求
            data = self._eapi_encrypt('/api/v6/playlist/detail', params)
            resp = self.session.post(url, data=data, timeout=15)
            result = resp.json()
            
            if result.get('code') == 200:
                logger.info(f"获取歌单详情成功: {playlist_id}")
                return result
            else:
                logger.warning(f"获取歌单详情失败: code={result.get('code')}, msg={result.get('message', '')}")
                return None
                
        except Exception as e:
            logger.error(f"获取歌单详情异常: {e}")
            return None
            
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
                    else:
                        # 记录失败原因
                        code = item.get('code', 'unknown')
                        fee_type = item.get('fee', 'unknown')
                        logger.warning(f"歌曲 {song_id} 无下载链接: code={code}, fee={fee_type}, level={level}")
            else:
                logger.warning(f"获取歌曲链接API失败: code={result.get('code')}, msg={result.get('message', '')}")
            return song_urls
            
        except Exception as e:
            logger.error(f"获取歌曲链接失败: {e}")
            return {}
    
    def get_lyrics(self, song_id: str) -> Optional[str]:
        """
        获取歌曲歌词 (LRC 格式)
        
        Args:
            song_id: 歌曲 ID
            
        Returns:
            LRC 格式歌词文本，失败返回 None
        """
        try:
            url = f"{self.BASE_URL}/api/song/lyric"
            params = {
                'id': song_id,
                'lv': 1,  # 原始歌词
                'tv': 1,  # 翻译歌词
                'rv': 1   # 罗马音
            }
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get('code') != 200:
                return None
            
            # 获取原始歌词
            lrc = data.get('lrc', {}).get('lyric', '')
            
            # 如果有翻译，合并翻译歌词
            tlyric = data.get('tlyric', {}).get('lyric', '')
            
            if lrc:
                logger.debug(f"获取歌词成功: {song_id}")
                return lrc
            
            return None
            
        except Exception as e:
            logger.warning(f"获取歌词失败 {song_id}: {e}")
            return None
    
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
            filename = filename.replace('/', ', ').replace('\\', ', ')
            filename = "".join(c for c in filename if c not in r'<>:"|?*')
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
            
            # 为普通下载的文件写入元数据
            try:
                # 获取封面
                cover_data = None
                album_info = song_info.get('album', {})
                cover_url = None
                if isinstance(album_info, dict):
                    cover_url = album_info.get('picUrl') or album_info.get('pic')
                elif song_info.get('coverUrl'):
                    cover_url = song_info.get('coverUrl')
                
                if cover_url:
                    try:
                        cover_resp = self.session.get(cover_url, timeout=10)
                        if cover_resp.status_code == 200:
                            cover_data = cover_resp.content
                    except:
                        pass
                
                # 构建元数据
                metadata = {
                    'musicName': song_info.get('title', ''),
                    'artist': [[song_info.get('artist', '')]] if song_info.get('artist') else [],
                    'album': album_info.get('name', '') if isinstance(album_info, dict) else str(album_info) if album_info else ''
                }
                
                # 写入元数据
                NCMDecryptor._write_metadata(str(output_path), metadata, cover_data, file_type)
                logger.info(f"元数据写入完成: {output_path}")
            except Exception as e:
                logger.warning(f"写入元数据失败: {e}")
            
            # 下载歌词
            try:
                lyrics = self.get_lyrics(song_id)
                if lyrics:
                    lrc_path = output_path.with_suffix('.lrc')
                    with open(lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics)
                    logger.info(f"歌词保存完成: {lrc_path.name}")
            except Exception as e:
                logger.debug(f"歌词下载失败: {e}")
            
            logger.info(f"下载完成: {output_path}")
            return str(output_path)
            
        except Exception as e:
            logger.error(f"下载歌曲失败 {song_id}: {e}")
            return None
    
    def batch_download(self, songs: List[dict], output_dir: str, 
                       quality: str = 'exhigh', progress_callback=None,
                       is_organize_mode: bool = False, organize_dir: str = None) -> Tuple[List[str], List[dict]]:
        """
        批量下载歌曲
        
        Args:
            songs: 歌曲列表 [{'source_id': ..., 'title': ..., 'artist': ...}, ...]
            output_dir: 输出目录
            quality: 音质
            progress_callback: 进度回调函数 (current, total, song_info)
            is_organize_mode: 是否整理模式
            organize_dir: 整理目录
            
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
            
            # 确定输出目录
            if is_organize_mode and organize_dir:
                # Folder Logic: Use Primary Artist ONLY (Graphic 1 style)
                # "许嵩/何曼婷" -> "许嵩"
                first_artist = artist.split('/')[0].split(',')[0].strip()
                safe_artist = "".join(c for c in first_artist if c not in r'<>:"|?*')
                
                safe_album = album.replace('/', ', ').replace('\\', ', ')
                safe_album = "".join(c for c in safe_album if c not in r'<>:"|?*')
                target_dir = Path(organize_dir) / safe_artist / safe_album
            else:
                target_dir = output_dir

            result = self.download_song(song_id, song, str(target_dir), quality)
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
                songs = data.get('result', {}).get('songs', [])
                if not songs:
                    return results
                
                # 基础搜索不返回封面，需要批量获取歌曲详情
                song_ids = [str(s['id']) for s in songs]
                cover_map = self._batch_get_covers(song_ids)
                
                for song in songs:
                    song_id = str(song['id'])
                    results.append({
                        'source_id': song_id,
                        'title': song.get('name', ''),
                        'artist': '/'.join([a.get('name', '') for a in song.get('artists', [])]),
                        'album': song.get('album', {}).get('name', ''),
                        'album_id': str(song.get('album', {}).get('id', '')),
                        'publish_time': song.get('album', {}).get('publishTime', 0),
                        'cover_url': cover_map.get(song_id, ''),
                        'platform': 'NCM'
                    })
            return results
        except Exception as e:
            logger.error(f"搜索歌曲失败: {e}")
            return []
    
    def _batch_get_covers(self, song_ids: List[str]) -> dict:
        """批量获取歌曲封面 URL"""
        cover_map = {}
        if not song_ids:
            return cover_map
        try:
            url = f"{self.BASE_URL}/api/song/detail"
            params = {'ids': json.dumps([int(i) for i in song_ids])}
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get('code') == 200:
                for s in data.get('songs', []):
                    song_id = str(s['id'])
                    cover_url = s.get('album', {}).get('picUrl', '')
                    if cover_url:
                        cover_map[song_id] = cover_url
                logger.info(f"[NCM] 批量获取封面成功: {len(cover_map)}/{len(song_ids)}")
        except Exception as e:
            logger.warning(f"批量获取封面失败: {e}")
        return cover_map
    
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
                        'pic_url': album.get('picUrl', ''),
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

    def get_song_detail(self, song_id: str) -> Optional[dict]:
        """
        获取歌曲详情（包含封面等）
        """
        try:
            url = f"{self.BASE_URL}/api/song/detail"
            params = {'ids': f"[{song_id}]"}
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get('code') == 200 and data.get('songs'):
                s = data['songs'][0]
                artists = [a.get('name', '') for a in s.get('artists', [])]
                album_info = s.get('album', {})
                return {
                    'title': s.get('name', ''),
                    'artist': '/'.join(artists),
                    'album_artist': artists[0] if artists else '',  # 专辑艺术家取第一个
                    'album': album_info.get('name', ''),
                    'album_id': str(album_info.get('id', '')),  # 专辑 ID
                    'coverUrl': album_info.get('picUrl'),
                    'publish_time': album_info.get('publishTime', 0),  # 毫秒时间戳
                    'source_id': str(s['id'])
                }
            return None
        except Exception as e:
            logger.error(f"获取歌曲详情失败: {e}")
            return None



class QQMusicAPI:
    """QQ 音乐 API (仅用于 Cookie 保活和刷新)"""
    
    BASE_URL = "https://y.qq.com"
    
    def __init__(self, cookie: str = None, proxy_url: str = None, proxy_key: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://y.qq.com/',
        })
        self.proxy_url = proxy_url
        self.proxy_key = proxy_key
        
        if cookie:
            self.set_cookie(cookie)
            
    def set_cookie(self, cookie: str):
        """设置 Cookie"""
        if isinstance(cookie, str):
            for item in cookie.split(';'):
                if '=' in item:
                    key, value = item.strip().split('=', 1)
                    self.session.cookies.set(key, value, domain='.qq.com')
        elif isinstance(cookie, dict):
            for key, value in cookie.items():
                self.session.cookies.set(key, value, domain='.qq.com')

    def search_song(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索歌曲"""
        try:
            url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                'w': keyword,
                'n': limit,
                'page': 1,
                'cr': 1,
                'new_json': 1,
                'format': 'json',
                'platform': 'yqq.json'
            }
            # 使用 API 自己的 session
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            results = []
            if data.get('code') == 0:
                for song in data.get('data', {}).get('song', {}).get('list', []):
                    # 获取专辑封面
                    album_mid = song.get('album', {}).get('mid', '')
                    cover_url = ""
                    if album_mid:
                        cover_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                        
                    results.append({
                        'source_id': str(song.get('mid', '')),  # 使用 mid 作为 ID
                        'title': song.get('name', ''),
                        'artist': '/'.join([a.get('name', '') for a in song.get('singer', [])]),
                        'album': song.get('album', {}).get('name', ''),
                        'cover_url': cover_url,
                        'platform': 'QQ'
                    })
            return results
        except Exception as e:
            logger.error(f"QQ音乐搜索失败: {e}")
            return []

    def get_download_url(self, song_mid: str, quality: str = 'exhigh') -> Optional[str]:
        """
        获取 QQ 音乐下载链接
        使用代理或直接 API
        """
        try:
            # 确定格式和文件类型
            quality_map = {
                'standard': ('M500', 'mp3'),
                'exhigh': ('M800', 'mp3'),
                'lossless': ('F000', 'flac'),
                'hires': ('RS01', 'flac'),
            }
            prefix, ext = quality_map.get(quality, ('M800', 'mp3'))
            filename = f"{prefix}{song_mid}.{ext}"
            
            # 使用代理 API (如果配置了)
            # 优先使用 /qq/download/ 端点（代理直接下载并流式返回，避免 CDN 地区限制）
            if self.proxy_url and self.proxy_key:
                try:
                    # 代理 API: /qq/download/<song_mid>?quality=X
                    # 直接返回音频流，无需二次下载
                    proxy_url = f"{self.proxy_url}/qq/download/{song_mid}"
                    proxy_headers = {
                        'X-API-Key': self.proxy_key,
                        'X-QQ-Cookie': '; '.join([f"{k}={v}" for k, v in self.session.cookies.items()]),
                    }
                    proxy_params = {'quality': quality}
                    
                    logger.info(f"调用代理流式下载: {proxy_url}, 音质={quality}")
                    
                    # 存储代理请求参数供 download_song 使用
                    self._last_proxy_request = {
                        'url': proxy_url,
                        'headers': proxy_headers,
                        'params': proxy_params,
                    }
                    
                    # 返回特殊标记
                    return "PROXY_STREAM"
                        
                except Exception as e:
                    logger.warning(f"代理 API 调用失败: {e}")
                    logger.warning("代理失败，回退到直连")
            
            # 直接使用 QQ 官方 VKey API
            uin = self._get_uin_from_cookie() or '0'
            g_tk = self._get_gtk()
            
            vkey_url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
            payload = {
                "comm": {"ct": 24, "cv": 0, "uin": uin},
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "guid": "1234567890",
                        "songmid": [song_mid],
                        "songtype": [0],
                        "uin": uin,
                        "loginflag": 1,
                        "platform": "20"
                    }
                }
            }
            
            resp = self.session.post(vkey_url, params={'g_tk': g_tk}, 
                                     data=json.dumps(payload), timeout=10)
            data = resp.json()
            
            midurlinfo = data.get('req_0', {}).get('data', {}).get('midurlinfo', [])
            if midurlinfo and midurlinfo[0].get('purl'):
                purl = midurlinfo[0]['purl']
                sip = data.get('req_0', {}).get('data', {}).get('sip', ['http://ws.stream.qqmusic.qq.com/'])[0]
                return f"{sip}{purl}"
            
            logger.warning(f"无法获取下载链接: {song_mid}")
            return None
            
        except Exception as e:
            logger.error(f"获取QQ下载链接失败: {e}")
            return None

    def download_song(self, song_mid: str, song_info: dict, output_dir: str, 
                      quality: str = 'exhigh') -> Optional[str]:
        """下载单首歌曲 - 带音质降级"""
        
        # 音质降级顺序
        quality_fallback = {
            'hires': ['hires', 'lossless', 'exhigh', 'standard'],
            'lossless': ['lossless', 'exhigh', 'standard'],
            'exhigh': ['exhigh', 'standard'],
            'standard': ['standard'],
        }
        
        qualities_to_try = quality_fallback.get(quality, ['exhigh', 'standard'])
        
        for try_quality in qualities_to_try:
            try:
                download_url = self.get_download_url(song_mid, try_quality)
                if not download_url:
                    logger.warning(f"无法获取 {try_quality} 下载链接, 尝试降级...")
                    continue
                
                # 检查是否是代理流式下载
                is_proxy_stream = (download_url == 'PROXY_STREAM')
                
                # 确定文件扩展名 (代理流式下载时从响应头获取)
                if is_proxy_stream:
                    ext = 'flac' if try_quality in ('lossless', 'hires') else 'mp3'
                else:
                    ext = 'flac' if 'flac' in download_url.lower() or try_quality in ('lossless', 'hires') else 'mp3'
                
                # 构建文件名
                title = song_info.get('title', song_mid)
                artist = song_info.get('artist', 'Unknown')
                safe_title = "".join(c for c in title if c not in r'\/:*?"<>|')
                safe_artist = "".join(c for c in artist if c not in r'\/:*?"<>|')
                filename = f"{safe_artist} - {safe_title}.{ext}"
                
                output_path = Path(output_dir) / filename
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                if is_proxy_stream:
                    # 代理流式下载 - 从实例变量获取参数
                    proxy_info = getattr(self, '_last_proxy_request', None)
                    if not proxy_info:
                        logger.error("代理请求参数丢失")
                        continue
                    
                    proxy_url = proxy_info['url']
                    proxy_headers = proxy_info['headers']
                    proxy_params = proxy_info['params']
                    
                    logger.info(f"使用代理流式下载 ({try_quality}): {proxy_url}")
                    resp = requests.get(proxy_url, headers=proxy_headers, params=proxy_params, timeout=120, stream=True)
                    
                    if resp.status_code == 404:
                        logger.warning(f"代理下载失败 404 ({try_quality}), 尝试降级...")
                        continue
                    elif resp.status_code != 200:
                        error_msg = "unknown"
                        try:
                            error_msg = resp.json().get('error', 'unknown')
                        except:
                            pass
                        logger.error(f"代理下载失败, HTTP {resp.status_code}: {error_msg}")
                        continue
                    
                    # 从响应头获取实际格式
                    actual_ext = resp.headers.get('X-File-Type', ext)
                    actual_quality = resp.headers.get('X-Quality', try_quality)
                    if actual_ext != ext:
                        filename = f"{safe_artist} - {safe_title}.{actual_ext}"
                        output_path = Path(output_dir) / filename
                        ext = actual_ext
                    
                    logger.info(f"代理返回: 格式={actual_ext}, 音质={actual_quality}")
                else:
                    # 直连下载 - QQ CDN 需要特定的 Headers
                    download_headers = {
                        'Referer': 'https://y.qq.com/',
                        'Origin': 'https://y.qq.com',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    }
                    
                    logger.info(f"尝试直连下载 ({try_quality}): {download_url[:80]}...")
                    resp = requests.get(download_url, headers=download_headers, timeout=60, stream=True)
                    
                    if resp.status_code == 404:
                        logger.warning(f"下载 404 ({try_quality}), 尝试降级...")
                        continue
                    elif resp.status_code != 200:
                        logger.error(f"下载失败, HTTP {resp.status_code}")
                        continue
                
                with open(output_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                logger.info(f"QQ音乐下载成功 ({try_quality}): {output_path}")
                
                # 尝试应用元数据
                try:
                    from bot.ncm_downloader import MusicAutoDownloader
                    downloader = MusicAutoDownloader()
                    downloader.apply_metadata_to_file(str(output_path), song_mid, 'qq')
                except Exception as e:
                    logger.warning(f"应用元数据失败: {e}")
                
                return str(output_path)
                
            except Exception as e:
                logger.error(f"下载失败 ({try_quality}): {e}")
                continue
        
        logger.error(f"所有音质都下载失败: {song_mid}")
        return None

    def batch_download(self, songs: List[dict], output_dir: str, 
                       quality: str = 'exhigh', progress_callback=None,
                       is_organize_mode: bool = False, organize_dir: str = None) -> Tuple[List[str], List[dict]]:
        """
        批量下载歌曲
        
        Args:
            songs: 歌曲列表 [{'source_id': ..., 'title': ..., 'artist': ...}, ...]
            output_dir: 输出目录
            quality: 音质
            progress_callback: 进度回调函数 (current, total, song_info)
            is_organize_mode: 是否整理模式
            organize_dir: 整理目录
            
        Returns:
            (成功下载的文件列表, 失败的歌曲列表)
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        success_files = []
        failed_songs = []
        
        total = len(songs)
        for i, song in enumerate(songs):
            song_mid = song.get('source_id')
            if not song_mid:
                failed_songs.append(song)
                continue
            
            if progress_callback:
                progress_callback(i + 1, total, song)
            
            # 确定输出目录
            if is_organize_mode and organize_dir:
                artist = song.get('artist', 'Unknown')
                album = song.get('album', 'Unknown')
                safe_artist = "".join(c for c in artist if c not in r'\/:*?"<>|')
                safe_album = "".join(c for c in album if c not in r'\/:*?"<>|')
                target_dir = Path(organize_dir) / safe_artist / safe_album
            else:
                target_dir = output_dir
            
            result = self.download_song(song_mid, song, str(target_dir), quality)
            if result:
                success_files.append(result)
            else:
                failed_songs.append(song)
            
            # 避免请求过快
            time.sleep(0.5)
        
        return success_files, failed_songs

    def refresh_cookie(self) -> Tuple[bool, dict]:
        """
        刷新 Cookie
        通过访问用户个人主页或鉴权接口触发 Set-Cookie
        """
        try:
            # 尝试访问用户主页 API，通常会触发 cookie 刷新
            # 这是一个不需要特殊参数的通用接口
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
            payload = {
                "comm": {"ct": 24, "cv": 0},
                "req": {
                    "method": "get_global_config",
                    "module": "music.pf_song_detail_svr",
                    "param": {}
                }
            }
            # 需要加上 g_tk，虽然有时候不需要
            g_tk = self._get_gtk()
            params = {'g_tk': g_tk}
            
            resp = self.session.post(url, params=params, data=json.dumps(payload), timeout=10)
            
            # 检查是否有 Set-Cookie
            new_cookies = {}
            has_new = False
            
            # 手动合并 session 中的 cookie
            for cookie in self.session.cookies:
                if cookie.name in ['qqmusic_key', 'qm_keyst', 'uin', 'euin']:
                    new_cookies[cookie.name] = cookie.value
                    
            # 转换成字典返回
            return True, {
                'message': '刷新成功',
                'cookies': new_cookies,
                'musickey': new_cookies.get('qqmusic_key') or new_cookies.get('qm_keyst')
            }
            
        except Exception as e:
            logger.error(f"QQ Cookie 刷新失败: {e}")
            return False, {'error': str(e)}

    def check_login(self) -> Tuple[bool, dict]:
        """检查登录状态"""
        try:
            # 1. 尝试从 Cookie 获取 UIN
            uin = self._get_uin_from_cookie()
            if not uin:
                logger.warning(f"CheckLogin: 未找到 UIN. Cookies: {self.session.cookies.keys()}")
                return False, {'message': 'Cookie 中未包含 uin'}
                
            # 2. 调用 API 验证并获取昵称
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
            payload = {
                "comm": {"ct": 24, "cv": 0},
                "req": {
                    "module": "music.pf_q_profile_svr",
                    "method": "get_profile",
                    "param": {
                        "q_uin": str(uin),
                        "uin": str(uin)
                    }
                }
            }
            g_tk = self._get_gtk()
            params = {'g_tk': g_tk}
            
            resp = self.session.post(url, params=params, data=json.dumps(payload), timeout=10)
            data = resp.json()
            
            code = data.get('code')
            req_data = data.get('req', {}).get('data', {})
            
            if code == 0 and req_data:
                creator = req_data.get('creator', {})
                nickname = creator.get('nick', str(uin))
                vip_type = creator.get('vip_type', 0)
                return True, {
                    'nickname': nickname,
                    'uin': uin,
                    'is_vip': vip_type > 0,
                    'vip_type': vip_type
                }
            else:
                logger.warning(f"QQ check_login get_profile failed (Code {code}), trying fallback...")
                
                # Fallback: 尝试获取用户歌单列表
                # 这个接口通常权限要求更低
                payload['req'] = {
                    "module": "music.song_list_server",
                    "method": "get_u_songlist",
                    "param": {
                        "uin": str(uin)
                    }
                }
                resp = self.session.post(url, params=params, data=json.dumps(payload), timeout=10)
                data = resp.json()
                req_data = data.get('req', {}).get('data', {})
                code = data.get('code')
                
                if code == 0 and req_data:
                    # 成功获取歌单，说明登录有效
                    # 尝试从歌单信息里找昵称，找不到就用 UIN
                    msg = "QQ User"
                    if req_data.get('list'):
                         msg = req_data['list'][0].get('nick', str(uin))
                         
                    return True, {
                        'nickname': msg,
                        'uin': uin,
                        'is_vip': False, # 无法判断，默认False
                        'vip_type': 0
                    }
                
                logger.warning(f"QQ check_login fallback 1 failed (Code {code}), trying fallback 2...")
                
                # Fallback 2: 尝试获取全局配置 (最宽松的接口)
                payload['req'] = {
                    "method": "get_global_config",
                    "module": "music.pf_song_detail_svr",
                    "param": {}
                }
                resp = self.session.post(url, params=params, data=json.dumps(payload), timeout=10)
                data = resp.json()
                req_data = data.get('req', {}).get('data', {})
                code = data.get('code')
                
                if code == 0:
                    # 至少说明 Cookie 是能通的
                    return True, {
                        'nickname': f'QQ User ({uin})',
                        'uin': uin,
                        'is_vip': False,
                        'vip_type': 0
                    }

                logger.warning(f"QQ check_login failed. UIN={uin}, gtk={g_tk}, Code={code}, Resp={data}")
                return False, {'message': f'Cookie 验证失败 (Code {code})'}
                
        except Exception as e:
            logger.error(f"QQ check_login error: {e}")
            return False, {'error': str(e)}

    def _get_uin_from_cookie(self) -> Optional[str]:
        """从 Cookie 提取 uin"""
        uin = self.session.cookies.get('uin') or self.session.cookies.get('euin') or \
              self.session.cookies.get('wxuin') or self.session.cookies.get('qm_keyst')
        
        # 处理 o0123456 格式
        if uin and uin.startswith('o'):
            uin = uin[1:]
        return uin

    def _get_gtk(self):
        """计算 g_tk"""
        hash_str = 5381
        p_skey = self.session.cookies.get('p_skey') or \
                 self.session.cookies.get('skey') or \
                 self.session.cookies.get('qqmusic_key') or \
                 ''
        for c in p_skey:
            hash_str += (hash_str << 5) + ord(c)
        return hash_str & 0x7fffffff



class MusicAutoDownloader:
    """自动下载补全模块"""
    
    def __init__(self, ncm_cookie: str = None, qq_cookie: str = None, download_dir: str = None, 
                 proxy_url: str = None, proxy_key: str = None):
        self.ncm_api = NeteaseMusicAPI(ncm_cookie) if ncm_cookie else None
        self.qq_cookie = qq_cookie  # 保存 QQ Cookie 供后续使用
        self.download_dir = Path(download_dir) if download_dir else Path('/tmp/music_downloads')
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_url = proxy_url
        self.proxy_key = proxy_key
        
        # 初始化 HTTP Session (用于歌词获取等)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
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
                                progress_callback=None,
                                is_organize_mode: bool = False,
                                organize_dir: str = None,
                                fallback_to_qq: bool = False) -> Tuple[List[str], List[dict]]:
        """
        下载缺失的歌曲
        
        Args:
            missing_songs: 缺失歌曲列表（必须是网易云的歌曲）
            quality: 下载音质
            progress_callback: 进度回调
            is_organize_mode: 是否自动整理模式
            organize_dir: 整理目标目录
            fallback_to_qq: 是否回退到 QQ 音乐（暂不支持）
            
        Returns:
            (成功下载的结果列表, 失败的歌曲列表)
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

    def _get_qq_api(self):
        """延迟初始化 QQ API"""
        if not hasattr(self, 'qq_api') or not self.qq_api:
            if self.qq_cookie:
                from bot.ncm_downloader import QQMusicAPI
                self.qq_api = QQMusicAPI(self.qq_cookie)
                # 传递代理配置
                self.qq_api.proxy_url = self.proxy_url
                self.qq_api.proxy_key = self.proxy_key
            else:
                self.qq_api = None
        return self.qq_api

    def download_missing_songs(self, missing_songs: List[dict], 
                                quality: str = 'exhigh',
                                progress_callback=None,
                                is_organize_mode: bool = False,
                                organize_dir: str = None,
                                fallback_to_qq: bool = False) -> Tuple[List[str], List[dict]]:
        """
        下载缺失的歌曲
        """
        success_files = []
        failed_songs = []
        
        # 1. 尝试网易云下载
        ncm_songs = [s for s in missing_songs if s.get('platform') == 'NCM' or not s.get('platform')]
        qq_songs = [s for s in missing_songs if s.get('platform') == 'QQ']
        
        if self.ncm_api:
            # 检查登录状态
            self.ncm_api.check_login()
            
            if ncm_songs:
                logger.info(f"开始下载 {len(ncm_songs)} 首网易云歌曲")
                # 批量下载 - 如果下载失败，batch_download 会返回在 failed_list 中
                # 我们需要修改 batch_download 还是在这里处理？
                # NeteaseMusicAPI.batch_download 返回 (success_files, failed_songs)
                
                s_files, f_songs = self.ncm_api.batch_download(
                    ncm_songs, 
                    str(self.download_dir), 
                    quality,
                    progress_callback,
                    is_organize_mode, 
                    organize_dir
                )
                success_files.extend(s_files)
                
                # 如果开启了回退，将失败的 NCM 歌曲标记为需要尝试 QQ
                if fallback_to_qq:
                    for song in f_songs:
                        song['_try_fallback'] = True
                    ncm_failed = f_songs
                else:
                    failed_songs.extend(f_songs)
            else:
                ncm_failed = []
        else:
            # 没有 NCM API，所有 NCM 歌曲都算失败
            if fallback_to_qq:
                for song in ncm_songs:
                    song['_try_fallback'] = True
                ncm_failed = ncm_songs
            else:
                failed_songs.extend(ncm_songs)
                ncm_failed = []

        # 2. 处理 QQ 歌曲 (无论是原本就是 QQ 的，还是 NCM 失败回退的)
        qq_api = self._get_qq_api()
        final_qq_tasks = []
        
        # 原本就是 QQ 的歌曲
        if qq_songs:
            final_qq_tasks.extend(qq_songs)
            
        # NCM 失败回退逻辑
        if fallback_to_qq and ncm_failed and qq_api:
            logger.info(f"尝试对 {len(ncm_failed)} 首失败歌曲进行跨平台(QQ)搜索匹配...")
            
            for song in ncm_failed:
                try:
                    title = song.get('title', '')
                    artist = song.get('artist', '')
                    album = song.get('album', '')
                    
                    if not title:
                        failed_songs.append(song)
                        continue
                        
                    # 搜索关键词
                    keyword = f"{title} {artist}"
                    # 搜索
                    results = qq_api.search_song(keyword, limit=5)
                    
                    if not results:
                        logger.info(f"QQ搜索未找到: {keyword}")
                        song['error_message'] = "跨平台搜索未找到"
                        failed_songs.append(song)
                        continue
                        
                    # 匹配逻辑 (模糊匹配)
                    best_match = None
                    
                    # 预处理目标信息
                    t_target = title.replace(' ', '').lower()
                    a_target = artist.replace(' ', '').lower() if artist else ''
                    al_target = album.replace(' ', '').lower() if album else ''
                    
                    for item in results:
                        # 预处理候选信息
                        t_cand = item['title'].replace(' ', '').lower()
                        a_cand = item['artist'].replace(' ', '').lower()
                        al_cand = item['album'].replace(' ', '').lower() if item.get('album') else ''
                        
                        # 1. 标题必须包含或相等
                        if t_target not in t_cand and t_cand not in t_target:
                            continue
                            
                        # 2. 歌手必须有重叠 (因为可能有多个歌手)
                        if a_target and a_cand:
                            # 简单检查：某一方包含另一方
                            if a_target not in a_cand and a_cand not in a_target:
                                continue
                        
                        # 3. 专辑匹配 (精确到专辑)
                        # 用户要求：精确到专辑。如果原歌曲有专辑信息，则必须匹配专辑。
                        if al_target: 
                            if not al_cand: # 候选无专辑，跳过
                                continue
                            if al_target not in al_cand and al_cand not in al_target:
                                # 专辑名不匹配
                                continue
                        
                        # 找到匹配
                        best_match = item
                        break
                    
                    if best_match:
                        logger.info(f"跨平台匹配成功: [{title}] -> [{best_match['title']}] (Album: {best_match['album']})")
                        # 构造新的任务对象，保留必要的元数据
                        new_task = best_match.copy()
                        new_task['platform'] = 'QQ'
                        
                        # 尽量保留原标题/歌手/专辑名，以便整理文件时一致？
                        # 不，下载后元数据应该是实际文件的。
                        # 但为了列表显示一致，可以保留 original_title
                        
                        final_qq_tasks.append(new_task)
                    else:
                        logger.info(f"跨平台搜索有结果但未通过专辑/歌手验证: {title} - {artist} (Album: {album})")
                        song['error_message'] = "未找到同名同专辑资源"
                        failed_songs.append(song)
                        
                except Exception as e:
                    logger.error(f"跨平台处理失败 {song.get('title')}: {e}")
                    failed_songs.append(song)
        elif fallback_to_qq and ncm_failed and not qq_api:
            logger.warning("需要回退但未配置 QQ Cookie")
            failed_songs.extend(ncm_failed)
        elif not fallback_to_qq and ncm_failed:
            # 已经加到 failed_songs 里了吗？上面 logic 稍微有点乱，整理一下
            # ncm_failed 是所有失败的。如果 fallback=False，它们已经在 line 1500+被处理了吗？
            # 修正：上面 fallback=False 时，ncm_failed extend 到了 failed_songs
            pass

        # 3. 执行 QQ 下载
        if final_qq_tasks and qq_api:
            logger.info(f"开始下载 {len(final_qq_tasks)} 首 QQ 音乐歌曲")
            s_files, f_songs = qq_api.batch_download(
                final_qq_tasks,
                str(self.download_dir),
                quality,
                progress_callback,
                is_organize_mode,
                organize_dir
            )
            success_files.extend(s_files)
            failed_songs.extend(f_songs)
        
        return success_files, failed_songs

    def search_qq(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索 QQ 音乐"""
        try:
            url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                'w': keyword,
                'n': limit,
                'page': 1,
                'cr': 1,
                'new_json': 1,
                'format': 'json',
                'platform': 'yqq.json'
            }
            headers = {
                'Referer': 'https://y.qq.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = self.session.get(url, params=params, headers=headers, timeout=10)
            data = resp.json()
            
            results = []
            if data.get('code') == 0:
                for song in data.get('data', {}).get('song', {}).get('list', []):
                    # 获取专辑封面
                    album_mid = song.get('album', {}).get('mid', '')
                    cover_url = ""
                    if album_mid:
                        cover_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                        
                    results.append({
                        'source_id': str(song.get('mid', '')),  # 使用 mid 作为 ID
                        'title': song.get('name', ''),
                        'artist': '/'.join([a.get('name', '') for a in song.get('singer', [])]),
                        'album': song.get('album', {}).get('name', ''),
                        'cover_url': cover_url,
                        'platform': 'QQ'
                    })
            return results
        except Exception as e:
            logger.error(f"QQ音乐搜索失败: {e}")
            return []

    def get_qq_lyrics(self, songmid: str) -> Optional[str]:
        """获取 QQ 音乐歌词"""
        try:
            url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
            params = {
                'songmid': songmid,
                'g_tk': 5381,
                'loginUin': 0,
                'hostUin': 0,
                'format': 'json',
                'inCharset': 'utf8',
                'outCharset': 'utf-8',
                'notice': 0,
                'platform': 'yqq.json',
                'needNewCode': 0
            }
            headers = {'Referer': 'https://y.qq.com/'}
            resp = self.session.get(url, params=params, headers=headers, timeout=10)
            data = resp.json()
            
            if data.get('code') == 0:
                lyric = data.get('lyric', '')
                if lyric:
                    import base64
                    return base64.b64decode(lyric).decode('utf-8')
            return None
        except Exception as e:
            logger.warning(f"获取QQ歌词失败: {e}")
            return None

    def apply_metadata_to_file(self, file_path: str, song_id: str, source: str = 'ncm') -> Tuple[bool, str]:
        """
        手动为现有的本地文件应用元数据
        source: 'ncm' 或 'qq'
        """
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                logger.error(f"文件不存在: {file_path}")
                return False, "文件不存在"

            song_info = {}
            cover_data = None
            lyrics = None

            if source in ('ncm', 'netease'):
                if not self.ncm_api:
                    logger.error("未初始化 NCM API")
                    return False, "未初始化 NCM API"
                # 1. 获取歌曲详情
                info = self.ncm_api.get_song_detail(song_id)
                if not info:
                    logger.error(f"无法获取歌曲详情: {song_id}")
                    return False, "无法获取歌曲详情"
                song_info = info
                
                # 2. 获取封面图片
                if song_info.get('coverUrl'):
                    try:
                        resp = self.ncm_api.session.get(song_info['coverUrl'], timeout=10)
                        if resp.status_code == 200:
                            cover_data = resp.content
                    except Exception as e:
                        logger.warning(f"下载封面失败: {e}")
                
                # 3. 获取歌词
                lyrics = self.ncm_api.get_lyrics(song_id)
                print(f"[ApplyMetadata] 歌词获取结果: {bool(lyrics)}, 长度: {len(lyrics) if lyrics else 0}", flush=True)
                
                # 4. 获取音轨号（通过专辑 API）
                track_number = ''
                album_id = song_info.get('album_id', '')
                if album_id:
                    try:
                        album_songs = self.ncm_api.get_album_songs(album_id)
                        for idx, s in enumerate(album_songs, 1):
                            if s.get('source_id') == song_id:
                                track_number = str(idx)
                                break
                        print(f"[ApplyMetadata] 音轨号: {track_number}/{len(album_songs)}", flush=True)
                    except Exception as e:
                        logger.warning(f"获取音轨号失败: {e}")
                song_info['track_number'] = track_number

            elif source == 'qq':
                # QQ 音乐逻辑 (需要先搜索或直接构建)
                # 由于 QQ 没有简单的 get_song_detail (需要 token), 我们假设 song_id 是 mid
                # 并重新搜索一次获取详情 (或者期望调用者 passing info, 但这里接口限制)
                # 简化起见，我们重新搜索一下 ID 确认信息? 不，太慢。
                # Hack: 我们无法仅凭 mid 获取详情，除非用 VKey API。
                # 但 Search Result 已经包含了所有信息.
                # 问题是这里参数只有 song_id.
                # 我们只能尝试用 song_id (mid) 去搜索 (QQ 支持搜 ID?) 不支持。
                # 替代方案：调用者必须传递 info... 但接口签名定死了。
                # 妥协：我们再次搜索 song_id 作为 keyword? 
                # 或者，我们假设调用者把 info 序列化在 song_id 里? 不优雅。
                # 最好是：我们用 'mobile' detail api?
                # https://u.y.qq.com/cgi-bin/musicu.fcg?data={"songinfo":{"method":"get_song_detail_yqq","param":{"song_type":0,"song_mid":"MID","song_id":ID},"module":"music.pf_song_detail_svr"}}
                
                try:
                    detail_url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
                    payload = {
                        "comm": {"ct": 24, "cv": 0},
                        "songinfo": {
                            "method": "get_song_detail_yqq",
                            "param": {"song_type": 0, "song_mid": song_id},
                            "module": "music.pf_song_detail_svr"
                        }
                    }
                    resp = requests.post(detail_url, data=json.dumps(payload), timeout=10)
                    dt = resp.json()
                    track = dt.get('songinfo', {}).get('data', {}).get('track_info', {})
                    if track and track.get('name'):
                        # 获取专辑信息
                        album_info = track.get('album', {})
                        # 发布时间 (格式: 2023-01-01)
                        pub_time = track.get('time_public', '')
                        year = pub_time[:4] if pub_time and len(pub_time) >= 4 else ''
                        # 音轨号
                        track_index = track.get('index_album', 0)
                        # 歌手列表
                        singers = [s.get('name') for s in track.get('singer', []) if s.get('name')]
                        
                        song_info = {
                            'title': track.get('name'),
                            'artist': '/'.join(singers),
                            'album': album_info.get('name', ''),
                            'album_artist': singers[0] if singers else '',
                            'year': year,
                            'track_number': str(track_index) if track_index else '',
                            'coverUrl': f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_info.get('mid')}.jpg"
                        }
                    else:
                        logger.error("无法获取 QQ 歌曲详情")
                        return False, "无法获取 QQ 歌曲详情"
                except Exception as e:
                    logger.error(f"获取QQ详情失败: {e}")
                    return False, f"获取QQ详情失败: {e}"
                
                # 获取封面
                if song_info.get('coverUrl'):
                    try:
                        resp = requests.get(song_info['coverUrl'], timeout=10)
                        if resp.status_code == 200:
                            cover_data = resp.content
                    except:
                        pass
                
                # 获取歌词
                lyrics = self.get_qq_lyrics(song_id)
            
            # --- 通用写入逻辑 ---
            
            # 写入歌词
            if lyrics:
                lrc_path = file_path.with_suffix('.lrc')
                try:
                    with open(lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics)
                    print(f"[ApplyMetadata] 歌词已写入: {lrc_path}", flush=True)
                except Exception as e:
                    print(f"[ApplyMetadata] 写入歌词失败: {e}", flush=True)
                    logger.warning(f"写入歌词失败: {e}")
            else:
                print(f"[ApplyMetadata] 无歌词可写入", flush=True)

            # 写入元数据
            # artist 格式需要是 [[name1], [name2], ...] 结构以匹配 NCM 原始格式
            # 如果 artist 是 "A/B/C"，需要拆分成 [[A], [B], [C]]
            artist_str = song_info.get('artist', '')
            artist_list = [[a.strip()] for a in artist_str.split('/')] if artist_str else []
            
            # 计算年份 (优先使用已有的 year 字段如 QQ 音乐，否则从时间戳计算)
            year = song_info.get('year', '')
            if not year:
                publish_time = song_info.get('publish_time', 0)
                if publish_time:
                    try:
                        from datetime import datetime
                        year = str(datetime.fromtimestamp(publish_time / 1000).year)
                    except:
                        pass
            
            metadata = {
                'musicName': song_info.get('title', ''),
                'artist': artist_list,
                'album': song_info.get('album', ''),
                'albumartist': song_info.get('album_artist', ''),
                'year': year,
                'track': song_info.get('track_number', ''),  # 音轨号
                'lyrics': lyrics  # 内嵌歌词
            }
            
            # Determine format
            import mimetypes
            mime, _ = mimetypes.guess_type(file_path)
            if mime == 'audio/flac' or file_path.suffix.lower() == '.flac':
                fmt = 'flac'
            else:
                fmt = 'mp3'

            print(f"[ApplyMetadata] 准备写入元数据, file={file_path}, fmt={fmt}, metadata={metadata}", flush=True)
            NCMDecryptor._write_metadata(str(file_path), metadata, cover_data, fmt)
            print(f"[ApplyMetadata] 元数据写入完成", flush=True)
            logger.info(f"手动修改元数据成功 ({source}): {file_path}")
            return True, "成功"

        except Exception as e:
            print(f"[ApplyMetadata] 异常: {e}", flush=True)
            logger.error(f"手动修改元数据异常: {e}")
            return False, f"异常: {e}"

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
