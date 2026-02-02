#!/usr/bin/env python3
"""
通用辅助函数
"""

import re
import html


def escape_markdown(text: str) -> str:
    """
    转义 Telegram Markdown 特殊字符
    
    Args:
        text: 原始文本
        
    Returns:
        转义后的文本
    """
    if not text:
        return ""
    # Markdown 特殊字符: _ * [ ] ( ) ~ ` > # + - = | { } . !
    special_chars = r'_*[]()~`>#+-=|{}.!'
    result = ""
    for char in text:
        if char in special_chars:
            result += f"\\{char}"
        else:
            result += char
    return result


def escape_html(text: str) -> str:
    """
    转义 HTML 特殊字符
    
    Args:
        text: 原始文本
        
    Returns:
        转义后的文本
    """
    if not text:
        return ""
    return html.escape(text)


def clean_filename(filename: str) -> str:
    """
    清理文件名中的非法字符
    
    Args:
        filename: 原始文件名
        
    Returns:
        清理后的文件名
    """
    if not filename:
        return "unknown"
    # 移除路径分隔符和非法字符
    cleaned = re.sub(r'[<>:"/\\|?*]', '', filename)
    # 移除首尾空格和点
    cleaned = cleaned.strip(' .')
    # 限制长度
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned or "unknown"


def parse_admin_ids(admin_str: str) -> list:
    """
    解析管理员 ID 字符串（支持逗号分隔）
    
    Args:
        admin_str: 管理员 ID 字符串，如 "123456789,987654321"
        
    Returns:
        管理员 ID 列表
    """
    if not admin_str:
        return []
    
    ids = []
    for item in admin_str.split(','):
        item = item.strip()
        if item.isdigit():
            ids.append(int(item))
    return ids


def is_admin(user_id: int, admin_str: str) -> bool:
    """
    检查用户是否为管理员
    
    Args:
        user_id: 用户 ID
        admin_str: 管理员 ID 字符串
        
    Returns:
        是否为管理员
    """
    admin_ids = parse_admin_ids(admin_str)
    return user_id in admin_ids


def format_duration(seconds: float) -> str:
    """
    格式化时长
    
    Args:
        seconds: 秒数
        
    Returns:
        格式化的时长字符串，如 "1:23:45" 或 "3:45"
    """
    if seconds < 0:
        return "0:00"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小
    
    Args:
        size_bytes: 字节数
        
    Returns:
        格式化的大小字符串，如 "1.5 MB"
    """
    if size_bytes < 0:
        return "0 B"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(size_bytes)
    unit_index = 0
    
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    else:
        return f"{size:.1f} {units[unit_index]}"
