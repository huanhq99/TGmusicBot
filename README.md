# 🎵 TGmusicbot

一站式 Telegram 音乐助手：同步网易云 / QQ音乐 / Spotify 歌单到 Emby，自动补全缺失歌曲，并提供 Web 管理与实时 Webhook 通知。

## ✨ 功能亮点
- **歌单同步**：多平台歌单导入，自动匹配 Emby 库并生成播放列表。
- **跨平台下载**：网易云 + QQ 音乐双引擎，失败自动切换，支持多种音质与元数据写入。
- **实时通知**：Emby Webhook 直接推送 Telegram，Web 面板提供测试按钮排障。
- **上传与整理**：聊天中上传音频自动落盘，可通过文件整理器按艺术家/专辑归档。
- **自动化任务**：歌单订阅、定时扫描、Cookie 预警、下载重试、每日统计等。
- **可视化管理**：Web UI 涵盖扫码登录、配置、下载历史、Webhook 状态、整理器等。

---

## 🚀 Docker 快速部署
**创建 `docker-compose.yml`**

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
      # QQ音乐国内中转URi KEy
      - MUSIC_PROXY_URL=
      - MUSIC_PROXY_KEY=
    
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

> **如果你是国外机器，则需要部署music-proxy-compose.yaml把部署好的RUL KEY填在变量中即可**

3. **启动与访问**
	 ```bash
	 docker compose up -d
	 # Web 管理界面: http://<服务器IP>:8080
	 ```
---

## ⚙️ 环境变量速查
| 变量 | 说明 | 是否必填 |
|------|------|----------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（1.7.8 推荐） | ✅ |
| `TELEGRAM_TOKEN` | 旧名，若保留将作为兼容备用 | 可选 |
| `ADMIN_USER_ID` | 接收系统 / Webhook 推送的 Telegram ID | ✅ |
| `EMBY_URL` / `EMBY_USERNAME` / `EMBY_PASSWORD` | Emby 服务地址与凭据 | ✅ |
| `PLAYLIST_BOT_KEY` | 加密存储用的随机字符串 | ✅ |
| `WEB_USERNAME` / `WEB_PASSWORD` | Web 管理界面登录信息 | 推荐 |
| `MUSIC_PROXY_URL` / `MUSIC_PROXY_KEY` | 海外主机使用国内代理下载时配置 | 可选 |
| `TG_API_ID` / `TG_API_HASH` | 启用 Pyrogram 大文件上传 | 可选 |
| `TELEGRAM_API_URL` | 自建 Telegram Bot API Server 地址 | 可选 |
| `EMBY_WEBHOOK_NOTIFY` | 是否启用 Webhook Telegram 推送 (默认 true) | 可选 |

> 更多变量请参考 `docker-compose.yml` 与代码注释。

> 关于音乐中转：机器人部署在海外时，请在国内单独部署代理服务（Clash、sing-box 或自建 API 均可），然后把该机器的公网地址填入 `MUSIC_PROXY_URL`。无需把代理容器和 bot 放在同一 compose 文件里。

---

## 📱 Bot 命令速览
| 命令 | 说明 |
|------|------|
| `/start` `/help` | 主菜单 / 帮助 |
| `/bind` | 绑定 Emby 账号 |
| `/status` | 查看当前配置、Cookie、订阅等 |
| `/search` `/album` | 搜索并下载歌曲 / 专辑 |
| `/request` `/myrequests` | 歌曲补全申请与查询 |
| `/schedule` `/unschedule` | 管理歌单订阅 |
| `/rescan` `/scaninterval` | 触发 / 设置 Emby 扫描 |

---

## 🧭 常用流程
1. **绑定 Emby**：`/bind 用户名 密码`。
2. **登录音乐平台**：Web → 设置 → 网易云/QQ 扫码或 Cookie 登录（推荐扫码）。
3. **同步歌单**：在 Telegram 里直接发送歌单链接；Web 端可管理订阅与自动下载策略。
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
- **v1.7.8**
	- Webhook 改为直接调用 Telegram HTTP API 发送消息，不再依赖 Bot 实例共享。
	- 新增 `TELEGRAM_BOT_TOKEN` 环境变量（保留旧变量兼容）。
	- Web “测试通知” 按钮会真实推送 Telegram 以便排查。
	- 多项 QQ/网易云下载、元数据、Webhook 队列相关修复。

> 更早的版本记录请查看 GitHub Releases。

👉 查看完整更新轨迹：`CHANGELOG.md`

---
##  💰 赞助
- **如果你觉得该项目能帮到你，且有条件的情况下可以请我喝杯咖啡**
![赞助](https://img.huanhq.com/1765174910475_e0bda6f3bae25cceadb246e71a814aea.jpg)
## 📄 License
MIT License

