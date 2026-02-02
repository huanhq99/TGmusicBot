#!/usr/bin/env python3
"""
私人雷达 (Personal Radar) 模块
为每个用户生成个性化每日推荐歌单
"""

import logging
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


def analyze_user_genre_profile(playback_history: List[Dict]) -> Dict[str, float]:
    """
    分析用户的流派偏好
    
    Args:
        playback_history: 用户播放历史列表
        
    Returns:
        流派偏好比例 {genre: ratio}
    """
    if not playback_history:
        return {}
    
    genre_counts = defaultdict(int)
    total_plays = 0
    
    for item in playback_history:
        genres = item.get('Genres', [])
        if not genres:
            genres = ['其他']
        
        # 每首歌可能有多个流派，按权重分配
        weight = 1.0 / len(genres)
        for genre in genres:
            genre_counts[genre] += weight
        total_plays += 1
    
    if total_plays == 0:
        return {}
    
    # 计算比例
    genre_ratios = {}
    for genre, count in genre_counts.items():
        genre_ratios[genre] = count / total_plays
    
    # 按比例排序
    genre_ratios = dict(sorted(genre_ratios.items(), key=lambda x: x[1], reverse=True))
    
    logger.info(f"[Radar] 用户流派分析: top5={list(genre_ratios.items())[:5]}")
    return genre_ratios


def recall_songs_by_genre(library_songs: List[Dict], genre_ratios: Dict[str, float],
                          played_song_ids: set, target_count: int = 30) -> Tuple[List[Dict], List[Dict]]:
    """
    按流派比例从库中召回歌曲
    
    Args:
        library_songs: Emby 库中所有歌曲
        genre_ratios: 用户流派偏好
        played_song_ids: 用户已播放过的歌曲 ID
        target_count: 目标召回数量
        
    Returns:
        (未播放的歌曲, 已播放的歌曲)
    """
    if not library_songs or not genre_ratios:
        return [], []
    
    # 为每首歌计算匹配分数
    song_scores = []
    for song in library_songs:
        song_id = song.get('Id') or song.get('id')
        song_genres = song.get('Genres', [])
        
        # 计算流派匹配分
        score = 0.0
        if song_genres:
            for genre in song_genres:
                score += genre_ratios.get(genre, 0)
            score /= len(song_genres)  # 归一化
        else:
            score = 0.01  # 无流派的歌曲给予极低分
        
        # 加入随机性（使每天不同）
        randomness = random.random() * 0.3
        score += randomness
        
        song_scores.append({
            'song': song,
            'score': score,
            'is_played': str(song_id) in played_song_ids
        })
    
    # 按分数排序
    song_scores.sort(key=lambda x: x['score'], reverse=True)
    
    # 分离已播放和未播放
    unplayed = [s['song'] for s in song_scores if not s['is_played']]
    played = [s['song'] for s in song_scores if s['is_played']]
    
    logger.info(f"[Radar] 召回: 未播放={len(unplayed)}, 已播放={len(played)}")
    
    return unplayed, played


def generate_user_radar(user_id: str, playback_history: List[Dict], 
                        library_songs: List[Dict], target_count: int = 30) -> List[Dict]:
    """
    为用户生成私人雷达歌单
    
    Args:
        user_id: 用户 ID
        playback_history: 用户全部播放历史
        library_songs: Emby 库中所有歌曲（带流派信息）
        target_count: 歌单歌曲数
        
    Returns:
        推荐歌曲列表
    """
    # 设置随机种子（同一天同一用户结果相同）
    seed = int(datetime.now().strftime('%Y%m%d')) + hash(user_id) % 10000
    random.seed(seed)
    
    # 1. 分析用户流派偏好
    genre_ratios = analyze_user_genre_profile(playback_history)
    
    if not genre_ratios:
        logger.warning(f"[Radar] 用户 {user_id} 无播放历史，使用随机推荐")
        # 无播放历史，随机推荐
        random.shuffle(library_songs)
        return library_songs[:target_count]
    
    # 2. 获取已播放歌曲 ID
    played_song_ids = set()
    for item in playback_history:
        item_id = item.get('Id') or item.get('id')
        if item_id:
            played_song_ids.add(str(item_id))
    
    # 3. 按流派召回
    unplayed, played = recall_songs_by_genre(
        library_songs, genre_ratios, played_song_ids, target_count
    )
    
    # 4. 组合：90% 未听过 + 10% 重温
    unplayed_count = int(target_count * 0.9)
    played_count = target_count - unplayed_count
    
    final_list = []
    final_list.extend(unplayed[:unplayed_count])
    final_list.extend(played[:played_count])
    
    # 5. 如果不够，补充更多
    if len(final_list) < target_count:
        remaining = target_count - len(final_list)
        extra = unplayed[unplayed_count:unplayed_count + remaining]
        final_list.extend(extra)
    
    # 6. 打乱顺序
    random.shuffle(final_list)
    
    logger.info(f"[Radar] 用户 {user_id} 生成歌单: {len(final_list)} 首")
    
    return final_list[:target_count]
