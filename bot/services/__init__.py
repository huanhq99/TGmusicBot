#!/usr/bin/env python3
"""
服务模块
"""
from .emby import (
    authenticate_emby,
    call_emby_api,
    trigger_emby_library_scan,
    scan_emby_library,
    load_library_cache,
    get_user_emby_playlists,
    delete_emby_playlist,
    create_emby_playlist,
    get_library_data,
    get_auth,
    emby_auth,
    emby_library_data,
)

from .download_persistence import (
    init_download_queue_table,
    persist_task,
    remove_persisted_task,
    get_pending_tasks,
    update_task_status,
    clear_completed_tasks,
)

__all__ = [
    # Emby
    'authenticate_emby',
    'call_emby_api', 
    'trigger_emby_library_scan',
    'scan_emby_library',
    'load_library_cache',
    'get_user_emby_playlists',
    'delete_emby_playlist',
    'create_emby_playlist',
    'get_library_data',
    'get_auth',
    'emby_auth',
    'emby_library_data',
    # Download Persistence
    'init_download_queue_table',
    'persist_task',
    'remove_persisted_task',
    'get_pending_tasks',
    'update_task_status',
    'clear_completed_tasks',
]
