#!/usr/bin/env python3
"""
文件整理模块 - 类似 MusicTag 的自动整理功能
功能：
1. 目录监控：监控指定目录，新文件自动处理
2. 可配置目录模板：支持多种变量组合
3. 元数据读取：从音频文件读取标签用于整理
"""

import os
import re
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# 支持的音频格式
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.ape', '.wma', '.aiff', '.dsf', '.dff'}

# 目录模板变量
TEMPLATE_VARIABLES = {
    'artist': '艺术家',
    'album_artist': '专辑艺术家',
    'album': '专辑',
    'title': '标题',
    'year': '年份',
    'genre': '风格',
    'track': '音轨号',
    'disc': '光盘编号',
}


@dataclass
class AudioMetadata:
    """音频元数据"""
    title: str = ''
    artist: str = ''
    album_artist: str = ''
    album: str = ''
    year: str = ''
    genre: str = ''
    track: str = ''
    disc: str = ''
    
    def get(self, key: str, default: str = '') -> str:
        """获取属性值"""
        value = getattr(self, key, default) or default
        return self._clean_path_component(value)
    
    @staticmethod
    def _clean_path_component(value: str) -> str:
        """清理路径组件中的非法字符"""
        if not value:
            return ''
        
        # 1. 先把路径分隔符 / 替换为可读的逗号（针对多艺术家情况 "A/B" -> "A, B"）
        value = value.replace('/', ', ').replace('\\', ', ')
        
        # 2. 移除其他非法字符
        value = re.sub(r'[<>:"|?*]', '_', value)
        
        # 3. 移除首尾空格和点
        value = value.strip(' .')
        
        # 4. 限制长度
        if len(value) > 100:
            value = value[:100]
        return value or 'Unknown'


def read_audio_metadata(file_path: str) -> Optional[AudioMetadata]:
    """
    读取音频文件元数据
    
    Args:
        file_path: 文件路径
        
    Returns:
        AudioMetadata 对象，失败返回 None
    """
    try:
        from mutagen import File
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.id3 import ID3
        
        audio = File(file_path, easy=True)
        if audio is None:
            logger.warning(f"[Metadata] mutagen 无法打开文件: {file_path}")
            return None
        
        metadata = AudioMetadata()
        
        # 尝试读取各种标签
        def get_tag(keys: List[str]) -> str:
            for key in keys:
                value = audio.get(key)
                if value:
                    return value[0] if isinstance(value, list) else str(value)
            return ''
        
        metadata.title = get_tag(['title', 'TIT2'])
        metadata.artist = get_tag(['artist', 'TPE1'])
        metadata.album_artist = get_tag(['albumartist', 'album artist', 'TPE2'])
        metadata.album = get_tag(['album', 'TALB'])
        metadata.year = get_tag(['date', 'year', 'TDRC', 'TYER'])[:4]  # 只取年份部分
        metadata.genre = get_tag(['genre', 'TCON'])
        
        # 音轨号
        track = get_tag(['tracknumber', 'TRCK'])
        if track:
            # 处理 "1/10" 格式
            metadata.track = track.split('/')[0].zfill(2)
        
        # 光盘编号
        disc = get_tag(['discnumber', 'TPOS'])
        if disc:
            metadata.disc = disc.split('/')[0]
        
        # 优化 Album Artist 逻辑：始终为了文件夹层级提取主要艺术家 (Graphic 1 风格)
        # 如果 album_artist 字段存在且包含分隔符，也强制分割取第一个
        raw_album_artist = metadata.album_artist or metadata.artist
        if raw_album_artist:
             metadata.album_artist = re.split(r'[ /;&,]', raw_album_artist)[0].strip()
        
        # 调试日志：记录关键字段为空的情况
        if not metadata.artist or not metadata.album:
            import os
            filename = os.path.basename(file_path)
            print(f"[Metadata] 元数据不完整: {filename} - artist='{metadata.artist}', album='{metadata.album}'")
        
        return metadata
        
    except Exception as e:
        logger.warning(f"读取元数据失败 {file_path}: {e}")
        return None


