#!/usr/bin/env python3
"""
下载相关处理器
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


async def cmd_download_status(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              get_download_manager, ADMIN_USER_ID):
    """查看下载状态 /ds"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("下载管理器未初始化")
        return
    
    stats = manager.get_stats()
    queue = stats['queue']
    today = stats['today']
    
    msg = "📊 **下载状态**\n\n"
    
    # 队列状态
    msg += "**📥 下载队列**\n"
    msg += f"├ 等待中: {queue['pending']}\n"
    msg += f"├ 下载中: {queue['downloading']}\n"
    msg += f"├ 重试中: {queue['retrying']}\n"
    msg += f"├ 已完成: {queue['completed']}\n"
    msg += f"└ 失败: {queue['failed']}\n\n"
    
    # 今日统计
    msg += "**📈 今日统计**\n"
    msg += f"├ 成功: {today['total_success']} 首\n"
    msg += f"├ 失败: {today['total_fail']} 首\n"
    msg += f"└ 总大小: {format_size(today['total_size'])}\n\n"
    
    # 平台分布
    if today['by_platform']:
        msg += "**🎵 平台分布**\n"
        for platform, data in today['by_platform'].items():
            msg += f"├ {platform}: {data['success']} 成功 / {data['fail']} 失败\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_download_queue(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             get_download_manager, ADMIN_USER_ID):
    """查看下载队列 /dq"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("下载管理器未初始化")
        return
    
    queue_status = manager.get_queue_status()
    tasks = queue_status['tasks']
    
    if not tasks:
        await update.message.reply_text("📭 下载队列为空")
        return
    
    msg = f"📥 **下载队列** ({queue_status['total']} 个任务)\n\n"
    
    status_emoji = {
        'pending': '⏳',
        'downloading': '📥',
        'completed': '✅',
        'failed': '❌',
        'retrying': '🔄',
        'cancelled': '🚫'
    }
    
    for i, task in enumerate(tasks[-10:], 1):  # 显示最近10个
        emoji = status_emoji.get(task['status'], '❓')
        name = task.get('title', '未知')[:25]
        artist = task.get('artist', '')[:15]
        msg += f"{emoji} `{name}` - {artist}\n"
    
    if len(tasks) > 10:
        msg += f"\n... 还有 {len(tasks) - 10} 个任务"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_download_history(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               get_download_manager, ADMIN_USER_ID):
    """查看下载历史 /dh"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    manager = get_download_manager()
    if not manager:
        await update.message.reply_text("下载管理器未初始化")
        return
    
    history = manager.stats.get_recent_history(20)
    
    if not history:
        await update.message.reply_text("📭 暂无下载历史")
        return
    
    msg = "📜 **最近下载历史**\n\n"
    
    status_emoji = {
        'completed': '✅',
        'failed': '❌',
    }
    
    for item in history:
        emoji = status_emoji.get(item['status'], '❓')
        title = (item.get('title') or '未知')[:20]
        artist = (item.get('artist') or '')[:12]
        platform = item.get('platform', '?')
        
        msg += f"{emoji} `{title}` - {artist} [{platform}]\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


async def handle_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """下载回调 - 占位，实际实现在 main.py"""
    pass
