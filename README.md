# 🎶 TGmusicbot - 你的全能 Emby 音乐管家

<p align="center">
  <img src="https://img.shields.io/github/stars/huanhq99/TGmusicbot?style=flat-square&logo=github" alt="Stars">
  <img src="https://img.shields.io/github/forks/huanhq99/TGmusicbot?style=flat-square&logo=github" alt="Forks">
  <img src="https://img.shields.io/github/license/huanhq99/TGmusicbot?style=flat-square&logo=mit" alt="License">
  <img src="https://img.shields.io/docker/pulls/huanhq99/tgmusicbot?style=flat-square&logo=docker" alt="Docker Pulls">
  <a href="https://t.me/EmbyCockpit" target="_blank">
    <img src="https://img.shields.io/badge/Telegram-加入交流群-0088cc?style=flat-square&logo=telegram" alt="Telegram Group">
  </a>
</p>

一站式 Telegram 音乐助手：**同步网易云 / QQ 音乐 / Spotify 歌单到 Emby**，自动补全缺失歌曲，并提供 Web 管理面板与实时 Webhook 通知。

> [!TIP]
> **觉得项目好用？给个 Star ⭐️ 是对我最大的支持！**

---

## ✨ 功能亮点

- 🔗 **歌单秒同步**：直接发送网易云/QQ/Spotify 歌单链接，机器人自动识别并同步到 Emby。
- 📥 **跨平台下载**：网易云 + QQ 音乐双引擎，支持无损、Hi-Res、Master 音质，自动补全元数据与封面。
- 🤖 **自动化任务**：订阅喜欢的歌单，定时扫描更新，新歌自动落盘并同步至 Emby 播放列表。
- 📦 **智能整理**：内置文件整理器，按 艺术家/专辑 自动分类归档，让库不再凌乱。
- 📱 **实时推送**：Emby 新入库歌曲通过 Telegram 实时通知，状态一目了然。
- 🖥️ **可视化面板**：精致的 Web UI，支持扫码登录、下载记录查看、系统配置管理。

---

## 👥 交流与支持

