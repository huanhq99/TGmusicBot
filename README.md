# 🎵 TGmusicbot

Telegram 音乐管理机器人 - 同步歌单到 Emby + 自动下载缺失歌曲

## ✨ 功能特性

### 🎵 歌单同步
- 支持 **网易云音乐**、**QQ音乐**、**Spotify** 歌单
- 自动匹配 Emby 媒体库歌曲
- 支持模糊匹配和完全匹配模式
- 自动创建 Emby 歌单

### 📥 自动下载
- 自动下载 Emby 中缺失的歌曲
- 支持网易云 + QQ音乐双平台（跨平台自动切换）
- 网易云下载失败自动尝试 QQ 音乐
- 多种音质可选：标准/高品质/无损/Hi-Res/Master
- 下载完成自动触发 Emby 扫库

### 🔐 账号登录
- 网易云：扫码登录 / Cookie 登录
- QQ音乐：扫码登录 / Cookie 登录
- Cookie 刷新功能延长有效期

### 🔍 搜索下载
- `/search <关键词>` - 搜索并下载单曲
- `/album <专辑名>` - 搜索并下载整张专辑

### 📅 定时同步
- 自动订阅已同步的歌单
- 每 6 小时检查歌单更新
- 发现新歌曲自动通知
- 一键下载新增歌曲

### 🔄 Emby 联动
- 可配置定时自动扫描 Emby 媒体库
- 支持 Emby Webhook 实时入库通知
- Web 管理界面可视化配置

### 📤 音乐上传
- 通过 Telegram 发送音频文件
- 自动保存到服务器
- 支持 MP3, FLAC, M4A, WAV 等格式
- 支持大文件上传（需配置 Pyrogram）

### 📝 歌曲补全申请
- 用户可申请下载缺失的歌曲
- 用户可申请同步歌单（需管理员审批）
- 管理员 Telegram/Web 审核
- 审核结果自动通知用户

### 🖥️ Web 管理界面
- 仪表盘总览
- 网易云/QQ音乐 扫码登录
- 下载音质设置
- 下载历史记录
- 文件自动整理配置

## 🚀 快速部署

### Docker 部署 (推荐)

1. **创建配置文件**

```bash
mkdir tgmusicbot && cd tgmusicbot

# 创建 .env 配置文件
cat > .env << 'EOF'
# Telegram 配置
TELEGRAM_TOKEN=你的Bot_Token
ADMIN_USER_ID=你的Telegram_ID

# Emby 配置
EMBY_URL=http://你的emby地址:8096
EMBY_USERNAME=emby用户名
EMBY_PASSWORD=emby密码

# 加密密钥（随机字符串，用于加密存储密码）
PLAYLIST_BOT_KEY=your-random-secret-key-here
EOF
```

2. **创建 docker-compose.yml**

```yaml
services:
  tgmusicbot:
    # 使用 Docker Hub 镜像
    image: huanhq99/tgmusicbot:latest
    container_name: tgmusicbot
    restart: unless-stopped
    
    ports:
      - "8080:8080"  # Web 管理界面
    
    volumes:
      - ./data:/app/data              # 数据库、缓存、日志
      - ./uploads:/app/uploads        # 下载的音乐文件
      # 文件自动整理（可选）
      # - /path/to/music:/music       # 整理目标目录
    
    environment:
      - TZ=Asia/Shanghai
      - DATA_DIR=/app/data
      - UPLOAD_DIR=/tmp/tgmusicbot_uploads
      - MUSIC_TARGET_DIR=/app/uploads
      # Telegram 配置
      - TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
      - ADMIN_USER_ID=${ADMIN_USER_ID}
      # Pyrogram 大文件上传支持（可选，可上传超过 20MB 的文件）
      # - TG_API_ID=${TG_API_ID}
      # - TG_API_HASH=${TG_API_HASH}
      # Telegram Local Bot API Server（可选，支持上传大文件）
      # - TELEGRAM_API_URL=http://telegram-bot-api:8081/bot
      # Web 管理界面登录（强烈建议设置）
      - WEB_USERNAME=${WEB_USERNAME:-admin}
      - WEB_PASSWORD=${WEB_PASSWORD}
      # Emby 配置
      - EMBY_URL=${EMBY_URL}
      - EMBY_USERNAME=${EMBY_USERNAME}
      - EMBY_PASSWORD=${EMBY_PASSWORD}
      # Emby 自动扫描间隔（小时，0=禁用）
      # - EMBY_SCAN_INTERVAL=6
      # 加密密钥
      - PLAYLIST_BOT_KEY=${PLAYLIST_BOT_KEY}
    
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

3. **启动服务**

```bash
docker-compose up -d
```

4. **访问管理界面**: `http://localhost:8080`

