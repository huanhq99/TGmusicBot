#!/usr/bin/env python3
"""
搜索相关处理器
"""

import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    get_ncm_cookie, get_ncm_settings, ADMIN_USER_ID, MUSIC_TARGET_DIR,
                    make_progress_message):
    """网易云搜索歌曲"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /search <关键词>\n例如: /search 周杰伦 晴天")
        return
    
    keyword = ' '.join(context.args)
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await update.message.reply_text("❌ 未配置网易云 Cookie")
        return
    
    await update.message.reply_text(f"🔍 正在搜索: {keyword}...")
    
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        results = api.search_song(keyword, limit=10)
        
        if not results:
            await update.message.reply_text("未找到相关歌曲")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['search_results'] = results
        
        msg = f"🎵 **搜索结果** ({len(results)} 首)\n\n"
        keyboard_buttons = []
        
        for i, song in enumerate(results):
            msg += f"`{i+1}.` {song['title']} - {song['artist']}\n"
            msg += f"    📀 {song.get('album', '未知专辑')}\n"
            # 添加试听和下载按钮
            keyboard_buttons.append([
                InlineKeyboardButton(f"🎧 试听", callback_data=f"preview_song_{i}"),
                InlineKeyboardButton(f"📥 下载", callback_data=f"dl_song_{i}")
            ])
        
        keyboard_buttons.append([InlineKeyboardButton("📥 全部下载", callback_data="dl_song_all")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"搜索失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_album(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   get_ncm_cookie, ADMIN_USER_ID):
    """网易云搜索专辑"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /album <专辑名或关键词>\n例如: /album 范特西")
        return
    
    keyword = ' '.join(context.args)
    ncm_cookie = get_ncm_cookie()
    
    if not ncm_cookie:
        await update.message.reply_text("❌ 未配置网易云 Cookie")
        return
    
    await update.message.reply_text(f"🔍 正在搜索专辑: {keyword}...")
    
    try:
        from bot.ncm_downloader import NeteaseMusicAPI
        api = NeteaseMusicAPI(ncm_cookie)
        results = api.search_album(keyword, limit=5)
        
        if not results:
            await update.message.reply_text("未找到相关专辑")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['album_results'] = results
        
        msg = f"💿 **专辑搜索结果** ({len(results)} 张)\n\n"
        keyboard_buttons = []
        
        for i, album in enumerate(results):
            msg += f"`{i+1}.` {album['name']}\n"
            msg += f"    🎤 {album['artist']} · {album['size']} 首歌\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {album['name'][:25]}", callback_data=f"dl_album_{i}")
            ])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"搜索专辑失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_qq_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       get_qq_cookie, ADMIN_USER_ID):
    """QQ音乐搜索歌曲"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /qs <关键词>\n例如: /qs 周杰伦 晴天")
        return
    
    keyword = ' '.join(context.args)
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await update.message.reply_text("❌ 未配置 QQ音乐 Cookie，请在 Web 设置中配置")
        return
    
    await update.message.reply_text(f"🔍 正在搜索 QQ音乐: {keyword}...")
    
    try:
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(qq_cookie)
        results = api.search_song(keyword, limit=10)
        
        if not results:
            await update.message.reply_text("未找到相关歌曲")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['qq_search_results'] = results
        
        msg = f"🎵 **QQ音乐搜索结果** ({len(results)} 首)\n\n"
        keyboard_buttons = []
        
        for i, song in enumerate(results):
            msg += f"`{i+1}.` {song['title']} - {song['artist']}\n"
            msg += f"    📀 {song.get('album', '未知专辑')}\n"
            # 添加试听和下载按钮
            keyboard_buttons.append([
                InlineKeyboardButton(f"🎧 试听", callback_data=f"qpreview_song_{i}"),
                InlineKeyboardButton(f"📥 下载", callback_data=f"qdl_song_{i}")
            ])
        
        keyboard_buttons.append([InlineKeyboardButton("📥 全部下载", callback_data="qdl_song_all")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"QQ音乐搜索失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


async def cmd_qq_album(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      get_qq_cookie, ADMIN_USER_ID):
    """QQ音乐搜索专辑"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("无权执行此命令")
        return
    
    if not context.args:
        await update.message.reply_text("用法: /qa <专辑名或关键词>\n例如: /qa 范特西")
        return
    
    keyword = ' '.join(context.args)
    qq_cookie = get_qq_cookie()
    
    if not qq_cookie:
        await update.message.reply_text("❌ 未配置 QQ音乐 Cookie，请在 Web 设置中配置")
        return
    
    await update.message.reply_text(f"🔍 正在搜索 QQ音乐专辑: {keyword}...")
    
    try:
        from bot.ncm_downloader import QQMusicAPI
        api = QQMusicAPI(qq_cookie)
        results = api.search_album(keyword, limit=5)
        
        if not results:
            await update.message.reply_text("未找到相关专辑")
            return
        
        # 保存搜索结果到用户数据
        context.user_data['qq_album_results'] = results
        
        msg = f"💿 **QQ音乐专辑搜索结果** ({len(results)} 张)\n\n"
        keyboard_buttons = []
        
        for i, album in enumerate(results):
            msg += f"`{i+1}.` {album['name']}\n"
            msg += f"    🎤 {album['artist']} · {album['size']} 首歌\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"📥 {album['name'][:25]}", callback_data=f"qdl_album_{i}")
            ])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.exception(f"QQ音乐搜索专辑失败: {e}")
        await update.message.reply_text(f"❌ 搜索失败: {e}")


# 下载回调处理器需要较多依赖，保留在 main.py 中或单独处理
async def handle_search_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """搜索结果下载回调 - 占位，实际实现在 main.py"""
    pass


async def handle_qq_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QQ音乐下载回调 - 占位，实际实现在 main.py"""
    pass
