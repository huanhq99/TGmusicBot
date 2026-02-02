#!/usr/bin/env python3
"""
进度条工具函数
"""


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
