# 视频下载器

多平台视频下载工具，本地运行，无需登录账号。

## 支持平台

| 平台 | 状态 | 方式 |
|------|------|------|
| 抖音 | ✅ | iesdouyin 公开分享页 |
| 小红书 | ✅ | 分享页 SSR 数据提取 |
| B站 | ✅ | yt-dlp |
| YouTube | ✅ | yt-dlp |
| 快手 | ✅ | yt-dlp |
| 视频号 | ❌ | 需要微信登录态，暂不支持 |

## 安装

```bash
# 克隆仓库
git clone https://github.com/HesDevz/video-downloader.git
cd video-downloader

# 安装 yt-dlp（B站/YouTube/快手需要）
brew install yt-dlp

# 启动服务
python3 -m douyin_downloader.server
```

启动后浏览器打开 http://localhost:8787

## 使用

1. 在任意平台复制视频分享链接
2. 粘贴到输入框
3. 点击下载

视频保存到 `~/Desktop/下载/`

## 工作原理

- **抖音**：通过 `iesdouyin.com` 公开分享页提取视频地址，直接从 CDN 下载
- **小红书**：从分享页的服务端渲染数据中提取视频地址
- **B站 / YouTube / 快手**：使用 yt-dlp 提取公开页面的视频流

所有方式均不需要登录账号，不使用任何 Cookies，请求行为与普通浏览器访问一致。

## 局域网访问

启动后同一 WiFi 下的其他设备（如手机）可以通过 Mac 的局域网 IP 访问：

```bash
# 查看本机 IP
ipconfig getifaddr en0

# 手机浏览器访问
http://<你的IP>:8787
```

## 依赖

- Python 3.9+
- yt-dlp（可选，B站/YouTube/快手需要）

## 许可

MIT