### 本地运行

```bash
git clone https://github.com/huanhq99/TGmusicBot.git
cd TGmusicBot
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填写配置

# 启动
./start.sh
```

## 📱 Telegram 命令

| 短命令 | 完整命令 | 说明 |
|--------|----------|------|
| `/start` | - | 🏠 主菜单 |
| `/help` | - | ❓ 帮助 |
| `/b` | `/bind` | 🔑 绑定Emby |
| `/s` | `/status` | 📊 查看状态 |
| `/ss` | `/search` | 🔍 搜索歌曲 |
| `/al` | `/album` | 💿 下载专辑 |
| `/req` | `/request` | 📝 申请歌曲 |
| `/mr` | `/myrequests` | 📋 我的申请 |
| `/sub` | `/schedule` | 📅 订阅列表 |
| `/unsub` | `/unschedule` | ❌ 取消订阅 |
| `/scan` | `/rescan` | 🔄 扫描Emby |
| `/si` | `/scaninterval` | ⏱️ 扫描间隔 |

## 📖 使用方法

### 1. 绑定 Emby 账户
```
/bind 你的用户名 你的密码
```

### 2. 同步歌单
直接发送歌单链接：
- 网易云: `https://music.163.com/playlist?id=xxx`
- QQ音乐: `https://y.qq.com/n/ryqq/playlist/xxx`
- Spotify: `https://open.spotify.com/playlist/xxx`

### 3. 登录音乐平台（下载功能）
访问 Web 管理界面 → 设置 → 扫码登录或 Cookie 登录
- 支持网易云音乐、QQ音乐
- 推荐使用扫码登录（有效期更长）

### 4. 搜索下载
```
/search 晴天
/album 叶惠美
```

### 5. 上传音乐
直接发送音频文件到 Bot

## 🔧 高级配置

### 大文件上传 (Pyrogram)
默认 Telegram Bot API 文件上传限制为 20MB，配置 Pyrogram 可上传最大 2GB 文件。

1. 访问 https://my.telegram.org 创建应用
2. 获取 `API_ID` 和 `API_HASH`
3. 设置环境变量：
```bash
TG_API_ID=你的API_ID
TG_API_HASH=你的API_HASH
```

### Telegram Local Bot API Server
另一种支持大文件的方式，需要自建 API Server。

1. 部署 [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api)
2. 设置环境变量：
```bash
TELEGRAM_API_URL=http://你的api地址:8081/bot
```

### MusicTag 集成
在 Web 设置页面配置 MusicTag 监控目录，下载的音乐会自动移动到该目录进行刮削。

### 音质设置

**网易云音乐：**
- `standard` - 标准音质 (128kbps MP3)
- `higher` - 较高音质 (192kbps MP3)
- `exhigh` - 极高音质 (320kbps MP3) [默认]
- `lossless` - 无损 SQ (FLAC) [需VIP]
- `hires` - 高清臻音 Hi-Res [需VIP]
- `jyeffect` - 高清环绕声 [需VIP]
- `sky` - 沉浸环绕声 [需VIP]
- `jymaster` - 超清母带 Master [需SVIP]

