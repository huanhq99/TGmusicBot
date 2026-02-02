# TGmusicbot AI自用开发笔记

> 本文档仅供AI协助开发/维护/排查时速查，非用户说明书。

---

## 1. 主要功能点与代码入口

### 1.1 Telegram Bot 指令与主逻辑
- 入口：`bot/main.py`
- 指令注册：`app.add_handler(CommandHandler(...))` 约 40+条，见文件末尾
- 典型指令：
  - `/start` `/help` → `cmd_start`, `cmd_help`
  - `/bind` `/unbind` → `cmd_bind`, `cmd_unbind`
  - `/search` `/album` `/qqsearch` `/qqalbum` → `cmd_search`, `cmd_album`, `cmd_qq_search`, `cmd_qq_album`
  - `/schedule` `/unschedule` `/syncinterval` → 订阅相关
  - `/rescan` `/scaninterval` → Emby库刷新
  - `/request` `/myrequests` → 歌曲补全
  - `/dlstatus` `/dlqueue` `/dlhistory` → 下载队列/历史
- Inline查询：`handle_inline_query`
- 回调按钮：`CallbackQueryHandler` 多个 pattern

### 1.2 Web管理端（FastAPI）
- 入口：`bot/web.py`
- 路由注册：`@app.get/post` + `async def ..._page`/`..._api`/`..._settings`等
- 主要页面：
  - `/` 仪表盘 → `index`
  - `/playlists` → `playlists_page`
  - `/uploads` → `uploads_page`
  - `/users` → `users_page`
  - `/settings` → `settings_page`
  - `/downloads` → `downloads_page`
  - `/requests` → `requests_page`
  - `/subscriptions` → `subscriptions_page`
  - `/logs` → `logs_page`（日志实时查看）
  - `/metadata` → `metadata_page`（元数据管理）
  - `/health` → 健康检查API
- 典型API：`/api/playlists` `/api/uploads` `/api/users` `/api/downloads` `/api/subscriptions` `/api/logs` `/api/config` `/api/metadata/batch-scrape` 等
- 登录/权限：`WEB_USERNAME`/`WEB_PASSWORD`，多管理员支持，session_id

### 1.3 歌单同步/订阅
- 入口：`main.py` 中 `process_playlist()`、`scheduled_sync_job()`、`cmd_schedule`、`cmd_syncinterval`
- 歌单解析：`ncm_downloader.py`/`qq_downloader.py`/`spotify_downloader.py`（如有）
- 匹配算法：`RapidFuzz`，`MATCH_THRESHOLD`，`source_id` 字段注意
- 订阅表：`subscriptions`，定时任务由 APScheduler/自写定时器驱动
- Emby缓存：`library_cache.json`，全局变量`emby_library_data`，注意缓存刷新逻辑
- 歌单链接自动识别：`handle_message` 正则检测 → `handle_playlist_action_callback`

### 1.4 文件整理器
- 入口：`file_organizer.py`，类`DirectoryWatcher`
- 递归扫描：`self.watch_dir.rglob('*')`
- 模板变量：`{artist}` `{album}` `{title}` `{year}` `{genre}` `{track}` `{disc}`
- 冲突策略：skip/overwrite/rename
- 空文件夹清理：移动后自动递归删除
- 监控目录/目标目录配置：Web端可设

### 1.5 下载管理
- 入口：`download_manager.py`，全局`download_manager`实例
- 队列/重试/失败处理：`get_download_manager()`
- 下载历史表：`download_history`
- 下载队列持久化：`download_queue` 表 + `services/download_persistence.py`（v1.8.0新增）
- 失败自动降级音质：hires→lossless→exhigh→higher→standard

### 1.6 Emby Webhook
- 入口：`web.py` `/webhook/emby` 路由
- 通知推送：`send_telegram_notification()`
- 事件处理：`handle_library_new_item`、`handle_library_item_removed`
- 配置：`EMBY_WEBHOOK_NOTIFY` 环境变量