def extract_cover_art(file_path: str, output_dir: str, filename: str = "cover.jpg") -> Optional[str]:
    """
    从音频文件提取内嵌封面并保存
    
    Args:
        file_path: 音频文件路径
        output_dir: 输出目录
        filename: 封面文件名
        
    Returns:
        保存的封面路径，无封面或失败返回 None
    """
    try:
        from mutagen import File
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.id3 import ID3
        
        output_path = Path(output_dir) / filename
        
        # 如果已有封面，跳过
        if output_path.exists():
            return str(output_path)
        
        audio = File(file_path)
        if audio is None:
            return None
        
        cover_data = None
        
        # FLAC
        if isinstance(audio, FLAC):
            if audio.pictures:
                cover_data = audio.pictures[0].data
        
        # MP3 (ID3)
        elif hasattr(audio, 'tags') and audio.tags:
            # 检查 APIC 帧 (封面)
            for key in audio.tags.keys():
                if key.startswith('APIC'):
                    cover_data = audio.tags[key].data
                    break
        
        # MP4/M4A
        elif isinstance(audio, MP4):
            if 'covr' in audio.tags:
                covers = audio.tags['covr']
                if covers:
                    cover_data = bytes(covers[0])
        
        # 保存封面
        if cover_data:
            with open(output_path, 'wb') as f:
                f.write(cover_data)
            logger.info(f"   🖼️ 已提取封面: {filename}")
            return str(output_path)
        
        return None
        
    except Exception as e:
        logger.debug(f"提取封面失败 {file_path}: {e}")
        return None


def parse_template(template: str, metadata: AudioMetadata) -> str:
    """
    解析目录模板
    
    Args:
        template: 目录模板，如 "{album_artist}/{album}"
        metadata: 音频元数据
        
    Returns:
        解析后的路径
    """
    result = template
    
    for var, desc in TEMPLATE_VARIABLES.items():
        placeholder = f"{{{var}}}"
        if placeholder in result:
            value = metadata.get(var, 'Unknown')
            result = result.replace(placeholder, value)
    
    # 清理多余的分隔符
    result = re.sub(r'/+', '/', result)
    result = result.strip('/')
    
    return result


def organize_file(file_path: str, target_dir: str, template: str = "{album_artist}/{album}",
                  move: bool = True, on_conflict: str = 'skip') -> Optional[str]:
    """
    整理单个文件
    
    Args:
        file_path: 源文件路径
        target_dir: 目标目录
        template: 目录模板
        move: True 移动，False 复制
        on_conflict: 冲突处理 skip/overwrite/rename
        
    Returns:
        整理后的文件路径，失败返回 None
    """
    try:
        file_path = Path(file_path)
        target_dir = Path(target_dir)
        
        # ⚠️ 防御性检查：防止目标目录被配置为模板字符串
        target_dir_str = str(target_dir)
        if '{' in target_dir_str or '}' in target_dir_str:
            logger.error(f"❌ 整理目标目录配置错误: '{target_dir_str}' 包含模板变量。请在设置中修正为绝对路径 (如 /music)")
            return None
            
        if not file_path.exists():
            logger.debug(f"文件不存在（可能已被处理）: {file_path}")
            return None
        
        # 检查是否是音频文件
        if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
            logger.debug(f"跳过非音频文件: {file_path}")
            return None
        
        # 读取元数据
        metadata = read_audio_metadata(str(file_path))
        if not metadata:
            logger.warning(f"无法读取元数据: {file_path}")
            # 使用 Unknown 便于用户找到问题文件
            metadata = AudioMetadata(
                title=file_path.stem,
                artist='Unknown',
                album_artist='Unknown',
                album='Unknown'
            )
        
        # 解析目录模板
        relative_dir = parse_template(template, metadata)
        target_subdir = target_dir / relative_dir
        target_subdir.mkdir(parents=True, exist_ok=True)
        
        # 生成目标文件名
        target_path = target_subdir / file_path.name
        
        # 处理冲突
        if target_path.exists():
            if on_conflict == 'skip':
                logger.info(f"文件已存在，跳过: {relative_dir}/{file_path.name}")
                if move:
                    file_path.unlink()
                return str(target_path)
            elif on_conflict == 'overwrite':
                target_path.unlink()
            elif on_conflict == 'rename':
                # 添加序号
                base = target_path.stem
                ext = target_path.suffix
                counter = 1
                while target_path.exists():
                    target_path = target_subdir / f"{base} ({counter}){ext}"
                    counter += 1
        
        # 移动或复制
        if move:
            try:
                # 再次确保目标目录存在 (防御性：解决跨文件系统或Docker卷特殊情况)
                parent_dir = target_path.parent
                if not parent_dir.exists():
                    logger.info(f"创建目标目录: {parent_dir}")
                    parent_dir.mkdir(parents=True, exist_ok=True)
                    
                # 验证目录是否创建成功
                if not parent_dir.exists():
                    logger.error(f"目录创建失败 (mkdir 后仍不存在): {parent_dir}")
                    # 检查父路径是否存在
                    check_path = parent_dir
                    while check_path != Path('/'):
                        if check_path.exists():
                            logger.error(f"  存在的最深路径: {check_path}")
                            break
                        check_path = check_path.parent
                    return None
                    
                # 尝试直接移动
                shutil.move(str(file_path), str(target_path))
            except OSError as e:
                logger.warning(f"移动文件失败 ({e})，尝试复制模式...")
                try:
                    # 跨文件系统/失败时，使用简单复制 (不保留元数据以避免网盘兼容问题)
                    shutil.copy(str(file_path), str(target_path))
                    if target_path.exists() and target_path.stat().st_size > 0:
                        file_path.unlink()
                except Exception as copy_e:
                    logger.error(f"复制也失败: {copy_e}")
                    return None
            logger.info(f"✅ 整理完成: {file_path.name}")
        else:
            # 复制模式
            shutil.copy(str(file_path), str(target_path))
            logger.info(f"✅ 整理完成: {file_path.name}")
            
        logger.info(f"   📂 {relative_dir}/{file_path.name}")
        
        # 提取封面图片（如果目录中没有的话）
        cover_path = extract_cover_art(str(target_path), str(target_subdir))
        
        # 如果没有提取到封面，尝试在线搜索
        if not cover_path:
            cover_file = target_subdir / "cover.jpg"
            if not cover_file.exists():
                try:
                    search_cover_online(
                        artist=metadata.get('artist', ''),
                        album=metadata.get('album', ''),
                        title=metadata.get('title', ''),
                        output_path=str(cover_file)
                    )
                except Exception as e:
                    logger.debug(f"在线搜索封面失败: {e}")
        
        # 确保艺术家目录有头像（艺术家目录是专辑目录的上一级）
        artist_dir = target_subdir.parent
        artist_name = metadata.get('album_artist') or metadata.get('artist', '')
        if artist_name and artist_dir != target_dir:  # 确保不是根目录
            try:
                ensure_artist_photo(str(artist_dir), artist_name)
            except Exception as e:
                logger.debug(f"补全艺术家头像失败: {e}")
        
        return str(target_path)
        
    except Exception as e:
        logger.error(f"整理文件失败 {file_path}: {e}")
        return None


