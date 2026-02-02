#!/usr/bin/env python3
"""
重复歌曲检测模块
通过 Emby API 扫描媒体库中的重复歌曲
"""

import logging
import re
import os
import requests
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

# 进度状态
scan_progress = {
    'status': 'idle',  # idle, scanning, analyzing, done
    'current': 0,
    'total': 0,
    'message': ''
}

def get_scan_progress():
    """获取扫描进度"""
    return scan_progress.copy()


def normalize_string(s: str) -> str:
    """
    归一化字符串，用于匹配比较
    - 转小写
    - 移除曲目编号（如 01., 01 -, Track 1 等）
    - 移除版本后缀（Remastered 等）  
    - 统一标点符号和空格
    """
    if not s:
        return ""
    
    result = s.lower()
    
    # 移除开头的曲目编号（01., 01 -, Track 01, etc.）
    result = re.sub(r'^\d{1,3}[\.\-\s]+', '', result)
    result = re.sub(r'^track\s*\d+[\.\-\s]*', '', result, flags=re.IGNORECASE)
    
    # 只移除不影响歌曲本质的版本后缀（如 remastered）
    # 保留：live, remix, acoustic, instrumental, 伴奏 等（这些是不同版本）
    version_patterns = [
        r'\s*\(remaster(ed)?\)',
        r'\s*\(single version\)',
        r'\s*\(album version\)',
        r'\s*\[remaster(ed)?\]',
    ]
    
    for pattern in version_patterns:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    
    # 统一分隔符
    result = result.replace('&', '/').replace('、', '/').replace(',', '/')
    
    # 移除常见标点，使 "Song Name" 和 "Song - Name" 能匹配
    result = re.sub(r'[\.\-\—\–\_]+', ' ', result)
    
    # 去除多余空格
    result = re.sub(r'\s+', ' ', result).strip()
    
    return result


def get_quality_score_from_container(container: str) -> int:
    """
    根据容器格式估算质量分数
    """
    format_scores = {
        'flac': 100,
        'ape': 95,
        'wav': 90,
        'alac': 90,
        'aiff': 85,
        'mp3': 50,
        'm4a': 45,
        'aac': 40,
        'ogg': 35,
        'wma': 30,
    }
    
    if not container:
        return 10
    
    container_lower = container.lower()
    return format_scores.get(container_lower, 10)


