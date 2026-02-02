#!/usr/bin/env python3
"""
播放统计命令处理器
- /mystats: 个人统计
- /ranking: 查看排行榜
- /yearreview: 年度总结
"""

import logging
from datetime import datetime
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """个人播放统计"""
    from bot.services.playback_stats import get_playback_stats
    from bot.utils.ranking_image import generate_user_stats_image
    
    user = update.effective_user
    telegram_id = str(user.id)
    
    try:
        stats = get_playback_stats()
        data = stats.get_user_stats(telegram_id=telegram_id)
        
        if not data or data.get('total_plays', 0) == 0:
            await update.message.reply_text(
                "📊 暂无播放记录\n\n"
                "播放记录需要通过 Emby Webhook 同步，"
                "请确保已配置 Webhook 并绑定了 Emby 账号"
            )
            return
        
        # 生成图片
        img_data = generate_user_stats_image(data, user.first_name, emby_url=stats.emby_url, emby_token=stats.emby_token)
        
        if img_data:
            await update.message.reply_photo(
                photo=BytesIO(img_data),
                caption=f"🎵 {user.first_name} 的音乐统计\n\n"
                        f"📊 总播放: {data['total_plays']} 次"
            )
        else:
            # 文字版本
            msg = f"🎵 **{user.first_name} 的音乐统计**\n\n"
            msg += f"📊 总播放: {data['total_plays']} 次\n\n"
            
            if data.get('top_artists'):
                msg += "❤️ 最爱歌手:\n"
                for i, a in enumerate(data['top_artists'][:3], 1):
                    msg += f"  {i}. {a['name']} ({a['count']}次)\n"
            
            if data.get('top_songs'):
                msg += "\n🎶 最爱歌曲:\n"
                for i, s in enumerate(data['top_songs'][:3], 1):
                    msg += f"  {i}. {s['title']} - {s['artist']}\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"获取个人统计失败: {e}")
        await update.message.reply_text("❌ 获取统计数据失败，请稍后再试")


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看排行榜"""
    from bot.services.playback_stats import get_playback_stats
    from bot.utils.ranking_image import generate_ranking_image
    import os
    
    # 参数: day/week/month
    period = 'day'
    if context.args:
        p = context.args[0].lower()
        if p in ['day', 'week', 'month', '日', '周', '月']:
            period = {'日': 'day', '周': 'week', '月': 'month'}.get(p, p)
    
    period_names = {'day': '日榜', 'week': '周榜', 'month': '月榜'}
    
    try:
        stats = get_playback_stats()
        ranking = stats.get_ranking(period=period, limit=10)
        
        if not ranking:
            await update.message.reply_text(f"📊 {period_names[period]}暂无数据")
            return
        
        # 生成图片
        emby_url = os.environ.get('EMBY_SERVER_URL', '')
        title = f"🏆 播放{period_names[period]}"
        subtitle = datetime.now().strftime('%Y-%m-%d')
        
        img_data = generate_ranking_image(
            ranking, 
            title=title,
            subtitle=subtitle,
            emby_base_url=emby_url
        )
        
        if img_data:
            await update.message.reply_photo(
                photo=BytesIO(img_data),
                caption=f"🏆 播放{period_names[period]} ({subtitle})"
            )
        else:
            # 文字版本
            msg = f"🏆 **播放{period_names[period]}** ({subtitle})\n\n"
            for i, item in enumerate(ranking, 1):
                medal = ['🥇', '🥈', '🥉'][i-1] if i <= 3 else f"{i}."
                msg += f"{medal} {item['artist']} - {item['title']} ({item['count']}次)\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"获取排行榜失败: {e}")
        await update.message.reply_text("❌ 获取排行榜失败")


async def cmd_yearreview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """年度总结"""
    from bot.services.playback_stats import get_playback_stats
    import os
    
    user = update.effective_user
    telegram_id = str(user.id)
    
    # 年份参数
    year = datetime.now().year - 1  # 默认上一年
    if context.args:
        try:
            year = int(context.args[0])
        except:
            pass
    
    await update.message.reply_text(f"⏳ 正在生成 {year} 年度总结...")
    
    try:
        stats = get_playback_stats()
        data = stats.get_yearly_summary(year, telegram_id=telegram_id)
        
        if not data or data.get('total_plays', 0) == 0:
            await update.message.reply_text(f"📊 {year} 年暂无播放记录")
            return
        
        # 基础总结
        msg = f"🎵 **{user.first_name} 的 {year} 年度音乐总结**\n\n"
        msg += f"📊 总播放: {data['total_plays']} 次\n"
        msg += f"🎶 听过 {data['unique_songs']} 首不同的歌\n\n"
        
        # Top 歌手
        if data.get('top_artists'):
            msg += "❤️ 年度最爱歌手:\n"
            for i, a in enumerate(data['top_artists'][:3], 1):
                msg += f"  {i}. {a['artist']} ({a['cnt']}次)\n"
        
        # Top 歌曲
        if data.get('top_songs'):
            msg += "\n🎶 年度最爱歌曲:\n"
            for i, s in enumerate(data['top_songs'][:5], 1):
                msg += f"  {i}. {s['title']} - {s['artist']}\n"
        
        # 尝试 AI 总结
        openai_key = os.environ.get('OPENAI_API_KEY', '')
        if openai_key:
            try:
                ai_summary = await generate_ai_summary(data, user.first_name, year, openai_key)
                if ai_summary:
                    msg += f"\n\n✨ **AI 点评**:\n{ai_summary}"
            except Exception as e:
                logger.error(f"AI 总结失败: {e}")
        
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"年度总结失败: {e}")
        await update.message.reply_text("❌ 生成年度总结失败")


async def generate_ai_summary(data: dict, username: str, year: int, api_key: str) -> str:
    """使用 OpenAI 生成个性化年度总结"""
    import httpx
    import os
    
    api_url = os.environ.get('OPENAI_API_URL', 'https://api.openai.com/v1/chat/completions')
    model = os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo')
    
    # 构建提示词
    top_artists = ', '.join([a['artist'] for a in data.get('top_artists', [])[:3]])
    top_songs = ', '.join([f"{s['title']}-{s['artist']}" for s in data.get('top_songs', [])[:3]])
    
    prompt = f"""用户 {username} 在 {year} 年的音乐播放数据:
