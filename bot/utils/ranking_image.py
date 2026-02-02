#!/usr/bin/env python3
"""排行榜图片生成器 - V34 VISUAL POLISH (Soft Gloss & Centering)"""

import io
import logging
import datetime
import random
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
    "/usr/share/fonts/truetype/arphic/uming.ttc", # Debian fallback
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", # WenQuanYi fallback
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

def get_font(size: int, bold: bool = False):
    paths = FONT_PATHS
    if not bold:
        paths = paths + ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except:
            continue
    return ImageFont.load_default()

def remove_emoji(text: str) -> str:
    for c in "🎵📊❤️🎶✨📅🏆▶️🔥💜🎧🎤🎹":
        text = text.replace(c, "")
    return text.strip()

def fetch_cover_image(emby_url: str, item_id: str, token: str) -> tuple[Optional[Image.Image], str]:
    if not emby_url: return None, "NoURL"
    if not item_id: return None, "NoID"
    if not token: return None, "NoTok"
    try:
        base = emby_url.rstrip('/')
        url = f"{base}/Items/{item_id}/Images/Primary?maxHeight=300&maxWidth=300&quality=90"
        headers = {'X-Emby-Token': token}
        resp = requests.get(url, headers=headers, timeout=5, verify=False)
        if resp.status_code == 200:
            return Image.open(io.BytesIO(resp.content)).convert('RGBA'), ""
        return None, str(resp.status_code)
    except:
        return None, "Err"

