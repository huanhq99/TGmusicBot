#!/usr/bin/env python3
"""
处理器模块 - 拆分 main.py 中的命令处理器
"""

# 搜索处理器
from .search import (
    cmd_search,
    cmd_album,
    cmd_qq_search,
    cmd_qq_album,
    handle_search_download_callback,
    handle_qq_download_callback,
)

# 下载处理器
from .download import (
    cmd_download_status,
    cmd_download_queue,
    cmd_download_history,
    handle_download_callback,
)

# 歌单处理器
from .playlist import (
    cmd_schedule,
    cmd_unschedule,
    handle_sync_callback,
)

__all__ = [
    'cmd_search',
    'cmd_album', 
    'cmd_qq_search',
    'cmd_qq_album',
    'handle_search_download_callback',
    'handle_qq_download_callback',
    'cmd_download_status',
    'cmd_download_queue',
    'cmd_download_history',
    'handle_download_callback',
    'cmd_schedule',
    'cmd_unschedule',
    'handle_sync_callback',
]
