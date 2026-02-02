# 音乐下载代理服务

部署在**国内服务器**，为海外 VPS 提供 QQ音乐/网易云 下载代理。

## 快速部署

### 1. 上传到国内服务器

```bash
scp -r proxy-server user@国内服务器IP:/home/user/
```

### 2. 配置环境变量

```bash
cd proxy-server
cp .env.example .env
nano .env
```

填写：
- `PROXY_API_KEY`: 自定义安全密钥（海外 VPS 也要配置同样的）

> **注意**：Cookie 不需要在代理服务器配置，由主 Bot 通过请求头传递。

### 3. 启动服务

```bash
docker compose up -d
```

### 4. 测试

```bash
curl http://localhost:8899/health
```

## 海外 VPS 配置

在海外 VPS 的 TGmusicbot `.env` 文件中添加：

```env
MUSIC_PROXY_URL=http://国内服务器IP:8899
MUSIC_PROXY_KEY=你设置的PROXY_API_KEY
```

Cookie 仍然在主 Bot 配置（不变），代理服务器会从请求头获取。

## API 接口

| 接口 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `GET /qq/diagnose` | 诊断 QQ 音乐 Cookie 和账号状态 |
| `GET /qq/url/<mid>?quality=exhigh` | 获取 QQ 音乐下载链接 |
| `GET /qq/download/<mid>?quality=exhigh` | 下载 QQ 音乐（返回文件流） |
| `GET /ncm/url/<id>?quality=exhigh` | 获取网易云下载链接 |
| `GET /ncm/download/<id>?quality=exhigh` | 下载网易云音乐 |
| `GET /search/qq?keyword=xxx` | 搜索 QQ 音乐 |
| `GET /search/ncm?keyword=xxx` | 搜索网易云 |

所有接口需要携带：
- API Key: `X-API-Key: your_key` (Header 或 URL 参数 `?key=xxx`)
- Cookie: `X-QQ-Cookie` 或 `X-NCM-Cookie` (Header，由主 Bot 自动传递)

## 音质选项

- `standard`: 128kbps MP3
- `higher`: 192kbps MP3
- `exhigh`: 320kbps MP3（默认）
- `lossless`: FLAC 无损
- `hires`: Hi-Res（如果有）