### 1.7 Cookie管理
- 网易云/QQ扫码：Web端二维码生成+轮询
- Cookie存储：数据库`bot_settings`表，优先读库，失效自动提醒
- 刷新/登出/状态检测API

---

## 2. 关键全局变量/配置
- `DATA_DIR`/`UPLOAD_DIR`/`MUSIC_TARGET_DIR` 路径相关
- `TELEGRAM_BOT_TOKEN`/`ADMIN_USER_ID`/`EMBY_URL`/`PLAYLIST_BOT_KEY` 等环境变量
- `emby_library_data` Emby缓存，注意多进程/多线程一致性
- `database_conn` SQLite连接，务必设置`row_factory=sqlite3.Row`避免tuple/dict混用bug
- **配置模块**：`bot/config.py` 集中管理所有配置

---

## 3. 典型业务流程
- 歌单同步：入口`process_playlist()`，先比对Emby缓存，未匹配自动下载，下载后再触发Emby扫描
- 歌单链接识别：用户发链接 → `handle_message` 正则检测 → 弹出按钮 → `handle_playlist_action_callback` → 调用`process_playlist()`
- 文件整理：`DirectoryWatcher`定时扫描，发现新文件即整理，支持递归，移动后清理空目录
- Webhook：Emby推送→`/webhook/emby`→解析事件→推送TG通知
- Cookie扫码：Web端生成二维码→App扫码→轮询API→写入数据库

---

## 4. 易错点/历史bug

### 4.1 类型安全问题（重要！）
- **`success_results` 混合类型**：可能是字符串（文件路径）或字典（包含 file/platform/song）
- 受影响函数：
  - `save_download_record_v2()` - 已修复，用 `isinstance(result, str)` 判断
  - `handle_sync_callback()` - 已修复
  - `handle_search_download_callback()` - 已修复
  - `process_batch_download()` - 已修复
- **修复模式**：
  ```python
  for r in success_results:
      if isinstance(r, str):
          file_path = r
          platform = 'NCM'
      else:
          file_path = r.get('file', '')
          platform = r.get('platform', 'NCM')
  ```

### 4.2 配置读取问题
- **`start_file_organizer_if_enabled()`**：曾有 bug 把 `organize_template` 赋值给 `target_dir`，已修复
- **配置键名不一致**：前端用 `organize_target_dir`，后端有时用 `organize_dir`，需同时保存两个键

### 4.3 其他历史 bug
- 歌曲ID字段：统一用`source_id`，部分老代码用`id`，需兼容
- SQLite返回类型：未设置`row_factory`会导致`tuple indices must be integers`错误
- Emby缓存：`emby_library_data`需及时reload，否则同步不到新歌
- 文件整理器：递归扫描需用`rglob`，否则子目录不处理
- 订阅同步间隔：单位为**分钟**，Web和Bot需同步显示
- 多管理员：`ADMIN_USER_ID`支持逗号分隔
- **Telegram 回调超时**：`query.answer()` 需用 `try/except` 包裹，防止 `Query is too old` 错误

---

## 5. 维护建议/调试入口
- 日志：Web端`/logs`可实时查看，支持级别/搜索/刷新
- 健康检查：`/health` 端点返回服务状态
- 订阅/同步问题：优先检查Emby缓存、歌单ID、日志
- 下载失败：看日志，注意音质降级、Cookie有效期
- 文件整理异常：检查目录挂载、权限、模板变量
- Webhook不通：检查环境变量、Emby配置、TG通知推送
- **全局错误处理**：`error_handler()` 函数会捕获未处理异常并通知管理员

---

## 6. 代码结构速查