class DirectoryWatcher:
    """
    目录监控器 - 监控目录变化并自动整理
    """
    
    def __init__(self, watch_dir: str, target_dir: str, template: str = "{album_artist}/{album}",
                 on_conflict: str = 'skip', poll_interval: float = 5.0):
        """
        初始化目录监控器
        
        Args:
            watch_dir: 监控目录
            target_dir: 目标目录
            template: 目录模板
            on_conflict: 冲突处理
            poll_interval: 轮询间隔（秒）
        """
        self.watch_dir = Path(watch_dir)
        self.target_dir = Path(target_dir)
        self.template = template
        self.on_conflict = on_conflict
        self.poll_interval = poll_interval
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._processed_files: set = set()
        self._callbacks: List[Callable] = []
        
        # 统计
        self.stats = {
            'total_processed': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'last_processed': None
        }
    
    def add_callback(self, callback: Callable):
        """添加处理完成回调"""
        self._callbacks.append(callback)
    
    def start(self):
        """启动监控"""
        if self._running:
            return
        
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        
        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        
        # 美化启动日志
        logger.info("="*50)
        logger.info("📁 文件整理器已启动")
        logger.info(f"   监控目录: {self.watch_dir}")
        logger.info(f"   目标目录: {self.target_dir}")
        logger.info(f"   整理模板: {self.template}")
        logger.info(f"   冲突处理: {self.on_conflict}")
        logger.info("="*50)
    
    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("📁 文件整理器已停止")
    
    def _watch_loop(self):
        """监控循环"""
        while self._running:
            try:
                self._scan_directory()
            except Exception as e:
                logger.error(f"监控循环出错: {e}")
            
            time.sleep(self.poll_interval)
    
    def _scan_directory(self):
        """扫描目录（递归扫描所有子目录）"""
        if not self.watch_dir.exists():
            return
        
        # 递归扫描所有文件
        for file_path in self.watch_dir.rglob('*'):
            if not file_path.is_file():
                continue
            
            # 检查文件扩展名
            if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            
            # 检查是否已处理（使用文件路径 + 修改时间）
            file_key = f"{file_path}:{file_path.stat().st_mtime}"
            if file_key in self._processed_files:
                continue
            
            # 等待文件写入完成（检查文件大小是否稳定）
            if not self._is_file_ready(file_path):
                continue
            
            # 再次检查文件是否存在（可能被其他进程处理了）
            if not file_path.exists():
                continue
            
            # 处理文件
            self._processed_files.add(file_key)
            self.stats['total_processed'] += 1
            
            result = organize_file(
                str(file_path), 
                str(self.target_dir), 
                self.template,
                move=True,
                on_conflict=self.on_conflict
            )
            
            if result:
                self.stats['success'] += 1
                self.stats['last_processed'] = datetime.now().isoformat()
                
                # 删除空的父目录（清理整理后留下的空文件夹）
                try:
                    parent = file_path.parent
                    while parent != self.watch_dir and parent.exists():
                        if not any(parent.iterdir()):  # 目录为空
                            parent.rmdir()
                            logger.info(f"删除空目录: {parent}")
                            parent = parent.parent
                        else:
                            break
                except Exception as e:
                    pass  # 删除空目录失败不影响主流程
                
                # 触发回调
                for callback in self._callbacks:
                    try:
                        callback(str(file_path), result)
                    except Exception as e:
                        logger.error(f"回调执行失败: {e}")
            else:
                self.stats['failed'] += 1
        
        # 清理旧的处理记录（防止内存泄漏）
        if len(self._processed_files) > 10000:
            self._processed_files.clear()
    
    def _is_file_ready(self, file_path: Path, wait_time: float = 2.0) -> bool:
        """
        检查文件是否写入完成
        
        Args:
            file_path: 文件路径
            wait_time: 等待时间
            
        Returns:
            文件是否就绪
        """
        try:
            initial_size = file_path.stat().st_size
            time.sleep(wait_time)
            
            if not file_path.exists():
                return False
            
            current_size = file_path.stat().st_size
            return initial_size == current_size and current_size > 0
            
        except Exception:
            return False
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            'is_running': self._running,
            'watch_dir': str(self.watch_dir),
            'target_dir': str(self.target_dir),
            'template': self.template
        }