- 总播放 {data['total_plays']} 次
- 听过 {data['unique_songs']} 首不同的歌
- 最爱歌手: {top_artists}
- 最爱歌曲: {top_songs}

请用幽默、温暖的语气，用2-3句话点评这个用户的音乐品味，给出鼓励或有趣的评价。中文回复，不要超过100字。"""
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                api_url,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': 200
                }
            )
            resp.raise_for_status()
            result = resp.json()
            return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {e}")
        return ""

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """每日全服排行"""
    from bot.services.playback_stats import get_playback_stats
    from bot.utils.ranking_image import generate_daily_ranking_image
    from io import BytesIO
    
    status_msg = await update.message.reply_text("⏳ 正在搜集全服数据... (可能需要几秒)")
    
    try:
        stats_svc = get_playback_stats()
        # Fetch Data
        data = stats_svc.get_global_daily_stats()
        
        if not data or not data.get('leaderboard'):
            await status_msg.edit_text("📊 今日全服暂无播放记录")
            return
            
        # Fetch Custom Titles from DB
        from bot.config import DAILY_RANKING_TITLE, DAILY_RANKING_SUBTITLE, DATABASE_FILE
        import sqlite3
        
        ranking_title = DAILY_RANKING_TITLE
        ranking_subtitle = DAILY_RANKING_SUBTITLE
        
        try:
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM bot_settings WHERE key IN ('ranking_daily_title', 'ranking_daily_subtitle')")
                rows = cursor.fetchall()
                settings = {row[0]: row[1] for row in rows}
                if settings.get('ranking_daily_title'):
                    ranking_title = settings.get('ranking_daily_title')
                if settings.get('ranking_daily_subtitle'):
                    ranking_subtitle = settings.get('ranking_daily_subtitle')
        except Exception as e:
            logger.error(f"Failed to fetch ranking settings: {e}")

        # Generate Image (Run in executor to avoid blocking event loop)
        import asyncio
        from functools import partial
        
        loop = asyncio.get_running_loop()
        img_bytes = await loop.run_in_executor(
            None,
            partial(
                generate_daily_ranking_image, 
                data, 
                emby_url=stats_svc.emby_url, 
                emby_token=stats_svc.emby_token, 
                title=ranking_title
            )
        )
        
        # Delete status message
        await status_msg.delete()
        
        if img_bytes:
            # Generate Text Caption
            from bot.config import EMBY_URL
            
            # Format:
            # 【WENJIAN 播放日榜】
            # ▎热门歌曲:
            # 1 . Song (Link)
            # ...
            # #DayRanks YYYY-MM-DD
            
            caption_lines = [
                f"【{ranking_subtitle} 播放日榜】\n",
                "▎热门歌曲：\n"
            ]
            
            top_songs = data.get('top_songs', [])[:10]
            for i, song in enumerate(top_songs):
                title = song.get('title', 'Unknown')
                artist = song.get('artist', 'Unknown')
                album = song.get('album', '')
                count = song.get('count', 0)
                sid = song.get('id', '')
                
                # Formatting:
                # 1. Song Title
                # 歌手: Artist
                # 专辑: Album (if available)
                # 播放次数: ...
                
                line = f"{i+1}. {title}"
                caption_lines.append(line)
                if artist and artist != 'Unknown':
                    caption_lines.append(f"歌手: {artist}")
                if album:
                    caption_lines.append(f"专辑: {album}")
                caption_lines.append(f"播放次数: {count}")
                # No extra newline between items in list based on screenshot text tightness? 
                # Screenshot text:
                # 1 . ...
                # 歌手: ...
                # 播放次数: ...
                # 2 . ...
                caption_lines.append("") # Empty line between songs? The text block 8272 looks tight but has numbering.
            
            caption_lines.append(f"\n#DayRanks  {data.get('date', 'Unknown')}")
            
            # Add explicit debug info if available
            if data.get('debug_keys'):
                 caption_lines.append(f"\n[Debug] Keys: {data['debug_keys']}")
            
            caption = "\n".join(caption_lines)
            
            if len(caption) > 1024:
                # Split if too long, but for 10 songs it should be ~600 chars max.
                caption = caption[:1020] + "..."

            await update.message.reply_photo(
                photo=BytesIO(img_bytes),
                caption=caption
            )
        else:
            await update.message.reply_text("❌ 生成图片失败")
            
    except Exception as e:
        logger.error(f"Daily Command Failed: {e}")
        await status_msg.edit_text("❌ 获取数据失败 (请查看日志)")
