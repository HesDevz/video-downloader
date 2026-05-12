import html as html_lib
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_SAVE_DIR = Path.home() / "Desktop" / "下载"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 aweme"
)

# LibreSSL 2.8.3 TLS workaround
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class DownloadError(Exception):
    pass


# ── Platform detection ──────────────────────────────────────────────────────

PLATFORM_RULES = [
    # (pattern, platform_name)
    (r"(?:v\.douyin\.com|iesdouyin\.com|douyin\.com/video)", "douyin"),
    (r"(?:xiaohongshu\.com|xhslink\.com)", "xiaohongshu"),
    (r"(?:bilibili\.com|b23\.tv)", "bilibili"),
    (r"(?:youtube\.com|youtu\.be)", "youtube"),
    (r"(?:kuaishou\.com|gifshow\.com|v\.kuaishou\.com)", "kuaishou"),
    (r"(?:weixin\.qq\.com/s/|channels\.weixin\.qq\.com)", "shipinhao"),
]


def detect_platform(url):
    for pattern, name in PLATFORM_RULES:
        if re.search(pattern, url):
            return name
    return None


# ── Shared helpers ──────────────────────────────────────────────────────────

def fetch(url, *, method="GET", referer=None, timeout=25):
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CTX) as response:
            return response.geturl(), response.headers, response.read()
    except urllib.error.HTTPError as exc:
        if method == "HEAD" and exc.headers.get("Location"):
            return exc.headers["Location"], exc.headers, b""
        raise DownloadError(f"请求失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"网络请求失败：{exc.reason}") from exc


def extract_url(text):
    match = re.search(r"https?://[^\s，。]+", text or "")
    if not match:
        raise DownloadError("没有找到有效链接")
    return match.group(0)


def safe_filename(title, aweme_id, max_len=80):
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", title, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:max_len] or "video"
    return f"{cleaned}_{aweme_id}.mp4"


def find_yt_dlp():
    return shutil.which("yt-dlp")


# ── Douyin ──────────────────────────────────────────────────────────────────

def _douyin_resolve(url):
    final_url, _, _ = fetch(url, method="HEAD")
    return final_url


def _douyin_extract_id(url):
    for p in [r"/video/(\d+)", r"aweme_id=(\d+)", r"item_ids=(\d+)"]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise DownloadError("没有从链接里识别到视频 ID")


def _douyin_extract_play_url(html):
    unescaped = html_lib.unescape(html)
    for c in re.findall(r'https:\\u002F\\u002F[^"\\]+(?:\\u002F[^"\\]*)*', unescaped):
        u = c.replace("\\u002F", "/")
        if "playwm" in u or "/play/" in u:
            return u
    m = re.search(r'"play_addr"\s*:\s*\{.*?"url_list"\s*:\s*(\[.*?\])', unescaped, re.S)
    if m:
        for u in json.loads(m.group(1)):
            if "playwm" in u or "/play/" in u:
                return u
    raise DownloadError("没有在页面里找到视频播放地址")


def _douyin_extract_title(html):
    text = html_lib.unescape(html)
    for p in [r'"desc"\s*:\s*"([^"]+)"', r'<title[^>]*>(.*?)</title>']:
        m = re.search(p, text, re.S)
        if m:
            v = re.sub(r"\s+", " ", m.group(1).strip()).replace("\\n", " ")
            if v:
                return v
    return "douyin_video"


def download_douyin(source_url, save_dir):
    final_url = _douyin_resolve(source_url)
    aweme_id = _douyin_extract_id(final_url)
    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    _, _, page_bytes = fetch(share_url, referer=source_url)
    page_html = page_bytes.decode("utf-8", errors="ignore")
    play_url = _douyin_extract_play_url(page_html)
    title = _douyin_extract_title(page_html)
    filename = safe_filename(title, aweme_id)

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")

    _, headers, video_bytes = fetch(play_url, referer=share_url)
    content_type = headers.get("Content-Type", "")
    if "video" not in content_type and not video_bytes.startswith(b"\x00\x00"):
        raise DownloadError("下载结果不像视频文件，可能需要登录或链接已失效")
    target.write_bytes(video_bytes)

    return {"ok": True, "path": str(target), "filename": target.name,
            "bytes": target.stat().st_size, "title": title, "platform": "抖音"}


# ── yt-dlp 通用下载器 (YouTube / B站 / 快手) ─────────────────────────────

def download_ytdlp(url, save_dir, platform_name):
    ytdlp = find_yt_dlp()
    if not ytdlp:
        raise DownloadError("yt-dlp 未安装，请运行: brew install yt-dlp")

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / "%(title).80s_%(id)s.%(ext)s")

    cmd = [
        ytdlp,
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        "--print", "after_move:filepath",
        url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise DownloadError("下载超时（5分钟）")

    if proc.returncode != 0:
        err = proc.stderr.strip().split("\n")[-1] if proc.stderr else "未知错误"
        raise DownloadError(f"下载失败：{err}")

    # yt-dlp prints the final file path
    filepath = proc.stdout.strip().split("\n")[-1]
    if not filepath or not os.path.exists(filepath):
        raise DownloadError("下载完成但找不到文件")

    p = Path(filepath)
    return {"ok": True, "path": str(p), "filename": p.name,
            "bytes": p.stat().st_size, "title": p.stem.rsplit("_", 1)[0],
            "platform": platform_name}


# ── 小红书 ──────────────────────────────────────────────────────────────────

def download_xiaohongshu(source_url, save_dir):
    """小红书视频下载：通过分享页提取视频地址"""
    # Resolve short link
    final_url, _, page_bytes = fetch(source_url, referer="https://www.xiaohongshu.com/")
    page_html = page_bytes.decode("utf-8", errors="ignore")

    # Extract video URL from page
    play_url = None
    title = "xiaohongshu_video"

    # Pattern 1: video URL in __INITIAL_STATE__ or SSR data
    m = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', page_html)
    if m:
        key = m.group(1)
        if key.startswith("http"):
            play_url = key
        else:
            play_url = f"https://sns-video-bd.xhscdn.com/{key}"

    if not play_url:
        m = re.search(r'"url"\s*:\s*"(https?://[^"]*(?:\.mp4|video)[^"]*)"', page_html)
        if m:
            play_url = m.group(1)

    if not play_url:
        # Try SSR embedded data
        m = re.search(r'<script>window\.__INITIAL_STATE__\s*=\s*({.+?})</script>', page_html, re.S)
        if m:
            try:
                data = json.loads(m.group(1).replace("undefined", "null"))
                # Walk to find video
                play_url, title = _xhs_find_video_in_data(data)
            except (json.JSONDecodeError, Exception):
                pass

    if not play_url:
        raise DownloadError(
            "没有在小红书页面找到视频地址。\n"
            "可能原因：链接是图文笔记（不是视频）、链接已失效、或需要登录。"
        )

    if not title or title == "xiaohongshu_video":
        m = re.search(r'"title"\s*:\s*"([^"]{2,})"', page_html)
        if m:
            title = m.group(1)

    # Download
    aweme_id = re.search(r"/explore/(\w+)", final_url) or re.search(r"/(\w+)$", final_url)
    vid = aweme_id.group(1) if aweme_id else str(int(time.time()))
    filename = safe_filename(title, vid)

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")

    _, headers, video_bytes = fetch(play_url, referer="https://www.xiaohongshu.com/")
    content_type = headers.get("Content-Type", "")
    if "video" not in content_type and len(video_bytes) < 10000:
        raise DownloadError("下载结果不像视频文件")
    target.write_bytes(video_bytes)

    return {"ok": True, "path": str(target), "filename": target.name,
            "bytes": target.stat().st_size, "title": title, "platform": "小红书"}


def _xhs_find_video_in_data(data, depth=0):
    if depth > 12:
        return None, None
    if isinstance(data, dict):
        if "originVideoKey" in data:
            key = data["originVideoKey"]
            url = key if key.startswith("http") else f"https://sns-video-bd.xhscdn.com/{key}"
            title = data.get("title", "") or data.get("desc", "") or "xiaohongshu_video"
            return url, title
        if "url" in data and isinstance(data["url"], str) and ".mp4" in data["url"]:
            return data["url"], data.get("title", "xiaohongshu_video")
        for v in data.values():
            r = _xhs_find_video_in_data(v, depth + 1)
            if r[0]:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _xhs_find_video_in_data(item, depth + 1)
            if r[0]:
                return r
    return None, None


# ── 视频号 ──────────────────────────────────────────────────────────────────

def download_shipinhao(source_url, save_dir):
    raise DownloadError(
        "视频号暂时不支持下载。\n"
        "视频号的视频需要微信登录态才能获取，技术上比较难实现。\n"
        "建议用手机端的录屏功能或者第三方微信视频号下载工具。"
    )


# ── Main entry ──────────────────────────────────────────────────────────────

PLATFORM_MAP = {
    "douyin":      ("抖音",   download_douyin),
    "xiaohongshu": ("小红书", download_xiaohongshu),
    "bilibili":    ("B站",    lambda url, d: download_ytdlp(url, d, "B站")),
    "youtube":     ("YouTube", lambda url, d: download_ytdlp(url, d, "YouTube")),
    "kuaishou":    ("快手",   lambda url, d: download_ytdlp(url, d, "快手")),
    "shipinhao":   ("视频号", download_shipinhao),
}

SUPPORTED_PLATFORMS = ", ".join(v[0] for v in PLATFORM_MAP.values())


def download_video(text, save_dir=DEFAULT_SAVE_DIR):
    source_url = extract_url(text)
    platform = detect_platform(source_url)

    if not platform:
        raise DownloadError(
            f"暂不支持该链接。\n"
            f"目前支持：{SUPPORTED_PLATFORMS}"
        )

    name, func = PLATFORM_MAP[platform]
    return func(source_url, save_dir)