# 全局监控器实例
_watcher: Optional[DirectoryWatcher] = None


def get_watcher() -> Optional[DirectoryWatcher]:
    """获取全局监控器"""
    return _watcher


def start_watcher(watch_dir: str, target_dir: str, template: str = "{album_artist}/{album}",
                  on_conflict: str = 'skip', callback: Callable = None) -> DirectoryWatcher:
    """启动全局监控器"""
    global _watcher
    
    if _watcher:
        _watcher.stop()
    
    _watcher = DirectoryWatcher(watch_dir, target_dir, template, on_conflict)
    
    # 添加回调
    if callback:
        _watcher.add_callback(callback)
    
    _watcher.start()
    return _watcher


def stop_watcher():
    """停止全局监控器"""
    global _watcher
    
    if _watcher:
        _watcher.stop()
        _watcher = None


# 预设模板
PRESET_TEMPLATES = {
    'artist_album': {
        'name': '艺术家/专辑',
        'template': '{album_artist}/{album}',
        'description': '按艺术家和专辑分类'
    },
    'artist_year_album': {
        'name': '艺术家/年份-专辑',
        'template': '{album_artist}/{year} - {album}',
        'description': '按艺术家分类，专辑按年份排序'
    },
    'genre_artist_album': {
        'name': '风格/艺术家/专辑',
        'template': '{genre}/{album_artist}/{album}',
        'description': '先按风格分类，再按艺术家'
    },
    'year_artist_album': {
        'name': '年份/艺术家/专辑',
        'template': '{year}/{album_artist}/{album}',
        'description': '按年份分类'
    },
    'flat_artist': {
        'name': '艺术家（平铺）',
        'template': '{album_artist}',
        'description': '只按艺术家分类，专辑不分子目录'
    }
}


