import requests
import sqlite3
import time
import json
import os

# Database path inside container
DB_PATH = '/app/data/bot.db'

def get_ncm_cookie():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key='ncm_cookie'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"Error reading DB cookie: {e}")
        return None

def debug_ncm_user():
    cookie = get_ncm_cookie()
    if not cookie:
        print("❌ No NCM Cookie found in DB!")
        return

    print(f"🍪 Loaded Cookie: {cookie[:20]}...")
    
    headers = {
        'Referer': 'https://music.163.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Cookie': cookie,
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }

    # 1. Get User Info (UID)
    print("👤 Fetching User Info...")
    try:
        # endpoint for web usually works
        resp = requests.get("https://music.163.com/api/v1/user/info", headers=headers, timeout=10)
        data = resp.json()
        if 'userPoint' in data: # sometimes structure differs
             pass
        # try another if fails, but let's see response
        # "code": 200, "userPoint": {... "userId": ...}
        # Or standard /api/user/account
        
    except Exception as e:
        print(f"Error fetching user info: {e}")
        return

    # Try explicit account endpoint
    try:
        resp = requests.get("https://music.163.com/api/user/account", headers=headers, timeout=10)
        account_data = resp.json()
        uid = account_data.get('account', {}).get('id')
        if not uid:
            print("❌ Could not get User ID from /api/user/account")
            print(json.dumps(account_data, ensure_ascii=False)[:200])
            return
        
        print(f"✅ User ID: {uid}")
        
        # 2. Get User Playlists
        print(f"📂 Fetching Playlists for UID {uid}...")
        resp = requests.get(f"https://music.163.com/api/user/playlist?uid={uid}&limit=100", headers=headers, timeout=10)
        pl_data = resp.json()
        playlists = pl_data.get('playlist', [])
        print(f"   Found {len(playlists)} playlists.")
        
        target_pl = None
        for pl in playlists:
            print(f"   - [{pl['id']}] {pl['name']} (Count: {pl['trackCount']})")
            if "喜欢的音乐" in pl['name']:
                target_pl = pl
        
        if target_pl:
            print(f"\n🎯 Target Playlist Found: [{target_pl['id']}] {target_pl['name']}")
            print(f"   TrackCount (Metadata): {target_pl['trackCount']}")
            
            # 3. Fetch Detail (The real test)
            debug_playlist_detail(target_pl['id'], headers)
            
    except Exception as e:
        print(f"Error in flow: {e}")

def debug_playlist_detail(playlist_id, headers):
    url = "https://music.163.com/api/v3/playlist/detail"
    params = {'id': playlist_id, 'n': 100000, 'timestamp': int(time.time() * 1000)}
    
    print(f"\n🔍 Fetching Playlist Detail (API v3) for {playlist_id}...")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        
        playlist = data.get('playlist')
        if not playlist:
            print("❌ No playlist object in response")
            return
            
        track_ids = playlist.get('trackIds', [])
        tracks = playlist.get('tracks', [])
        
        print(f"✅ API Response Code: {data.get('code')}")
        print(f"📋 trackIds Length: {len(track_ids)}")
        print(f"🎵 tracks Length: {len(tracks)}")
        
        if len(track_ids) != playlist.get('trackCount'):
             print(f"⚠️ MISMATCH: Meta says {playlist.get('trackCount')} but trackIds has {len(track_ids)}")
        else:
             print(f"✅ MATCH: Meta count {playlist.get('trackCount')} == trackIds {len(track_ids)}")

        # Print first few track IDs
        print(f"First 5 IDs: {[t['id'] for t in track_ids[:5]]}")

    except Exception as e:
        print(f"Request Failed: {e}")

if __name__ == "__main__":
    debug_ncm_user()