def create_bg_gradient(width, height):
    """V33/34 Dark Background (Deep Purple/Navy)"""
    base = Image.new('RGBA', (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(base)
    for y in range(height):
        # Dark Deep Blue Gradient
        r = int(10 + (y/height)*8)
        g = int(5 + (y/height)*8)
        b = int(30 + (y/height)*15)
        d.line([(0, y), (width, y)], fill=(r,g,b))
    return base

def draw_skewed_card(img, x, y, w, h, c_start, c_end, slope=0.15):
    """Draws a gradient parallelogram card"""
    base_grad = Image.new('RGBA', (int(w + h*slope), h), c_start)
    top_grad = Image.new('RGBA', (int(w + h*slope), h), c_end)
    grad_mask = Image.new('L', base_grad.size)
    g_data = []
    gw = base_grad.width
    for i in range(h):
        for j in range(gw):
             g_data.append(int(255 * (j/gw)))
    grad_mask.putdata(g_data)
    base_grad.paste(top_grad, (0,0), grad_mask)
    
    mask = Image.new('L', base_grad.size, 0)
    d = ImageDraw.Draw(mask)
    shift = int(h * slope)
    points = [(shift, 0), (w + shift, 0), (w, h), (0, h)]
    d.polygon(points, fill=255)
    
    shine = Image.new('RGBA', base_grad.size, (0,0,0,0))
    s_draw = ImageDraw.Draw(shine)
    s_draw.polygon([
        (shift, 0), (w+shift, 0),
        (w+shift - (w*0.3), h), (shift - (w*0.1), h)
    ], fill=(255,255,255,40))
    # Alpha composite shine onto base_grad
    base_grad.alpha_composite(shine)

    img.paste(base_grad, (x, y), mask)
    return shift

def get_personality_tag(stats: Dict) -> str:
    # return "深度聆听者 · 华语情歌控"
    # Make it dynamic if possible, or static as per request
    return "深度聆听者 · 华语情歌控"

def generate_user_stats_image(stats: Dict, username: str = "", year: str = None, emby_url: str = None, emby_token: str = None) -> Optional[bytes]:
    """生成个人统计图片 (V34 Polish)"""
    if not HAS_PIL: return None
    
    width = 1080
    height = 1920
    current_year = year or str(datetime.datetime.now().year)
    slope = 0.15
    
    # 1. Base Image
    img = create_bg_gradient(width, height)
    draw = ImageDraw.Draw(img)

    # Fonts
    title_font = get_font(64, bold=True)
    tag_font = get_font(32, bold=True)
    stat_val_font = get_font(56, bold=True)
    stat_lbl_font = get_font(24)
    section_font = get_font(42, bold=True)
    
    rank_font = get_font(38, bold=True)
    song_font = get_font(28, bold=True)
    artist_font = get_font(22, bold=False)
    plays_font = get_font(24, bold=True)
    
    # HEADER Y-START
    y = 100 
    
    # --- HEADER ---
    display_name = remove_emoji(username or "用户")
    header_text = f"{display_name} 的 {current_year} 音乐回顾"
    hw = draw.textlength(header_text, font=title_font)
    draw.text(((width-hw)//2, y), header_text, font=title_font, fill='white')
    
    y += 100
    
    # --- BADGE ---
    p_tag = get_personality_tag(stats)
    tw = draw.textlength(p_tag, font=tag_font)
    bw, bh = tw + 80, 56
    bx = (width - bw) // 2
    draw.rounded_rectangle([bx, y, bx+bw, y+bh], radius=28, fill=(140, 60, 220))
    
    # [V34 CHANGE] Text Centering:
    # Box Height: 56. Font Size: 32. 
    # (56 - 32) / 2 = 12. So y + 12.
    # Was y+6.
    draw.text((bx + 40, y + 10), p_tag, font=tag_font, fill='white') # y+10 looks visually centered for CJK
    
    y += 110
    
    # --- STATS ROW ---
    card_w = 280
    card_h = 130 
    gap = 30
    cx_start = (width - (card_w*3 + gap*2)) // 2
    
    stats_data = [
        ("总播放", f"{stats.get('total_plays', 0)}次"),
        ("最爱歌手", (stats.get('top_artists', [{'name': '未知'}])[0]['name'])[:6]),
        ("统计周期", f"{current_year}年")
    ]
    
    overlay = Image.new('RGBA', (width, height), (0,0,0,0))
    
    for i, (lbl, val) in enumerate(stats_data):
        cx = cx_start + i*(card_w+gap)
        cy = y
        
        card_img = Image.new('RGBA', (card_w, card_h), (0,0,0,0))
        cd = ImageDraw.Draw(card_img)
        
        # Body
        cd.rounded_rectangle([0, 0, card_w, card_h], radius=28, fill=(50, 50, 90, 80))
        
        # [V34 CHANGE] SOFT GROSS
        # Instead of sharp line at 0.5, we gradient whole height
        gloss_img = Image.new('RGBA', (card_w, card_h), (0,0,0,0))
        gd = ImageDraw.Draw(gloss_img)
        
        # Soft white gradient from top to bottom
        # Top alpha: 120 -> Bottom alpha: 0
        for j in range(card_h):
            # Non-linear fade for glass look
            # decay faster
            alpha = int(120 * (1 - (j/card_h))**2) 
            gd.line([(0, j), (card_w, j)], fill=(255, 255, 255, alpha))
        
        gloss_mask = Image.new('L', (card_w, card_h), 0)
        md = ImageDraw.Draw(gloss_mask)
        md.rounded_rectangle([0, 0, card_w, card_h], radius=28, fill=255)
        
        masked_gloss = Image.new('RGBA', (card_w, card_h), (0,0,0,0))
        masked_gloss.paste(gloss_img, (0,0), gloss_mask)
        card_img.alpha_composite(masked_gloss)
        
        # Border
        cd.rounded_rectangle([0, 0, card_w, card_h], radius=28, outline=(200, 220, 255, 120), width=2)
        
        overlay.paste(card_img, (cx, cy))

    img.alpha_composite(overlay)
    
    # Text
    for i, (lbl, val) in enumerate(stats_data):
        cx = cx_start + i*(card_w+gap)
        
        lw = draw.textlength(lbl, font=stat_lbl_font)
        draw.text((cx + (card_w-lw)//2, y + 20), lbl, font=stat_lbl_font, fill=(200,200,200))
        
        vw = draw.textlength(val, font=stat_val_font)
        draw.text((cx + (card_w-vw)//2, y + 65), val, font=stat_val_font, fill='white')
        
    y += 130 + 50
    
    # --- RANKING ---
    draw.text((50, y), "个人播放排行榜 TOP 15", font=section_font, fill='white')
    y += 70
    
    colors = [
        ((255, 80, 60), (255, 130, 90)),    # Orange
        ((40, 100, 240), (80, 150, 255)),   # Blue
        ((160, 60, 220), (200, 120, 255)),  # Purple
        ((40, 180, 120), (90, 220, 160)),   # Green
        ((240, 60, 100), (255, 120, 150)),  # Red
        ((255, 170, 40), (255, 210, 100)),  # Gold
        ((120, 100, 220), (160, 150, 255)), # Indigo
        ((200, 60, 60), (240, 120, 120)),   # Brick
        ((60, 180, 180), (120, 230, 230)),  # Teal
        ((180, 120, 80), (220, 170, 120)),  # Brown
        ((100, 60, 180), (150, 100, 240)),  # Violet
        ((80, 160, 240), (130, 200, 255)),  # Sky
        ((220, 100, 140), (255, 150, 190)), # Rose
        ((140, 180, 60), (190, 230, 100)),  # Lime
        ((160, 140, 100), (200, 180, 140)), # Tan
    ]
    
    top_songs = stats.get('top_songs', [])[:15]
    if not top_songs:
        top_songs = [{'title': 'Test Song', 'artist': 'Test Artist', 'count': 999}] * 15
    
    col_gap = 25
    margin = 40
    avail_w = width - margin*2 - col_gap
    item_w = avail_w // 2
    item_h = 85
    row_gap = 18
    
    start_y = y
    
    import os
    if not emby_url: emby_url = os.environ.get("EMBY_URL", "")
    if not emby_token: emby_token = os.environ.get("EMBY_API_KEY", "")

    for i in range(15):
        if i >= len(top_songs): break
        col = i % 2
        row = i // 2
        x = margin + col * (item_w + col_gap)
        curr_y = start_y + row * (item_h + row_gap)
        
        item = top_songs[i]
        c_i = i % len(colors)
        c_start, c_end = colors[c_i]
        
        shift_x = draw_skewed_card(img, x, curr_y, item_w, item_h, c_start, c_end, slope=slope)
        
        rank_str = f"{i+1:02d}"
        draw.text((x + 20 + shift_x, curr_y + 25), rank_str, font=rank_font, fill='white')
        
        cover_sz = 65
        cx = x + 75 + shift_x
        cy = curr_y + 10
        draw.rounded_rectangle([cx, cy, cx+cover_sz, cy+cover_sz], radius=6, fill=(0,0,0,30))
        
        cover_img = None
        if item.get('id'):
            cover_img, _ = fetch_cover_image(emby_url, item.get('id'), emby_token)
        
        if cover_img:
            cover_img = cover_img.resize((cover_sz, cover_sz))
            cmask = Image.new('L', (cover_sz, cover_sz), 0)
            dmask = ImageDraw.Draw(cmask)
            dmask.rounded_rectangle([0,0,cover_sz,cover_sz], radius=6, fill=255)
            img.paste(cover_img, (int(cx), int(cy)), cmask)

        tx = cx + cover_sz + 12
        title = remove_emoji(item.get('title', 'Unknown'))
        artist = remove_emoji(item.get('artist', 'Unknown'))
        
        if len(title) > 9: title = title[:8] + "..."
        if len(artist) > 10: artist = artist[:9] + "..."
        
        draw.text((tx, curr_y + 12), title, font=song_font, fill='white')
        draw.text((tx, curr_y + 45), artist, font=artist_font, fill=(255,255,255,220))
        
        count_str = f"{item.get('count',0)}"
        cw = draw.textlength(count_str, font=plays_font)
        rx = x + item_w + shift_x - 30 - cw
        draw.text((rx, curr_y + 10), count_str, font=plays_font, fill='white')
        lbl_w = draw.textlength("次", font=get_font(16))
        draw.text((rx + cw - lbl_w, curr_y + 35), "次", font=get_font(16), fill=(255,255,255,200))

    final_h = start_y + ((15+1)//2) * (item_h + row_gap)
    y = final_h + 40
    
    # Share Button
    btn_w = 200
    btn_h = 56
    btn_x = (width - btn_w) // 2
    draw.rounded_rectangle([btn_x, y, btn_x+btn_w, y+btn_h], radius=28, fill=(100, 140, 240))
    st = "分享"
    stw = draw.textlength(st, font=tag_font)
    draw.text((btn_x + (btn_w-stw)//2, y + 8), st, font=tag_font, fill='white')
    
    y = height - 40
    footer = "TGmusicbot · v34 VisualPolish"
    fw = draw.textlength(footer, font=get_font(18))
    draw.text((width//2 - fw//2, y), footer, font=get_font(18), fill=(100,100,120))

    output = io.BytesIO()
    img.save(output, format='PNG', quality=100)
    output.seek(0)
    return output.getvalue()

def generate_daily_ranking_image(data: Dict, emby_url: str = None, emby_token: str = None, title: str = None) -> Optional[bytes]:
    """生成每日热曲榜图片 - V36 新设计（封面网格 + 歌曲列表）"""
    if not HAS_PIL: return None
    
    width = 800
    margin = 30
    
    top_songs = data.get('top_songs', [])[:10]
    leaderboard = data.get('leaderboard', [])
    date_str = data.get('date', 'Unknown Date')
    total_mins = data.get('total_minutes', 0)
    
    if not top_songs and not leaderboard:
        return None
    
    import os
    if not emby_url: emby_url = os.environ.get("EMBY_URL", "") or os.environ.get("EMBY_SERVER_URL", "")
    if not emby_token: emby_token = os.environ.get("EMBY_API_KEY", "")
    
    # 重新计算高度
    # Row 0 top: 60. Bottom: 190.
    # Row 1 top: 235. Bottom: 365.
    # Text area: 100px.
    # Total ~465+. Setting to 500 specifically.
    height = 500 
    
    # 创建纯渐变蓝色背景 (Matching screenshot dark blue to lighter blue)
    img = Image.new('RGBA', (width, height), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    
    # 渐变背景 (深蓝 #001f3f 到 亮蓝 #0074D9 style)
    # Top: Darker, Bottom: Lighter
    for y in range(height):
        ratio = y / height
        # dark blue (0,30,60) to lighter (0,80,140)
        r = int(0 + ratio * 0)
        g = int(20 + ratio * 60)
        b = int(50 + ratio * 100)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    
    # ===== 封面网格 (2行 x 5列) =====
    # Screenshot: 5 cols centered
    cover_size = 130 # Slightly bigger
    cover_gap = 15
    covers_per_row = 5
    
    grid_w = covers_per_row * cover_size + (covers_per_row - 1) * cover_gap
    start_x = (width - grid_w) // 2
    start_y = 60 # Push down a bit for top numbers
    
    rank_font = get_font(20, bold=True)
    
    # Draw Top 10 covers
    for i in range(10): 
        if i >= len(top_songs): break
        song = top_songs[i]
        
        row = i // covers_per_row
        col = i % covers_per_row
        x = start_x + col * (cover_size + cover_gap)
        y = start_y + row * (cover_size + cover_gap + 30) # More vertical gap for numbers
        
        # Draw Rank Number ABOVE cover
        rank_str = str(i + 1)
        rw = draw.textlength(rank_str, font=rank_font)
        draw.text((x + (cover_size - rw)//2, y - 25), rank_str, font=rank_font, fill='white')
        
        # Initial Placeholder
        draw.rounded_rectangle([x, y, x + cover_size, y + cover_size], radius=4, fill=(0, 0, 0, 60))
        
        # Fetch Cover
        if song.get('id'):
            cover, _ = fetch_cover_image(emby_url, song['id'], emby_token)
            if cover:
                cover = cover.resize((cover_size, cover_size))
                # No mask, square covers in screenshot
                img.paste(cover, (x, y))
        else:
            pass
            
    # ===== 标题区域 (左下角) =====
    from bot.config import DAILY_RANKING_TITLE
    
    text_y = height - 100
    margin_left = start_x # Align with grid left
    
    # "稳健音乐热曲日榜"
    title_text = title if title else DAILY_RANKING_TITLE
    title_font = get_font(32, bold=True)
    draw.text((margin_left, text_y), title_text, font=title_font, fill='white')
    
    # "Daily Music Charts"
    subtitle = "Daily Music Charts"
    sub_font = get_font(18)
    draw.text((margin_left, text_y + 40), subtitle, font=sub_font, fill=(200, 200, 200))
    
    # Right side decorative shape? The screenshot has a dark geometric overlay on right?
    # Let's keep it simple for now, just the gradient.
    
    output = io.BytesIO()
    img.save(output, format='PNG', quality=100)
    output.seek(0)
    return output.getvalue()



def generate_ranking_image(ranking: list, title: str, date_str: str, emby_base_url: str = None) -> Optional[bytes]:
    """生成排行榜图片 (简化版)"""
    try:
        WIDTH, HEIGHT = 600, 400
        
        # 创建背景
        img = Image.new('RGBA', (WIDTH, HEIGHT), (30, 30, 40, 255))
        draw = ImageDraw.Draw(img)
        
        # 标题
        title_font = get_font(28, bold=True)
        draw.text((30, 20), title, fill=(255, 255, 255), font=title_font)
        
        # 日期
        date_font = get_font(16)
        draw.text((30, 55), date_str, fill=(180, 180, 180), font=date_font)
        
        # 排行榜列表
        y = 90
        item_font = get_font(18)
        for i, item in enumerate(ranking[:10], 1):
            artist = item.get('artist', '未知')
            song_title = item.get('title', '未知')
            count = item.get('count', 0)
            
            # 排名颜色
            if i == 1:
                rank_color = (255, 215, 0)  # 金
            elif i == 2:
                rank_color = (192, 192, 192)  # 银
            elif i == 3:
                rank_color = (205, 127, 50)  # 铜
            else:
                rank_color = (150, 150, 150)
            
            draw.text((30, y), f"{i}.", fill=rank_color, font=item_font)
            draw.text((60, y), f"{artist} - {song_title}", fill=(255, 255, 255), font=item_font)
            draw.text((WIDTH - 80, y), f"{count}次", fill=(150, 150, 150), font=item_font)
            y += 28
        
        # 输出
        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
        
    except Exception as e:
        print(f"生成排行榜图片失败: {e}")
        return None