遇到问题或有功能建议？欢迎加入我们的 [Telegram 交流群](https://t.me/EmbyCockpit)。

---

## 🚀 Docker 快速部署

**1. 创建 `docker-compose.yml`**

```yaml
services:
  tgmusicbot:
    image: huanhq99/tgmusicbot:latest
    container_name: tgmusicbot
    restart: unless-stopped
    ports:
      - "8080:8080"  # Web 管理界面
    volumes:
      - ./data:/app/data              # 数据库、缓存、日志
      - ./uploads:/app/uploads        # 下载的音乐文件
      - /path/to/music:/music         # 整理目标目录（可选）
    environment:
      - TZ=Asia/Shanghai
      - DATA_DIR=/app/data
      - UPLOAD_DIR=/tmp/tgmusicbot_uploads
      - MUSIC_TARGET_DIR=/app/uploads
      # Telegram 配置（Bot Token 和管理员 ID）
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - ADMIN_USER_ID=${ADMIN_USER_ID}
      # Telegram API 大文件上传支持（可选，可上传超过 20MB 的文件）
      - TG_API_ID=${TG_API_ID:-}
      - TG_API_HASH=${TG_API_HASH:-}
      # Web 管理界面用户名密码
      - WEB_USERNAME=${WEB_USERNAME:-admin}
      - WEB_PASSWORD=${WEB_PASSWORD}
      # Emby 配置
      - EMBY_URL=${EMBY_URL}
      - EMBY_USERNAME=${EMBY_USERNAME}
      - EMBY_PASSWORD=${EMBY_PASSWORD}
      # Emby 自动扫描间隔（小时，0=禁用）
      - EMBY_SCAN_INTERVAL=${EMBY_SCAN_INTERVAL:-6}
      # 加密密钥（自定义）
      - PLAYLIST_BOT_KEY=${PLAYLIST_BOT_KEY}
      # QQ音乐国内中转 URL & KEY
      - MUSIC_PROXY_URL=${MUSIC_PROXY_URL:-}
      - MUSIC_PROXY_KEY=${MUSIC_PROXY_KEY:-}
      # 国内机器请配置代理环境
      - HTTP_PROXY=
      - HTTPS_PROXY=
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

> **如果您的主机器人部署在国外服务器，请在一台国内机器上部署此中转代理服务，否则可能无法下载高音质的 QQ 音乐和网易云。**

## 🚀 国内中转代理部署 (Music Proxy)
在您的**国内服务器**上创建一个 `docker-compose.yml` 文件：

```yaml
version: '3.8'

services:
  music-proxy:
    # 直接使用 GitHub 的源码目录进行构建
    build: https://github.com/huanhq99/TGmusicBot.git#:proxy-server
    container_name: music-proxy
    restart: unless-stopped
    ports:
      - "8899:8899"  # <服务器外部映射端口>:<容器内部端口>
    environment:
      # 【必填】中转安全密钥。请在此设置一个复杂的随机字符串！
      # 必须与主机器人环境变量配置的 `MUSIC_PROXY_KEY` 完全一致。
      - PROXY_API_KEY=your_secure_api_key_here
      
      # 【可选】内部监听端口，如果不填默认是 8899，必须和 ports 内部一致
      - PORT=8899
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

**2. 启动服务**

```bash
docker compose up -d
```

**3. 配置主体机器人验证代理**
代理服务启动后，返回您的**主 TGmusicbot 服务**，在其 `docker-compose.yml` 中配置这两个变量：
- `MUSIC_PROXY_URL=http://<国内机器的公网IP>:8899` （必须带上 http:// 和真实端口）
- `MUSIC_PROXY_KEY=your_secure_api_key_here` （必须和上面在国内机器设置的密钥一致）
重启主机器人后即可生效。

---

## ⚙️ 环境变量详细说明

| 变量 | 说明 | 是否必填 |
|------|------|:---:|
| `TELEGRAM_BOT_TOKEN` | 从 @BotFather 获取的机器令牌 | ✅ |
| `ADMIN_USER_ID` | 接收系统通知的 Telegram 用户 ID | ✅ |
| `WEB_PASSWORD` | Web 管理界面的登录密码 | ✅ |
| `EMBY_URL` | Emby 服务的访问地址 (如 `http://192.168.1.100:8096`) | ✅ |
| `EMBY_USERNAME` | Emby 管理员或具有库编辑权限的用户名 | ✅ |
| `EMBY_PASSWORD` | 对应的 Emby 登录密码 | ✅ |
| `PLAYLIST_BOT_KEY` | 用于数据库加密存储的随机字符串（建议 16 位以上） | ✅ |
| `WEB_USERNAME` | Web 用户名 (默认 `admin`) | 可选 |
| `EMBY_SCAN_INTERVAL` | 自动扫描 Emby 库的时间间隔 (单位：小时，0 为禁用) | 可选 |
| `MUSIC_PROXY_URL` | 国内中转代理地址 (海外 VPS 访问国内音乐接口用) | 可选 |
| `MUSIC_PROXY_KEY` | 国内中转代理对应的访问 Key | 可选 |
| `TG_API_ID` / `TG_API_HASH` | 开启 Pyrogram 大文件上传支持 (需在 my.telegram.org 申请) | 可选 |
| `TZ` | 容器时区 (默认 `Asia/Shanghai`) | 可选 |

---

## 🧭 常用流程
1. **绑定 Emby**：`/bind 用户名 密码`。
2. **登录音乐平台**：Web → 设置 → 网易云/QQ 扫码或 Cookie 登录（推荐扫码）。
3. **同步歌单**：在 Telegram 里直接发送歌单链接，Bot 自动识别并弹出 [立即下载] / [订阅同步] 按钮；Web 端可管理订阅与自动下载策略。
4. **Webhook 通知**：Emby Webhooks 插件中填写 `http(s)://<服务器>:8080/webhook/emby`，勾选 `ItemAdded / library.new`；在 Web → 设置 中“发送测试通知”即可验证。
5. **文件整理**：Web → 文件整理器，配置监控目录、命名模板、冲突策略等。

---

## 🛰️ Emby Webhook 配置示例
1. Emby → Dashboard → Webhooks → Add → HTTP。
2. 填写：
	 - URL: `https://example.com/webhook/emby`
	 - Method: `POST`
	 - Body:
		 ```json
		 {
			 "itemName": "{{Name}}",
			 "event": "{{Event}}",
			 "mbId": "{{ItemId}}",
			 "mbUser": "{{UserName}}"
		 }
		 ```
	 - Events: 勾选 `ItemAdded`、`library.new`。
3. 在 TGmusicbot Web → 设置 → Webhook 中点击“发送测试通知”确认可达性。

> 如果代理/反代层开启了额外认证，记得同步更新 `WEBHOOK_SECRET` 或反代白名单。

---

## 🔧 进阶特性
- **大文件上传**：配置 `TG_API_ID` / `TG_API_HASH` 启用 Pyrogram，支持 2GB 文件。
- **代理下载**：`MUSIC_PROXY_URL` + `MUSIC_PROXY_KEY` 让海外 VPS 通过国内代理访问 QQ/网易云。
- **音质与元数据**：Web 中分别设置网易云 / QQ 音质（standard / higher / exhigh / lossless / hires / master 等），下载完成自动写入封面、歌词、标签。
- **安全建议**：可在反代层对 `/webhook/emby` 添加 Basic Auth 或 IP 白名单。

---

## 📁 目录结构
```
TGmusicbot/
├── bot/
│   ├── main.py            # Telegram Bot 主程序
│   ├── web.py             # FastAPI Web 管理界面
│   ├── ncm_downloader.py  # 网易云 & QQ 下载器
│   ├── file_organizer.py  # 文件自动整理器
│   └── templates/         # Web UI 模板
├── data/                  # 运行期数据库、缓存
├── uploads/               # 下载完成的音频
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---


## 🆕 最近更新
- **v1.10.1**
	- 🌟 **歌单链接自动识别**：直接发送网易云/QQ音乐歌单链接（支持短链接 163cn.tv），Bot 自动弹出 [立即下载] / [订阅同步] 按钮。
	- 🌟 **智能同步**：先同步到 Emby，再下载缺失歌曲，不会重复下载。
	- 🌟 **同步间隔继承**：订阅歌单可跟随全局同步间隔设置。
	- 修复多项类型安全、配置读取、回调超时等问题。

- **v1.10.0**
	- 文件整理器优化与 bug 修复。
	- 下载记录持久化改进。

- **v1.9.x**
	- Webhook 改为直接调用 Telegram HTTP API 发送消息。
	- Web "测试通知" 按钮真实推送 Telegram。
	- 多项 QQ/网易云下载、元数据修复。

> 更早的版本记录请查看 GitHub Releases。

👉 查看完整更新轨迹：`CHANGELOG.md`

---
##  💰 赞助
- **如果你觉得该项目能帮到你，且有条件的情况下可以请我喝杯咖啡**
<img src="https://img.huanhq.com/1765174910475_e0bda6f3bae25cceadb246e71a814aea.jpg" width="200" alt="赞助">

## 📄 开源协议
MIT License