if __name__ == '__main__':
    # 测试代码
    import sys
    
    logging.basicConfig(level=logging.DEBUG)
    
    if len(sys.argv) >= 3:
        source = sys.argv[1]
        target = sys.argv[2]
        template = sys.argv[3] if len(sys.argv) > 3 else "{album_artist}/{album}"
        
        if Path(source).is_file():
            result = organize_file(source, target, template, move=False)
            print(f"整理结果: {result}")
        else:
            print(f"启动监控: {source} -> {target}")
            watcher = DirectoryWatcher(source, target, template)
            watcher.start()
            
            try:
                while True:
                    time.sleep(10)
                    print(f"统计: {watcher.get_stats()}")
            except KeyboardInterrupt:
                watcher.stop()
    else:
        print("用法: python file_organizer.py <源文件/目录> <目标目录> [模板]")
        print("模板示例: {album_artist}/{album}")


def search_cover_online(artist: str, album: str, title: str = "", output_path: str = None) -> Optional[str]:
    """
    从网易云/QQ音乐搜索并下载高清封面（严格匹配）
    
    Args:
        artist: 艺术家名
        album: 专辑名
        title: 歌曲标题（备用）
        output_path: 封面保存路径
        
    Returns:
        保存的封面路径，失败返回 None
    """
    import requests
    import re
    
    if not output_path:
        return None
    
    output_path = Path(output_path)
    if output_path.exists():
        return str(output_path)
    
    # 必须有专辑名才搜索
    if not album or len(album.strip()) < 2:
        print(f"[CoverSearch] 跳过：专辑名太短或为空 '{album}'")
        return None
    
    album_clean = album.strip().lower()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    def is_album_match(found_name: str, target_name: str) -> bool:
        """检查搜索到的专辑名是否匹配"""
        if not found_name or not target_name:
            return False
        found = found_name.lower().strip()
        target = target_name.lower().strip()
        # 完全匹配
        if found == target:
            return True
        # 包含匹配（双向）
        if target in found or found in target:
            return True
        # 去除特殊字符后匹配
        found_simple = re.sub(r'[\s\-_（）()【】\[\]《》]', '', found)
        target_simple = re.sub(r'[\s\-_（）()【】\[\]《》]', '', target)
        if found_simple == target_simple or target_simple in found_simple or found_simple in target_simple:
            return True
        return False
    
    cover_url = None
    matched_album = None
    
    # 1. 尝试网易云音乐搜索
    try:
        ncm_search_url = "https://music.163.com/api/search/get"
        params = {
            's': album,  # 只用专辑名搜索，更精确
            'type': 10,  # 专辑搜索
            'limit': 10,
            'offset': 0
        }
        resp = requests.get(ncm_search_url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('result') and data['result'].get('albums'):
                for album_item in data['result']['albums']:
                    found_album = album_item.get('name', '')
                    if is_album_match(found_album, album):
                        pic_url = album_item.get('picUrl')
                        if pic_url:
                            cover_url = pic_url + "?param=800y800"
                            matched_album = found_album
                            print(f"[CoverSearch] 网易云匹配成功: '{found_album}' ≈ '{album}'")
                            break
    except Exception as e:
        print(f"[CoverSearch] 网易云搜索失败: {e}")
    
    # 2. 如果网易云未匹配，尝试 QQ 音乐
    if not cover_url:
        try:
            qq_search_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                'w': album,
                'format': 'json',
                'p': 1,
                'n': 10,
                't': 8
            }
            resp = requests.get(qq_search_url, params=params, headers={
                **headers,
                'Referer': 'https://y.qq.com'
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                albums = data.get('data', {}).get('album', {}).get('list', [])
                for album_item in albums:
                    found_album = album_item.get('albumName', '')
                    if is_album_match(found_album, album):
                        mid = album_item.get('albumMID')
                        if mid:
                            cover_url = f"https://y.qq.com/music/photo_new/T002R800x800M000{mid}.jpg"
                            matched_album = found_album
                            print(f"[CoverSearch] QQ音乐匹配成功: '{found_album}' ≈ '{album}'")
                            break
        except Exception as e:
            print(f"[CoverSearch] QQ音乐搜索失败: {e}")
    
    # 3. 下载封面（只有匹配成功才下载）
    if cover_url and matched_album:
        try:
            resp = requests.get(cover_url, headers=headers, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 1000:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(resp.content)
                print(f"[CoverSearch] 下载封面成功: {output_path}")
                return str(output_path)
        except Exception as e:
            print(f"[CoverSearch] 下载封面失败: {e}")
    else:
        print(f"[CoverSearch] 未找到精确匹配的专辑: '{album}'")
    
    return None


def extract_or_search_cover(file_path: str, output_dir: str, filename: str = "cover.jpg") -> Optional[str]:
    """
    先尝试提取内嵌封面，如果没有则在线搜索
    
    Args:
        file_path: 音频文件路径
        output_dir: 输出目录
        filename: 封面文件名
        
    Returns:
        保存的封面路径，失败返回 None
    """
    # 先尝试提取内嵌封面
    result = extract_cover_art(file_path, output_dir, filename)
    if result:
        return result
    
    # 如果没有内嵌封面，读取元数据并在线搜索
    try:
        metadata = read_audio_metadata(file_path)
        if metadata:
            artist = metadata.get('artist', '')
            album = metadata.get('album', '')
            title = metadata.get('title', '')
            
            output_path = Path(output_dir) / filename
            return search_cover_online(artist, album, title, str(output_path))
    except Exception as e:
        print(f"[CoverSearch] 读取元数据失败: {e}")
    
    return None


def search_artist_photo(artist: str, output_path: str = None) -> Optional[str]:
    """
    从网易云/QQ音乐搜索艺术家头像并下载
    
    Args:
        artist: 艺术家名
        output_path: 保存路径 (如 /music/周杰伦/folder.jpg)
        
    Returns:
        保存的文件路径，失败返回 None
    """
    import requests
    
    if not output_path or not artist or len(artist.strip()) < 2:
        return None
    
    output_path = Path(output_path)
    if output_path.exists():
        return str(output_path)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    photo_url = None
    
    # 1. 尝试网易云音乐搜索艺术家
    try:
        ncm_search_url = "https://music.163.com/api/search/get"
        params = {
            's': artist,
            'type': 100,  # 艺术家搜索
            'limit': 5,
            'offset': 0
        }
        resp = requests.get(ncm_search_url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('result') and data['result'].get('artists'):
                for ar in data['result']['artists']:
                    ar_name = ar.get('name', '')
                    # 模糊匹配艺术家名
                    if ar_name.lower() == artist.lower() or artist.lower() in ar_name.lower():
                        pic_url = ar.get('img1v1Url') or ar.get('picUrl')
                        if pic_url and 'default' not in pic_url.lower():
                            photo_url = pic_url + "?param=500y500"
                            print(f"[ArtistPhoto] 网易云匹配: '{ar_name}' ≈ '{artist}'")
                            break
    except Exception as e:
        print(f"[ArtistPhoto] 网易云搜索失败: {e}")
    
    # 2. 如果网易云未找到，尝试 QQ 音乐
    if not photo_url:
        try:
            qq_search_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                'w': artist,
                'format': 'json',
                'p': 1,
                'n': 5,
                't': 9  # 歌手搜索
            }
            resp = requests.get(qq_search_url, params={
                **params
            }, headers={
                **headers,
                'Referer': 'https://y.qq.com'
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                singers = data.get('data', {}).get('singer', {}).get('list', [])
                for singer in singers:
                    singer_name = singer.get('singername', '')
                    if singer_name.lower() == artist.lower() or artist.lower() in singer_name.lower():
                        singer_mid = singer.get('singermid', '')
                        if singer_mid:
                            photo_url = f"https://y.gtimg.cn/music/photo_new/T001R500x500M000{singer_mid}.jpg"
                            print(f"[ArtistPhoto] QQ音乐匹配: '{singer_name}' ≈ '{artist}'")
                            break
        except Exception as e:
            print(f"[ArtistPhoto] QQ音乐搜索失败: {e}")
    
    # 3. 下载头像
    if photo_url:
        try:
            resp = requests.get(photo_url, headers=headers, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 5000:  # 确保不是空图
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(resp.content)
                print(f"[ArtistPhoto] 已保存艺术家头像: {output_path}")
                return str(output_path)
        except Exception as e:
            print(f"[ArtistPhoto] 下载失败: {e}")
    
    return None


def ensure_artist_photo(artist_dir: str, artist_name: str) -> Optional[str]:
    """
    确保艺术家目录有头像文件
    
    Args:
        artist_dir: 艺术家目录路径
        artist_name: 艺术家名
        
    Returns:
        头像文件路径，失败返回 None
    """
    artist_path = Path(artist_dir)
    if not artist_path.exists():
        return None
    
    # 检查是否已有头像
    for name in ['folder.jpg', 'artist.jpg', 'poster.jpg']:
        if (artist_path / name).exists():
            return str(artist_path / name)
    
    # 搜索并下载
    return search_artist_photo(artist_name, str(artist_path / 'folder.jpg'))
