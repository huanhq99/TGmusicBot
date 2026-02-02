#!/usr/bin/env python3
"""
通用装饰器
"""

import logging
import functools
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def error_handler(func: Callable):
    """
    统一错误处理装饰器
    捕获异常并向用户发送友好错误消息
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.exception(f"命令处理异常 [{func.__name__}]: {e}")
            
            try:
                if update.message:
                    await update.message.reply_text(
                        f"❌ 操作失败：{str(e)[:100]}",
                        parse_mode=None
                    )
                elif update.callback_query:
                    await update.callback_query.answer("❌ 操作失败", show_alert=True)
            except:
                pass
            return None
    return wrapper


def admin_only(func: Callable):
    """仅管理员可用装饰器"""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        from bot.config import ADMIN_USER_ID
        from bot.utils.helpers import is_admin
        
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not is_admin(user_id, ADMIN_USER_ID or ""):
            if update.message:
                await update.message.reply_text("⛔ 此命令仅管理员可用")
            return None
        return await func(update, context, *args, **kwargs)
    return wrapper


def rate_limit(seconds: int = 3):
    """频率限制装饰器"""
    _last_call = {}
    
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            import time
            user_id = update.effective_user.id if update.effective_user else 0
            key = f"{func.__name__}:{user_id}"
            now = time.time()
            
            if key in _last_call:
                elapsed = now - _last_call[key]
                if elapsed < seconds:
                    remaining = int(seconds - elapsed)
                    if update.message:
                        await update.message.reply_text(f"⏳ 请等待 {remaining} 秒后再试")
                    return None
            
            _last_call[key] = now
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator
