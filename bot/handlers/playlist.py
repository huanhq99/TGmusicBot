#!/usr/bin/env python3
"""
歌单相关处理器 - 占位文件
实际实现保留在 main.py 中，因为依赖较多
"""

import logging

logger = logging.getLogger(__name__)


async def cmd_schedule(*args, **kwargs):
    """查看定时同步歌单 - 占位"""
    pass


async def cmd_unschedule(*args, **kwargs):
    """取消定时同步 - 占位"""
    pass


async def handle_sync_callback(*args, **kwargs):
    """同步回调 - 占位"""
    pass