**QQ音乐：**
- `standard` - 标准音质 (128kbps)
- `higher` - HQ高品质 (320kbps)
- `lossless` - 无损 SQ (FLAC) [需VIP]
- `hires` - 臻品母带 Hi-Res [需SVIP]
- `dolby` - 臻品全景声 Dolby [需SVIP]
- `master` - 臻品母带2.0 [需SVIP]

## 📁 目录结构

```
TGmusicbot/
├── bot/
│   ├── main.py           # Telegram Bot 主程序
│   ├── web.py            # Web 管理界面
│   ├── ncm_downloader.py # 音乐下载模块（网易云+QQ音乐）
│   ├── file_organizer.py # 文件自动整理模块
│   └── templates/        # HTML 模板
├── data/                 # 数据目录（数据库、缓存）
├── uploads/              # 音乐文件目录
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## 📝 更新日志

### v1.5.6
- 🎨 优化 QQ 音乐 Cookie 输入界面（预填模板，只需填值）

### v1.5.5
- ✨ 新增 QQ 音乐扫码登录（Cookie 有效期更长）
- ✨ 新增 Cookie 刷新功能（延长有效期最长 90 天）
- 🎨 更新关于页面功能列表和版本号

### v1.5.4
- ✨ 新增 Spotify 歌单同步支持
- ✨ 网易云下载失败自动尝试 QQ 音乐（跨平台下载）
- ✨ 新增更多音质选项（Hi-Res、Master、Dolby 等）
- ✨ 未匹配歌曲列表分页显示
- 🔧 更新 Cookie 获取说明（使用 Application 标签页）

### v1.5.3
- ✨ 新增 Emby Webhook 实时入库通知
- ✨ 新增歌单同步申请功能（用户申请，管理员审批）
- ✨ 新增下载历史记录

### v1.5.2
- ✨ 新增下载失败重试按钮（一键重试所有失败歌曲）
- ✨ 搜索下载记录自动保存到下载历史
- 🔧 修复音频预览在整理模式下不可用的问题

### v1.5.0
- ✨ 搜索结果缓存优化（3分钟缓存，避免重复请求）
- ✨ 完善定时任务系统
  - 修复歌单订阅 `is_active` 字段缺失
  - Cookie 过期检查优化（启动1分钟后首次检查）
- 🔧 修复下载统计图表高度问题

### v1.4.9
- 🔧 修复下载统计页面图表高度显示问题
- 📊 优化图表容器布局

### v1.4.7
- 🔧 修复 Pyrogram 超时问题
- 🔧 优化大文件上传稳定性

### v1.4.6
- 🔧 整合文件整理器到下载设置
- ✨ 选择"下载后自动整理"模式时自动启动整理器
- 🔧 简化设置页面，移除独立的整理器配置卡片

### v1.4.5
- ✨ 文件整理时自动提取封面保存为 `cover.jpg`
- 🔧 降低第三方库日志级别，避免刷屏
- 🔧 文件整理通知改为日志记录（不再刷 Telegram）
- 🔧 启动时发送一次性目录映射通知

### v1.4.4
- 🔧 修复搜索结果 Markdown 转义错误（歌名含特殊字符时报错）
- ✨ 文件整理完成后发送 Telegram 通知
- ✨ 机器人启动时自动启动文件整理器（如果已启用）

### v1.4.3
- 🔧 修复文件整理器"启用监控"开关保存后刷新丢失问题
- 🔧 完善 enabled 字段前后端同步逻辑
- 🔧 取消启用时自动停止监控

### v1.4.2
- 🔧 修复设置页面刷新后配置丢失问题
- 🔧 修复文件整理器表单 ID 不匹配问题
- 🔧 优化设置页面交互体验

### v1.4.0
- ✨ 新增文件自动整理功能（类似 MusicTag 监控模式）
- ✨ 支持目录模板配置（按艺术家/专辑自动分类）
- ✨ 增强元数据写入（专辑艺术家、年份）
- ✨ Web 设置页面新增文件整理配置
- 📦 新增 watchdog 依赖支持目录监控

### v1.3.0
- ✨ Web 界面支持亮色/暗色主题切换
- ✨ 新增下载统计页面（图表、Cookie 状态）
- ✨ 每日统计报告自动推送（每天 9:00）
- ✨ Cookie 过期自动告警通知
- ✨ 支持 Inline 模式搜索（任意聊天 @bot 歌名）
- ✨ 搜索结果缓存优化
- 📱 移动端界面适配

### v1.2.0
- ✨ 新增下载管理器（队列管理、并发控制、自动重试）
- ✨ 新增 `/ds` 查看下载状态和统计
- ✨ 新增 `/dq` 查看下载队列
- ✨ 新增 `/dh` 查看下载历史
- ✨ Cookie 状态监控和过期预警
- 🔧 代码架构优化

### v1.4.6
- 🔧 整合文件整理器到下载设置
- ✨ 选择"下载后自动整理"模式时自动启动整理器
- 🔧 简化设置页面，移除独立的整理器配置卡片

### v1.4.5
- ✨ 文件整理器自动提取专辑封面（cover.jpg）
- 🔧 修复搜索命令 Markdown 解析错误
- 🔧 减少第三方库日志输出
- ✨ 新增 QQ 音乐搜索下载 (`/qs`, `/qa`)
- ✨ 新增歌曲元数据嵌入（封面、歌词、标签）
- ✨ 下载进度条美化显示

### v1.0.9
- ✨ 新增 Emby 自动扫描功能（可配置间隔）
- ✨ 新增 `/scaninterval` 命令配置扫描间隔
- ✨ Web 设置页面支持配置扫描间隔

### v1.0.8
- ✨ 新增 Web 管理界面登录保护
- ✨ 新增用户权限管理（上传/申请权限）
- ✨ 新增 `/request` 歌曲补全申请功能
- ✨ 新增 `/myrequests` 查看申请状态
- ✨ 管理员 Telegram 审核推送
- ✨ 支持 Pyrogram 大文件上传（最大 2GB）
- ✨ 音频格式白名单限制

### v1.5.8
- ✨ 优化设置页面布局：网易云/QQ音乐账号并排显示
- ✨ 新增 QQ 音乐独立音质设置
- ✨ 音质设置并排显示，下载目录配置共用
- 🔧 简化设置界面，删除重复的配置区块

### v1.5.7
- ✨ 新增网易云 Cookie 刷新功能
- ✨ 新增 Web 端歌单订阅管理页面
- ✨ 订阅管理支持启用/禁用和删除
- 🔧 完善定时同步任务（每6小时检查歌单更新）

### v1.5.6
- ✨ 新增 QQ 音乐扫码登录
- ✨ 新增 QQ 音乐 Cookie 刷新功能
- ✨ 优化 Cookie 输入界面（预填 uin= 和 qm_keyst= 前缀）
- 🔧 修复版本号显示

### v1.0.7
- ✨ 新增 Telegram Local Bot API Server 支持（上传大文件）
- 🔧 修复 Cookie 读取问题
- 🔧 修复异步事件循环问题

### v1.0.6
- ✨ 新增 `/search` 搜索下载单曲
- ✨ 新增 `/album` 搜索下载专辑
- ✨ 新增定时同步歌单（每6小时检查更新）
- ✨ 下载进度实时显示
- ✨ 下载完成自动触发 Emby 扫库
- ✨ Bot 启动自动注册命令菜单
- 🔧 优化网易云 Cookie 登录

### v1.0.5
- ✨ 网易云自动下载缺失歌曲
- ✨ MusicTag 集成支持
- ✨ 音质选择配置

### v1.0.0
- ✨ Web 管理界面
- ✨ 网易云登录支持

## 📄 License

MIT