```
bot/
├── main.py                    # Bot主逻辑、指令、定时任务 (~5400行)
├── web.py                     # Web管理、API、页面、/health
├── config.py                  # 集中配置管理
├── file_organizer.py          # 文件整理器
├── ncm_downloader.py          # 网易云/QQ下载、歌词下载
├── download_manager.py        # 下载队列
├── handlers/                  # 命令处理器模块（占位）
│   ├── __init__.py
│   ├── search.py              # 搜索命令（占位）
│   ├── download.py            # 下载命令（占位）
│   └── playlist.py            # 歌单命令（占位）
├── services/                  # 服务模块
│   ├── __init__.py
│   ├── emby.py                # Emby API封装
│   ├── playback_stats.py      # 播放统计
│   └── download_persistence.py # 下载队列持久化
├── utils/                     # 工具模块
│   ├── __init__.py
│   ├── database.py            # 数据库封装
│   ├── decorators.py          # @error_handler, @admin_only
│   ├── helpers.py             # 辅助函数
│   ├── progress.py            # 进度条工具
│   ├── ranking_image.py       # 排行榜图片生成
│   └── redis_client.py        # Redis客户端
├── static/css/                # CSS样式
└── templates/                 # Web模板 (10个)
data/                          # 数据库、缓存
uploads/                       # 音频文件
```

---

## 7. 版本/镜像
- 当前主线：`v1.10.9`![alt text](image.png)
- Docker镜像：`huanhq99/tgmusicbot:latest`、`huanhq99/tgmusicbot:1.10.9`、`huanhq99/music-proxy:latest`

---

## 8. 数据库表结构速查

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `bot_settings` | 全局配置 | key, value |
| `user_bindings` | 用户绑定 | telegram_id, emby_user_id, emby_token |
| `scheduled_playlists` | 订阅歌单 | playlist_url, platform, sync_interval, last_song_ids |
| `download_history` | 下载历史 | song_id, title, artist, platform, status |
| `download_queue` | 下载队列 | song_id, status, progress, retry_count |
| `song_requests` | 歌曲申请 | song_name, artist, status, admin_note |
| `playlist_records` | 歌单记录 | playlist_id, name, track_count |
| `upload_records` | 上传记录 | file_name, file_size, user_id |

---

## 9. 定时任务一览

| 任务 | 函数 | 触发位置 |
|------|------|----------|
| 歌单同步 | `scheduled_sync_job()` | `post_init` |
| Emby扫描 | `scheduled_emby_scan_job()` | `post_init` |
| 排行榜生成 | `scheduled_ranking_job()` | `post_init` |
| 每日统计 | `daily_stats_job()` | `post_init` |
| Webhook通知 | `emby_webhook_notify_job()` | `post_init` |
| 文件整理 | `start_file_organizer_if_enabled()` | `post_init` |

---

## 10. 新模块使用示例

```python
# 导入配置
from bot.config import APP_VERSION, TELEGRAM_TOKEN, EMBY_URL

# 使用装饰器
from bot.utils import error_handler, admin_only

@error_handler
@admin_only
async def my_admin_command(update, context):
    ...

# 数据库操作
from bot.utils import get_database
db = get_database()
db.set_setting('key', 'value')

# Emby 服务
from bot.services.emby import authenticate_emby, scan_emby_library

# 下载队列持久化
from bot.services import persist_task, get_pending_tasks
```

---

## 11. 最近修复记录（2026-01）

| 问题 | 修复位置 | 修复方式 |
|------|----------|----------|
| `QQMusicAPI` 未定义 | `ncm_downloader.py` | 移除无效引用 |
| `download_missing_songs` 参数不匹配 | `ncm_downloader.py` | 添加缺失参数 |
| 回调超时崩溃 | `main.py` | `query.answer()` 加 try/except |
| 目标目录被模板覆盖 | `main.py` | 删除错误赋值代码 |
| `target_base` 未定义 | `file_organizer.py` | 改为 `target_dir` |
| `success_results` 类型错误 | 多处 | 添加 `isinstance` 检查 |
| 歌单链接无响应 | `main.py` | `handle_message` 使用 `parse_playlist_input()` |
| 短链接不支持 | `main.py` | 复用现有的 `_resolve_short_url()` |
| 下载不检查库 | `main.py` | 调用 `process_playlist()` 先同步 Emby |
---

## 12. 2026-01-17 重大更新：QQ 音乐代理下载与 Cookie 保活

### 12.1 QQMusicAPI 类重构 (`ncm_downloader.py`)

