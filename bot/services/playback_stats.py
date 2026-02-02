#!/usr/bin/env python3
"""播放统计服务 - 从 Emby Playback Reporting 插件获取数据"""

import sqlite3
import logging
import os
import requests
from bot.services.emby import get_all_users
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class PlaybackStats:
    """播放统计服务"""
    
    def __init__(self, db_path: str = None, emby_url: str = None, emby_token: str = None):
        self.db_path = db_path
        self.emby_url = emby_url or os.environ.get('EMBY_URL', '') or os.environ.get('EMBY_SERVER_URL', '')
        self.emby_token = emby_token or os.environ.get('EMBY_API_KEY', '')
        
        # 如果没有 token，尝试从数据库获取
        if not self.emby_token and db_path:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'emby_token'")
                row = cursor.fetchone()
                if row:
                    self.emby_token = row['value']
                conn.close()
            except:
                pass
        
        if db_path:
            self._init_local_table()
    
    def _init_local_table(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS playback_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT, telegram_id TEXT, item_id TEXT,
                    title TEXT, artist TEXT, album TEXT,
                    album_id TEXT, cover_url TEXT, play_date DATE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"初始化本地表失败: {e}")
    
    def _call_emby_api(self, path: str, params: dict = None) -> Optional[Any]:
        """调用 Emby Playback Reporting API"""
        if not self.emby_url:
            logger.warning("[PlaybackStats] Emby URL 未配置")
            return None
        if not self.emby_token:
            logger.warning("[PlaybackStats] Emby API Key 未配置")
            return None
        
        try:
            url = f"{self.emby_url.rstrip('/')}/emby{path}"
            params = params or {}
            params['api_key'] = self.emby_token
            
            logger.info(f"[PlaybackStats] 调用: {path}")
            resp = requests.get(url, params=params, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                count = len(data) if isinstance(data, list) else 'object'
                logger.info(f"[PlaybackStats] 成功, 返回 {count} 条")
                return data
            else:
                logger.error(f"[PlaybackStats] API 返回 {resp.status_code}")
                if resp.status_code == 401:
                    logger.error("[PlaybackStats] API Key 无效或无权限")
        except Exception as e:
            logger.error(f"[PlaybackStats] 调用失败: {e}")
        
        return None
    
    def get_user_stats_from_emby(self, user_id: str = None) -> Dict:
        """从 Emby Playback Reporting 获取用户统计"""
        end_date = datetime.now().strftime('%Y-%m-%d')
        
        # 使用 UserPlaylist API 获取用户播放列表
        params = {
            'days': 365,
            'end_date': end_date,
            'filter': 'Audio',
            'aggregate_data': 'false'
        }
        if user_id:
            params['user_id'] = user_id
        
        data = self._call_emby_api('/user_usage_stats/UserPlaylist', params)
        
        if data and isinstance(data, list) and len(data) > 0:
            logger.info(f"[Debug] Sample Data: {data[0]}")
            total = len(data)
            artists = {}
            songs = {}
            
            for item in data:
                # 获取字段 (可能是 Name/ItemName, Artist/AlbumArtist 等)
                artist = item.get('Artist') or item.get('AlbumArtist') or item.get('ArtistName')
                title = item.get('Name') or item.get('ItemName') or item.get('Title')
                
                # 如果标准字段为空，尝试从 item_name 解析 ("歌手 - 歌名 (专辑)")
                if not artist and item.get('item_name'):
                    try:
                        parts = item['item_name'].split(' - ', 1)
                        if len(parts) >= 2:
                            artist = parts[0].strip()
                            rest = parts[1].strip()
                            # 尝试匹配末尾的专辑信息 (...)
                            import re
                            match = re.match(r'(.*)\s+\((.*)\)$', rest)
                            if match:
                                title = match.group(1).strip()
                            else:
                                title = rest
                    except:
                        pass
                
                artist = artist or 'Unknown'
                title = title or 'Unknown'
                
                artists[artist] = artists.get(artist, 0) + 1
                key = f"{title}|{artist}"
                item_id = item.get('item_id') or item.get('ItemId') or item.get('Id')
                if key not in songs:
                    songs[key] = {'title': title, 'artist': artist, 'count': 0, 'id': item_id}
                songs[key]['count'] += 1
            
            top_artists = sorted([{'name': k, 'count': v} for k, v in artists.items()], 
                                key=lambda x: x['count'], reverse=True)[:15]
            top_songs = sorted(songs.values(), key=lambda x: x['count'], reverse=True)[:15]
            
            logger.info(f"[PlaybackStats] 用户统计: {total} 条, {len(artists)} 艺术家")
            return {
                'total_plays': total,
                'top_artists': top_artists,
                'top_songs': top_songs
            }
        
        # 如果 UserPlaylist 失败，尝试 submit_custom_query
        logger.info("[PlaybackStats] 尝试自定义查询...")
        query_result = self._call_custom_query(user_id)
        if query_result:
            return query_result
        
        return {'total_plays': 0, 'top_artists': [], 'top_songs': []}
    
    def _call_custom_query(self, user_id: str = None) -> Optional[Dict]:
        """使用自定义 SQL 查询获取统计"""
        if not self.emby_url or not self.emby_token:
            return None
        
        try:
            url = f"{self.emby_url.rstrip('/')}/emby/user_usage_stats/submit_custom_query"
            
            # 查询音频播放记录
            sql = """
                SELECT 
                    ItemId,
                    ItemName as title,
                    CASE WHEN ItemType = 'Audio' THEN ItemName ELSE '' END as artist,
                    COUNT(*) as play_count
                FROM PlaybackActivity
                WHERE ItemType = 'Audio'
            """
            if user_id:
                sql += f" AND UserId = '{user_id}'"
            sql += " GROUP BY ItemId ORDER BY play_count DESC LIMIT 50"
            
            resp = requests.post(url, json={
                'CustomQueryString': sql,
                'ReplaceUserId': False
            }, params={'api_key': self.emby_token}, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                results = data.get('results', [])
                if results:
                    total = sum(r.get('play_count', 0) for r in results)
                    top_songs = [{'title': r.get('title', ''), 'artist': r.get('artist', ''), 'count': r.get('play_count', 0), 'id': r.get('ItemId') or r.get('item_id')} for r in results[:15]]
                    
                    logger.info(f"[PlaybackStats] 自定义查询成功: {len(results)} 条")
                    return {
                        'total_plays': total,
                        'top_artists': [],
                        'top_songs': top_songs
                    }
        except Exception as e:
            logger.error(f"[PlaybackStats] 自定义查询失败: {e}")
        
        return None
    
    def get_ranking_from_emby(self, period: str = 'day', limit: int = 10) -> List[Dict]:
        """获取排行榜"""
        days = {'day': 1, 'week': 7, 'month': 30}.get(period, 1)
        end_date = datetime.now().strftime('%Y-%m-%d')
        
        # 使用 PlayActivity API
        data = self._call_emby_api('/user_usage_stats/PlayActivity', {
            'days': days,
            'end_date': end_date,
            'filter': 'Audio',
            'data_type': 'count'
        })
        
        if data and isinstance(data, list):
            play_count = {}
            for item in data:
                title = item.get('Name') or item.get('label') or ''
                artist = item.get('Artist') or item.get('AlbumArtist') or ''
                key = f"{title}|{artist}"
                
                if key not in play_count:
                    play_count[key] = {'title': title, 'artist': artist, 'album': '', 'cover_url': '', 'count': 0}
                play_count[key]['count'] += item.get('count', 1)
            
            result = sorted(play_count.values(), key=lambda x: x['count'], reverse=True)
            return result[:limit]
        
        return []
    
    def get_ranking(self, period: str = 'day', limit: int = 10) -> List[Dict]:
        return self.get_ranking_from_emby(period, limit)
    
    def get_user_stats(self, user_id: str = None, telegram_id: str = None) -> Dict:
        return self.get_user_stats_from_emby(user_id=user_id)
    
    def record_playback(self, user_id: str, telegram_id: str, item_id: str,
                       title: str, artist: str, album: str, album_id: str = '',
                       cover_url: str = '') -> bool:
        if not self.db_path:
            return False
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO playback_records 
                (user_id, telegram_id, item_id, title, artist, album, album_id, cover_url, play_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, DATE('now'))
            """, (user_id, telegram_id, item_id, title, artist, album, album_id, cover_url))
            conn.commit()
            conn.close()
            return True
        except:
            return False
    
    def get_yearly_summary(self, year: int, user_id: str = None, telegram_id: str = None) -> Dict:
        result = self.get_user_stats_from_emby(user_id=user_id)
        result['year'] = year
        result['unique_songs'] = len(result.get('top_songs', []))
        return result



    def get_global_stats(self, days: int = 1) -> Dict:
        """获取全服播放统计 (智能去重)"""
        try:
            users = get_all_users()
            logger.info(f"[GlobalStats] Found {len(users)} users")
            
            end_date = datetime.now().strftime('%Y-%m-%d')
            # Calculate start date for strict filtering
            start_dt = datetime.now() - timedelta(days=days-1) # inclusive
            start_date_str = start_dt.strftime('%Y-%m-%d')
            
            logger.info(f"[GlobalStats] Days={days}, Range: {start_date_str} to {end_date}")
            
            ranking_list = []
            global_song_counts = {}
            total_server_ticks = 0
            
            for user in users:
                uid = user['id']
                uname = user['name']
                
                # Request data from Emby
                params = {
                    'days': days,
                    'end_date': end_date,
                    'filter': 'Audio',
                    'user_id': uid,
                    'Fields': 'Id,Name,Artist,AlbumArtist,RunTimeTicks,Date' 
                }
                
                # Fetch data
                data = self._call_emby_api('/user_usage_stats/UserPlaylist', params)
                
                user_ticks = 0
                merged_sessions = []  # Initialize here to prevent UnboundLocalError
                if data and isinstance(data, list):
                    if len(data) > 0:
                        logger.info(f"[Debug] First Raw Item: {data[0]}")
                        
                    print(f"DEBUG_DATE_RANGE: Start={start_date_str}, End={end_date}")
                    valid_items = []
                    
                    for item in data:
                        # 解析日期 - 兼容两种格式
                        item_dt = None
                        local_date_str = None
                        
                        try:
                            from zoneinfo import ZoneInfo
                            import os
                            local_tz = ZoneInfo(os.environ.get('TZ', 'Asia/Shanghai'))
                            
                            if item.get('Date'):
                                # ISO格式: "2023-01-01T12:00:00Z" (UTC) 或 "2023-01-01T12:00:00+08:00"
                                dt_str = item['Date']
                                item_dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                                # 转换为本地时区后再提取日期
                                if item_dt.tzinfo:
                                    item_dt = item_dt.astimezone(local_tz)
                                local_date_str = item_dt.strftime('%Y-%m-%d')
                            elif item.get('date'):
                                # 简单格式: date="2023-01-01", time="12:00:00" (假设已经是本地时间)
                                local_date_str = item['date']
                                if item.get('time'):
                                    item_dt = datetime.fromisoformat(f"{item['date']}T{item['time']}")
                                else:
                                    item_dt = datetime.fromisoformat(item['date'])
                        except Exception as e:
                            print(f"[DateCheck] Error parsing date: {e}")
                            continue
                            
                        if not item_dt or not local_date_str:
                            print(f"[DateCheck] Missing date info for item: {item.get('Name') or item.get('item_name')}")
                            continue

                        # Debug: 显示过滤逻辑
                        if len(valid_items) < 5:
                            print(f"[DateCheck] Item: {item.get('Name')} | RawDate: {item.get('Date')} | Local: {local_date_str} | InRange: {start_date_str} <= {local_date_str} <= {end_date}")
                        
                        if local_date_str < start_date_str or local_date_str > end_date:
                            continue
                        item['parsed_date'] = item_dt
                        valid_items.append(item)

                    logger.info(f"[GlobalStats] User {uname}: {len(data)} total -> {len(valid_items)} in date range")
                    
                    # Sort time ascending
                    valid_items.sort(key=lambda x: x['parsed_date'])
                    
                    # Deduplication Logic
                    merged_sessions = []
                    last_session = None
                    
                    for item in valid_items:
                        iid = item.get('item_id') or item.get('Id')
                        
                        # Process Name/Artist
                        name = item.get('Name') or item.get('item_name') or 'Unknown'
                        artist = item.get('Artist') or item.get('AlbumArtist') or item.get('ArtistName')
                        
                        # 如果没有 Artist 字段，尝试从 item_name 解析 ("歌手 - 歌名")
                        if not artist and ' - ' in name:
                            try:
                                parts = name.split(' - ', 1)
                                artist = parts[0].strip()
                                # name 保持原样或者也做分割，这里保持原样后续处理
                            except:
                                artist = 'Unknown'
                        
                        if not artist:
                            artist = 'Unknown'

                        # Process Duration
                        item_ticks = item.get('RunTimeTicks', 0)
                        if item_ticks == 0 and item.get('duration'):
                            try:
                                # duration 可能是秒字符串
                                item_ticks = int(float(item['duration']) * 10000000)
                            except:
                                pass
                                
                        item_dt = item['parsed_date']
                        
                        # Fix metadata
                        if name.startswith("Not Known - "):
                            name = name.replace("Not Known - ", "", 1)
                        if name.endswith(" (Not Known)"):
                            name = name[:-len(" (Not Known)")]
                            
                        key = iid if iid else f"{name}|{artist}"
                        
                        is_merged = False
                        if last_session:
                            last_key = last_session['key']
                            last_time = last_session['end_time']
                            gap = (item_dt - last_time).total_seconds()
                            
                            if key == last_key and gap < 300:
                                last_session['ticks'] += item_ticks
                                last_session['end_time'] = item_dt
                                is_merged = True
                        
                        if not is_merged:
                            session = {
                                'key': key, 'id': iid, 'title': name, 'artist': artist,
                                'ticks': item_ticks, 'start_time': item_dt, 'end_time': item_dt
                            }
                            merged_sessions.append(session)
                            last_session = session

                    # Count Stats
                    logger.info(f"[GlobalStats] Merged into {len(merged_sessions)} sessions")
                    
                    # Count Stats
                    for session in merged_sessions:
                        if session['ticks'] < 100000000: # < 10s
                            logger.info(f"[DurationFilter] Skipping {session['title']} - Too short ({session['ticks']} ticks)")
                            continue
                            
                        user_ticks += session['ticks']
                        key = session['key']
                        
                        s_title = session['title']
                        s_artist = session['artist']
                        
                        if s_artist == 'Unknown' and ' - ' in s_title:
                            try:
                                parts = s_title.split(' - ')
                                if len(parts) >= 2:
                                    s_artist = parts[0].strip()
                                    s_title = ' - '.join(parts[1:]).strip()
                            except:
                                pass

                        if key not in global_song_counts:
                            global_song_counts[key] = {
                                'title': s_title, 'artist': s_artist, 'id': session['id'], 
                                'count': 0, 'need_fetch': session['id'] is not None
                            }
                        global_song_counts[key]['count'] += 1
                
                minutes = int(user_ticks / 600000000) 
                
                if merged_sessions:
                    ranking_list.append({
                        'name': uname,
                        'minutes': minutes
                    })
                    total_server_ticks += user_ticks
            
            ranking_list.sort(key=lambda x: x['minutes'], reverse=True)
            
            top_songs = []
            if global_song_counts:
                valid_songs = [s for s in global_song_counts.values()]
                sorted_songs = sorted(valid_songs, key=lambda x: x['count'], reverse=True)
                top_songs = sorted_songs[:10]
                
                # Enrich metadata
                for song in top_songs:
                    if song.get('need_fetch'):
                        try:
                            item_data = self._call_emby_api(f"/Items?Ids={song['id']}&Fields=ArtistItems,AlbumArtist,Album")
                            if item_data and 'Items' in item_data and len(item_data['Items']) > 0:
                                item_details = item_data['Items'][0]
                                song['title'] = item_details.get('Name', song['title'])
                                artists = item_details.get('ArtistItems', [])
                                if artists:
                                    song['artist'] = "/".join([a.get('Name', '') for a in artists])
                                else:
                                    song['artist'] = item_details.get('AlbumArtist', 'Unknown')
                                song['album'] = item_details.get('Album', '')
                        except:
                            pass
            
            logger.info(f"[GlobalStats] Final: ranking_list={len(ranking_list)}, top_songs={len(top_songs)}, total_minutes={int(total_server_ticks / 600000000)}")
            
            return {
                'date': f"{start_date_str} ~ {end_date}" if days > 1 else end_date,
                'leaderboard': ranking_list[:10],
                'top_songs': top_songs,
                'top_song': top_songs[0] if top_songs else None,
                'total_minutes': int(total_server_ticks / 600000000),
                'debug_keys': []
            }
            
        except Exception as e:
            logger.error(f"[GlobalStats] Failed: {e}")
            return {}

    def get_global_daily_stats(self) -> Dict:
        return self.get_global_stats(days=1)

    def get_global_weekly_stats(self) -> Dict:
        return self.get_global_stats(days=7)


_stats_instance: Optional[PlaybackStats] = None

def get_playback_stats(db_path: str = None) -> PlaybackStats:
    global _stats_instance
    if _stats_instance is None:
        if db_path is None:
            try:
                from bot.config import DATABASE_FILE
                db_path = str(DATABASE_FILE)
            except:
                db_path = None
        _stats_instance = PlaybackStats(db_path)
    return _stats_instance
