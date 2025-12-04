# 🎵 TGmusicbot

Telegram 音乐管理机器人 - 同步歌单到 Emby + 自动下载缺失歌曲

## ✨ 功能特性

### 🎵 歌单同步
- 支持 **QQ音乐** 和 **网易云音乐** 歌单
- 自动匹配 Emby 媒体库歌曲
- 支持模糊匹配和完全匹配模式
- 自动创建 Emby 歌单

### 📥 自动下载
- 自动下载 Emby 中缺失的歌曲
- 支持网易云 VIP 音质（需登录）
- 音质可选：标准/高品质/无损
- 下载完成自动触发 Emby 扫库

### 🔍 搜索下载
- `/search <关键词>` - 搜索并下载单曲
- `/album <专辑名>` - 搜索并下载整张专辑

### 📅 定时同步
- 自动订阅已同步的歌单
- 每 6 小时检查歌单更新
- 发现新歌曲自动通知
- 一键下载新增歌曲

### 🔄 Emby 自动扫描
- 可配置定时自动扫描 Emby 媒体库
- 支持 Telegram 命令配置扫描间隔
- Web 管理界面可视化配置
- 确保 Emby 库与本地文件同步

### 📤 音乐上传
- 通过 Telegram 发送音频文件
- 自动保存到服务器
- 支持 MP3, FLAC, M4A, WAV 等格式
- 支持大文件上传（需配置 Pyrogram）

### 📝 歌曲补全申请
- 用户可申请下载缺失的歌曲
- 管理员 Telegram/Web 审核
- 审核结果自动通知用户

### 🔐 权限管理
- Web 管理界面登录保护
- 用户上传/申请权限控制

### 🖥️ Web 管理界面
- 仪表盘总览
- 网易云扫码/Cookie 登录
- 下载音质设置
- MusicTag 集成配置

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
version: '3.8'
services:
  tgmusicbot:
    image: huanhq99/tgmusicbot:latest
    container_name: tgmusicbot
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./uploads:/app/uploads
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
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

### 3. 登录网易云（下载功能）
访问 Web 管理界面 → 设置 → 使用 Cookie 登录

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
- `standard` - 标准音质 (128kbps)
- `higher` - 较高音质 (192kbps)  
- `exhigh` - 极高音质 (320kbps) [默认]
- `lossless` - 无损音质 (FLAC) [需VIP]

## 📁 目录结构

```
TGmusicbot/
├── bot/
│   ├── main.py           # Telegram Bot 主程序
│   ├── web.py            # Web 管理界面
│   ├── ncm_downloader.py # 网易云下载模块
│   └── templates/        # HTML 模板
├── data/                 # 数据目录（数据库、缓存）
├── uploads/              # 音乐文件目录
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## 📝 更新日志

### v2.5.0
- ✨ 新增 Emby 自动扫描功能（可配置间隔）
- ✨ 新增 `/scaninterval` 命令配置扫描间隔
- ✨ Web 设置页面支持配置扫描间隔

### v2.4.0
- ✨ 新增 Web 管理界面登录保护
- ✨ 新增用户权限管理（上传/申请权限）
- ✨ 新增 `/request` 歌曲补全申请功能
- ✨ 新增 `/myrequests` 查看申请状态
- ✨ 管理员 Telegram 审核推送
- ✨ 支持 Pyrogram 大文件上传（最大 2GB）
- ✨ 音频格式白名单限制

### v2.3.0
- ✨ 新增 Telegram Local Bot API Server 支持（上传大文件）
- 🔧 修复 Cookie 读取问题
- 🔧 修复异步事件循环问题

### v2.2.0
- ✨ 新增 `/search` 搜索下载单曲
- ✨ 新增 `/album` 搜索下载专辑
- ✨ 新增定时同步歌单（每6小时检查更新）
- ✨ 下载进度实时显示
- ✨ 下载完成自动触发 Emby 扫库
- ✨ Bot 启动自动注册命令菜单
- 🔧 优化网易云 Cookie 登录

### v2.1.0
- ✨ 网易云自动下载缺失歌曲
- ✨ MusicTag 集成支持
- ✨ 音质选择配置

### v2.0.0
- ✨ Web 管理界面
- ✨ 网易云登录支持

## 📄 License

MIT