**完全重写的 `QQMusicAPI` 类**，解决 QQ 音乐下载功能缺失问题：

| 方法 | 功能 |
|------|------|
| `__init__(cookie, proxy_url, proxy_key)` | 初始化，支持代理配置 |
| `set_cookie(cookie)` | 设置 Cookie |
| `search_song(keyword)` | 搜索歌曲 |
| `get_download_url(song_mid, quality)` | 获取下载链接（支持代理） |
| `download_song(song_mid, song_info, output_dir, quality)` | 下载单曲（带音质降级） |
| `batch_download(songs, output_dir, quality, callback...)` | 批量下载 |
| `check_login()` | 验证 Cookie（多层回退） |
| `refresh_cookie()` | 刷新 Cookie |
| `_get_gtk()` | 计算 g_tk |
| `_get_uin_from_cookie()` | 提取 UIN |

### 12.2 代理流式下载机制

**问题**：海外服务器直接访问 QQ CDN 返回 404。

**解决方案**：使用 `/qq/download/<mid>` 端点，代理服务器在国内下载后流式回传：

```
Bot(海外) → Proxy(国内) → QQ CDN → Proxy → Bot
```

**代码流程**：
1. `get_download_url()` 返回 `"PROXY_STREAM"` 标记
2. 同时存储请求参数到 `self._last_proxy_request`
3. `download_song()` 检测标记后从 `_last_proxy_request` 读取参数
4. 直接从代理流式下载（timeout=120s）

**关键配置**：
- `MUSIC_PROXY_URL`：代理服务器地址，如 `https://music.xxx.xyz:7777`
- `MUSIC_PROXY_KEY`：API 密钥

### 12.3 音质自动降级

```python
quality_fallback = {
    'hires': ['hires', 'lossless', 'exhigh', 'standard'],
    'lossless': ['lossless', 'exhigh', 'standard'],
    'exhigh': ['exhigh', 'standard'],
    'standard': ['standard'],
}
```

当高音质返回 404 时自动尝试下一级音质。

### 12.4 Cookie 验证多层回退 (`check_login`)

1. **主尝试**：`get_profile` 用户信息
2. **回退1**：`get_u_songlist` 用户歌单
3. **回退2**：`get_global_config` 全局配置（最宽松）

解决 `subcode=860100005` 权限错误导致验证失败的问题。

### 12.5 Cookie 自动保活

```python
# main.py
async def refresh_qq_cookie_task(application):
    while True:
        await asyncio.sleep(6 * 3600)  # 每6小时
        api = QQMusicAPI(cookie)
        api.refresh_cookie()
```

### 12.6 MusicAutoDownloader session 修复

**问题**：`get_qq_lyrics` 调用 `self.session` 但未初始化。

**修复**：在 `__init__` 中添加：
```python
self.session = requests.Session()
self.session.headers.update({...})
```

---

## 13. 2026-01-17 其他修复

| 问题 | 修复位置 | 修复方式 |
|------|----------|----------|
| `query.data` 不可修改 | `main.py` handle_retry_callback | 内联下载逻辑，不修改 CallbackQuery |
| 代理 API 端点错误 | `ncm_downloader.py` | 改用 `/qq/download/<mid>` 流式下载 |
| JSON 解析错误 | `ncm_downloader.py` | 用实例变量替代字符串编码 |
| `is_proxy_stream` 检测失败 | `ncm_downloader.py` | 改为 `== 'PROXY_STREAM'` |
| 歌词获取失败 | `ncm_downloader.py` | 添加 `self.session` 初始化 |

---

## 14. 代理服务器 API 速查 (`proxy-server/app.py`)

| 端点 | 方法 | 功能 | 必要Header |
|------|------|------|------------|
| `/health` | GET | 健康检查 | - |
| `/qq/url/<mid>` | GET | 获取下载URL | X-API-Key, X-QQ-Cookie |
| `/qq/download/<mid>` | GET | 流式下载 | X-API-Key, X-QQ-Cookie |
| `/qq/diagnose` | GET | Cookie诊断 | X-API-Key, X-QQ-Cookie |
| `/ncm/url/<id>` | GET | 网易云URL | X-API-Key, X-NCM-Cookie |
| `/ncm/download/<id>` | GET | 网易云下载 | X-API-Key, X-NCM-Cookie |

