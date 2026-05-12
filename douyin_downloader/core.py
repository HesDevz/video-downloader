import html as html_lib
import json
import re
import ssl
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


class DownloadError(Exception):
    pass


# LibreSSL 2.8.3 on macOS 3.9.6 has TLS issues with some CDNs.
# Create a permissive SSL context to work around EOF errors.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


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
        raise DownloadError("没有找到抖音链接")
    return match.group(0)


def resolve_url(url):
    final_url, _, _ = fetch(url, method="HEAD")
    return final_url


def extract_aweme_id(url):
    patterns = [
        r"/video/(\d+)",
        r"aweme_id=(\d+)",
        r"item_ids=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise DownloadError("没有从链接里识别到视频 ID")


def extract_play_url(page_html):
    unescaped = html_lib.unescape(page_html)
    candidates = re.findall(r"https:\\u002F\\u002F[^\"\\]+(?:\\u002F[^\"\\]*)*", unescaped)
    for candidate in candidates:
        url = candidate.replace("\\u002F", "/")
        if "playwm" in url or "/play/" in url:
            return url

    match = re.search(r'"play_addr"\s*:\s*\{.*?"url_list"\s*:\s*(\[.*?\])', unescaped, re.S)
    if match:
        for url in json.loads(match.group(1)):
            if "playwm" in url or "/play/" in url:
                return url

    raise DownloadError("没有在页面里找到视频播放地址")


def extract_title(page_html):
    text = html_lib.unescape(page_html)
    patterns = [
        r'"desc"\s*:\s*"([^"]+)"',
        r'<title[^>]*>(.*?)</title>',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            value = match.group(1).strip()
            value = re.sub(r"\s+", " ", value)
            value = value.replace("\\n", " ")
            if value:
                return value
    return "douyin_video"


def build_filename(title, aweme_id):
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", title, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:80] or "douyin_video"
    return f"{cleaned}_{aweme_id}.mp4"


def download_douyin_video(text, save_dir=DEFAULT_SAVE_DIR):
    source_url = extract_url(text)
    final_url = resolve_url(source_url)
    aweme_id = extract_aweme_id(final_url)
    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    _, _, page_bytes = fetch(share_url, referer=source_url)
    page_html = page_bytes.decode("utf-8", errors="ignore")
    play_url = extract_play_url(page_html)
    title = extract_title(page_html)
    filename = build_filename(title, aweme_id)

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        stem = target.stem
        target = target.with_name(f"{stem}_{int(time.time())}{target.suffix}")

    _, headers, video_bytes = fetch(play_url, referer=share_url)
    content_type = headers.get("Content-Type", "")
    if "video" not in content_type and not video_bytes.startswith(b"\x00\x00"):
        raise DownloadError("下载结果不像视频文件，可能需要登录或链接已失效")
    target.write_bytes(video_bytes)

    return {
        "ok": True,
        "path": str(target),
        "filename": target.name,
        "bytes": target.stat().st_size,
        "aweme_id": aweme_id,
        "title": title,
        "play_url": play_url,
    }