def scan_duplicates_emby() -> List[Dict]:
    """
    通过 Emby API 扫描重复歌曲
    
    Returns:
        重复组列表
    """
    logger.info("[Duplicates] 通过 Emby API 开始扫描...")
    
    # 使用 API Key 认证
    emby_url = os.environ.get('EMBY_URL', '') or os.environ.get('EMBY_SERVER_URL', '')
    emby_token = os.environ.get('EMBY_API_KEY', '')
    
    if not emby_url or not emby_token:
        logger.error("[Duplicates] Emby URL 或 API Key 未配置")
        return []
    
    # 获取所有音频
    global scan_progress
    scan_progress = {'status': 'scanning', 'current': 0, 'total': 0, 'message': '正在获取媒体库信息...'}
    
    all_items = []
    start_index = 0
    page_size = 500
    
    while True:
        try:
            url = f"{emby_url.rstrip('/')}/emby/Items"
            params = {
                'api_key': emby_token,
                'IncludeItemTypes': 'Audio',
                'Recursive': 'true',
                'Fields': 'Id,Name,Path,Container,Size,ArtistItems,Album,AlbumArtist',
                'Limit': page_size,
                'StartIndex': start_index
            }
            
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code != 200:
                logger.error(f"[Duplicates] Emby API 返回 {resp.status_code}: {resp.text[:200]}")
                break
            
            try:
                data = resp.json()
            except Exception as json_err:
                logger.error(f"[Duplicates] Emby 响应不是 JSON: {resp.text[:200]}")
                break
            items = data.get('Items', [])
            if not items:
                break
            all_items.extend(items)
            
            logger.info(f"[Duplicates] 已获取 {len(all_items)} 首歌曲...")
            scan_progress['current'] = len(all_items)
            scan_progress['message'] = f'已获取 {len(all_items)} 首歌曲...'
            
            if len(items) < page_size:
                break
            start_index += page_size
        except Exception as e:
            logger.error(f"[Duplicates] Emby API 调用失败: {e}")
            break
    
    logger.info(f"[Duplicates] Emby 返回 {len(all_items)} 首歌曲")
    
    if not all_items:
        scan_progress = {'status': 'done', 'current': 0, 'total': 0, 'message': '没有找到音频文件'}
        return []
    
    scan_progress['status'] = 'analyzing'
    scan_progress['total'] = len(all_items)
    scan_progress['message'] = f'正在分析 {len(all_items)} 首歌曲...'
    
    # 按 (title, artist) 分组
    groups: Dict[str, List[Dict]] = defaultdict(list)
    
    for item in all_items:
        title = item.get('Name', '')
        album = item.get('Album', '')
        path = item.get('Path', '')
        
        # 获取艺术家
        artists = item.get('ArtistItems', [])
        if artists:
            artist = '/'.join([a.get('Name', '') for a in artists])
        else:
            artist = item.get('AlbumArtist', '')
        
        # 获取父目录路径（作为唯一性判断的一部分）
        # 不同目录的文件绝对不是重复的
        parent_dir = os.path.dirname(path) if path else ''
        
        # 从文件路径中提取文件名（不含扩展名）
        # 使用文件名而不是 Emby 元数据，因为元数据可能不准确
        filename = os.path.basename(path) if path else ''
        filename_no_ext = os.path.splitext(filename)[0] if filename else title
        
        # 归一化生成 key（必须同一目录 + 同文件名才算重复）
        norm_filename = normalize_string(filename_no_ext)
        key = f"{parent_dir}|{norm_filename}"
        
        # 调试：记录特定歌曲的路径信息
        if '水瓶座' in title or '水瓶座' in filename:
            logger.info(f"[Duplicates][Debug] 水瓶座: path='{path}', parent_dir='{parent_dir}', filename='{filename}', key='{key}'")
        
        # 获取质量信息
        container = item.get('Container', '')
        size_bytes = item.get('Size', 0)
        size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else 0
        
        score = get_quality_score_from_container(container)
        score += min(size_mb / 10, 20)  # 大文件加分
        
        file_info = {
            'id': item.get('Id'),
            'path': item.get('Path', ''),
            'format': container.lower() if container else 'unknown',
            'size_mb': size_mb,
            'score': int(score),
            'original_title': title,
            'original_artist': artist,
            'album': item.get('Album', '')
        }
        
        groups[key].append(file_info)
    
    logger.info(f"[Duplicates] 分组完成: {len(groups)} 个唯一歌曲")
    
    # 筛选重复组（至少 2 个文件）
    duplicates = []
    for key, files in groups.items():
        if len(files) > 1:
            # 按质量分数排序（高分在前）
            files.sort(key=lambda x: x['score'], reverse=True)
            
            # 提取原始标题和歌手
            title = files[0].get('original_title', key.split('|')[0])
            artist = files[0].get('original_artist', key.split('|')[1] if '|' in key else '')
            
            duplicates.append({
                'key': key,
                'title': title,
                'artist': artist,
                'count': len(files),
                'files': files
            })
    
    # 按重复数量排序
    duplicates.sort(key=lambda x: x['count'], reverse=True)
    
    logger.info(f"[Duplicates] 发现 {len(duplicates)} 组重复歌曲")
    
    # 更新进度为完成
    scan_progress['status'] = 'done'
    scan_progress['message'] = f'扫描完成，发现 {len(duplicates)} 组重复'
    
    return duplicates


def delete_emby_item(item_id: str) -> Tuple[bool, str]:
    """
    通过 Emby API 删除媒体项
    
    Returns:
        (成功, 消息)
    """
    try:
        emby_url = os.environ.get('EMBY_URL', '') or os.environ.get('EMBY_SERVER_URL', '')
        emby_token = os.environ.get('EMBY_API_KEY', '')
        
        if not emby_url or not emby_token:
            return False, "Emby 未配置"
        
        url = f"{emby_url.rstrip('/')}/emby/Items/{item_id}"
        
        # 使用 headers 传递认证（某些 Emby 版本需要）
        headers = {
            'X-Emby-Token': emby_token,
            'X-MediaBrowser-Token': emby_token,
        }
        params = {'api_key': emby_token}
        
        logger.info(f"[Duplicates] 准备删除: {item_id}, URL: {url}")
        
        resp = requests.delete(url, headers=headers, params=params, timeout=30)
        
        logger.info(f"[Duplicates] 删除响应: {resp.status_code}, Body: {resp.text[:200] if resp.text else 'empty'}")
        
        if resp.status_code in [200, 204]:
            logger.info(f"[Duplicates] 删除成功: {item_id}")
            return True, "删除成功"
        else:
            logger.error(f"[Duplicates] 删除失败: HTTP {resp.status_code}, {resp.text}")
            return False, f"删除失败: HTTP {resp.status_code}"
            
    except Exception as e:
        logger.error(f"[Duplicates] 删除异常 {item_id}: {e}")
        return False, str(e)