---

## 15. 2026-01-20~21 更新：Logger 修复与文件搜索优化

### 15.1 Logger 未定义错误修复

**问题**：多个模块中 `logger` 未正确导入或初始化，导致运行时 `NameError`。

**修复位置**：
- `bot/file_organizer.py` (120-133行)
  - 简化调试日志，将 `logger.warning` 改为 `print` 输出
  - 避免在函数作用域内的 logger 定义问题
  
- `bot/web.py` (3869-3875行)
  - `api_organize_current_dir` 函数中添加 `import logging` 和 logger 初始化

**最佳实践**：
```python
# 方式1：模块级 logger（推荐）
import logging
logger = logging.getLogger(__name__)

# 方式2：函数内 logger（需谨慎）
def my_function():
    import logging
    logger = logging.getLogger(__name__)
    logger.info("...")

# 方式3：直接 print（调试用）
print(f"[Debug] message", flush=True)
```

### 15.2 元数据浏览器搜索功能演进

#### 阶段1：递归搜索尝试

**目标**：支持在所有子目录中搜索文件和文件夹。

**实现**：
- 新增 `/api/metadata/search_files` API 端点
- 使用 `Path.rglob('*')` 递归扫描
- 支持搜索文件夹和音频文件
- 超时保护：5秒 → 10秒 → 30秒

**代码示例**：
```python
@app.get("/api/metadata/search_files")
async def metadata_search_files(query: str, base_dir: str = ""):
    for item_path in base_path.rglob('*'):
        if query_lower in item_path.name.lower():
            results.append({...})
```

#### 阶段2：性能问题发现

**测试环境**：
- 总文件数：31,199 首歌曲
- 存储类型：CloudDrive 网络挂载（`/music`）
- 搜索超时：30秒

**实测结果**：
```
[Search] 开始搜索: query='爱怎么了', base_dir='/music'
[Search] 完成: scanned=1140, matched=0, results=0, time=30.25s
```

**瓶颈分析**：
- 网络存储文件访问延迟极高
- 30秒仅扫描 1,140 个项目（仅 3.7%）
- 文件系统调用（`stat()`, `is_dir()`）成为性能瓶颈
- 无法在合理时间内完成全盘扫描

#### 阶段3：回退到本地过滤

**最终方案**：恢复为简单的本地过滤，仅在当前显示的文件列表中搜索。

**代码**：
```javascript
document.getElementById('dir-search').addEventListener('input', function () {
    const query = this.value.toLowerCase().trim();
    document.querySelectorAll('#dir-list .file-item').forEach(el => {
        const name = el.textContent.toLowerCase();
        el.style.display = (query === '' || name.includes(query)) ? '' : 'none';
    });
});
```

**优势**：
- 即时响应，无延迟
- 不依赖后端 API
- 适合网络存储环境

### 15.3 替代方案展望

针对全局文件搜索需求，可考虑：

1. **搜索 Emby 数据库**（推荐）
   - Emby 已索引全部 31,199 首歌曲
   - 秒级响应
   - 可直接在 Emby 中实现

2. **建立本地索引**
   - 定期后台扫描生成文件索引
   - 存储到 SQLite 数据库
   - 提供快速 FTS 全文搜索

3. **增量索引更新**
   - 监听 Emby Webhook 事件
   - 仅索引变化的文件
   - 保持索引实时性

### 15.4 经验教训

| 问题 | 教训 |
|------|------|
| 网络存储递归扫描 | 绝不在网络挂载上使用 `rglob()` |
| 实时文件搜索 | 大型媒体库必须使用数据库索引 |
| 超时设置 | 超时再长也无法弥补存储延迟 |
| 用户体验 | 简单快速的方案优于复杂慢速的方案 |

---

> 本文档仅供AI协助开发/维护/排查时速查，勿对外公开。

