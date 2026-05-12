import html as html_lib
import json
import re
import time
import subprocess
from pathlib import Path


DEFAULT_SAVE_DIR = Path.home() / "Desktop" / "下载"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 aweme"
)
PC_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    pass


def _curl(url, *, method="GET", headers=None, timeout=25, follow=True, save_cookies=None, load_cookies=None):
    """Run curl and return (final_url, resp_headers_dict, body_bytes)."""
    cmd = ["curl", "-sSL" if follow else "-sS", "-D-", "--max-time", str(timeout)]
    if method == "HEAD":
        cmd += ["--head", "-o", "/dev/null"]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if save_cookies:
        cmd += ["-c", save_cookies]
    if load_cookies:
        cmd += ["-b", load_cookies]
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError("网络请求失败：超时") from exc

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise DownloadError(f"网络请求失败：{err or proc.returncode}")

    raw = proc.stdout
    # Parse multiple HTTP response blocks (redirects produce multiple header sets)
    # Find the LAST header block
    blocks = raw.split(b"\r\n\r\n")
    if len(blocks) >= 2:
        # Last header block is blocks[-2], body is blocks[-1]
        head_text = blocks[-2].decode("utf-8", errors="ignore")
        body = blocks[-1]
    else:
        head_text = raw.decode("utf-8", errors="ignore")
        body = b""

    # Extract Location from the first header block (the 302)
    first_head = blocks[0].decode("utf-8", errors="ignore") if blocks else ""
    location = None
    for line in first_head.splitlines():
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
            break

    # Build headers dict from last response
    hdr_lines = head_text.splitlines()
    # skip status line
    if hdr_lines and hdr_lines[0].startswith("HTTP/"):
        hdr_lines = hdr_lines[1:]

    class _Headers:
        def __init__(self, lines):
            self._lines = lines
        def get(self, key, default=""):
            prefix = key.lower() + ":"
            for l in self._lines:
                if l.lower().startswith(prefix):
                    return l.split(":", 1)[1].strip()
            return default
        def __getitem__(self, key):
            return self.get(key)

    final_url = location or url
    return final_url, _Headers(hdr_lines), body


def extract_url(text):
    match = re.search(r"https?://[^\s，。]+", text or "")
    if not match:
        raise DownloadError("没有找到抖音链接")
    return match.group(0)


def resolve_share_url(url):
    """Follow short link to get the actual page URL with video ID."""
    final_url, _, _ = _curl(url, method="HEAD",
                            headers={"User-Agent": USER_AGENT})
    return final_url


def extract_aweme_id(url):
    patterns = [
        r"/video/(\d+)",
        r"aweme_id=(\d+)",
        r"item_ids=(\d+)",
        r"/note/(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_video_info_via_web(aweme_id, referer=None):
    """Use douyin.com web page to extract video info."""
    # Method 1: Try the detail page directly
    detail_url = f"https://www.douyin.com/video/{aweme_id}"
    headers = {
        "User-Agent": PC_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    cookies_file = "/tmp/douyin_cookies.txt"
    _, _, page_bytes = _curl(detail_url, headers=headers, save_cookies=cookies_file)
    page_html = page_bytes.decode("utf-8", errors="ignore")

    # Try to extract video info from SSR data in the page
    play_url = None
    title = None

    # Pattern 1: RENDER_DATA JSON in script tag
    render_match = re.search(r'<script\s+id="RENDER_DATA"[^>]*>(.+?)</script>', page_html, re.S)
    if render_match:
        try:
            import urllib.parse
            raw_data = urllib.parse.unquote(render_match.group(1))
            data = json.loads(raw_data)
            # Walk the nested structure to find video info
            play_url, title = _extract_from_render_data(data, aweme_id)
        except (json.JSONDecodeError, Exception):
            pass

    # Pattern 2: Direct play URL in page
    if not play_url:
        play_url = _extract_play_url_from_html(page_html)

    if not title:
        title = _extract_title_from_html(page_html)

    return play_url, title, page_html


def _extract_from_render_data(data, aweme_id):
    """Walk RENDER_DATA JSON to find video play URL and title."""
    play_url = None
    title = None

    def walk(obj, depth=0):
        nonlocal play_url, title
        if depth > 15:
            return
        if isinstance(obj, dict):
            # Look for video play_addr
            if "play_addr" in obj:
                addr = obj["play_addr"]
                if isinstance(addr, dict) and "url_list" in addr:
                    urls = addr["url_list"]
                    if urls:
                        play_url = urls[0]
            # Look for desc/title
            if "desc" in obj and isinstance(obj["desc"], str) and len(obj["desc"]) > 2:
                if not title:
                    title = obj["desc"]
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(data)
    return play_url, title


def _extract_play_url_from_html(html):
    """Try various regex patterns to find video play URL."""
    unescaped = html_lib.unescape(html)

    # Pattern: playwm or /play/ URLs in escaped JSON
    candidates = re.findall(r'https:\\u002F\\u002F[^"\\]+(?:\\u002F[^"\\]*)*', unescaped)
    for c in candidates:
        url = c.replace("\\u002F", "/")
        if "playwm" in url or "/play/" in url:
            return url

    # Pattern: play_addr url_list
    match = re.search(r'"play_addr"\s*:\s*\{.*?"url_list"\s*:\s*(\[.*?\])', unescaped, re.S)
    if match:
        try:
            urls = json.loads(match.group(1))
            for u in urls:
                if "playwm" in u or "/play/" in u:
                    return u
            if urls:
                return urls[0]
        except json.JSONDecodeError:
            pass

    # Pattern: video src
    match = re.search(r'<video[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)

    return None


def _extract_title_from_html(html):
    """Extract title from page HTML."""
    text = html_lib.unescape(html)
    patterns = [
        r'"desc"\s*:\s*"([^"]{3,})"',
        r'<title[^>]*>(.*?)</title>',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            value = match.group(1).strip()
            value = re.sub(r"\s+", " ", value)
            value = value.replace("\\n", " ")
            if value and "抖音" not in value[:5]:
                return value
    return "douyin_video"


def build_filename(title, aweme_id):
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", title, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:80] or "douyin_video"
    return f"{cleaned}_{aweme_id}.mp4"


def download_douyin_video(text, save_dir=DEFAULT_SAVE_DIR):
    source_url = extract_url(text)

    # Step 1: Resolve short link
    final_url = resolve_share_url(source_url)

    # Step 2: Extract aweme_id
    aweme_id = extract_aweme_id(final_url)

    # If short link didn't resolve to a video URL, try extracting from the
    # share page directly (some links resolve to mobile page with embedded data)
    if not aweme_id:
        # Try fetching the mobile page and extracting from HTML
        _, _, mobile_html = _curl(final_url, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
        })
        mobile_text = mobile_html.decode("utf-8", errors="ignore")
        aweme_id = extract_aweme_id(mobile_text)
        if not aweme_id:
            # Try regex for aweme_id in page data
            m = re.search(r'"aweme_id"\s*:\s*"?(\d+)"?', mobile_text)
            if m:
                aweme_id = m.group(1)

    if not aweme_id:
        raise DownloadError("没有从链接里识别到视频 ID，链接可能已失效或需要在App内打开")

    # Step 3: Get video info from web page
    play_url, title, page_html = get_video_info_via_web(aweme_id, referer=source_url)

    if not play_url:
        raise DownloadError(
            f"没有在页面里找到视频播放地址 (ID: {aweme_id})。\n"
            "可能原因：视频已删除、需要登录、或接口变更。"
        )

    if not title:
        title = "douyin_video"

    # Step 4: Download the video
    filename = build_filename(title, aweme_id)
    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        stem = target.stem
        target = target.with_name(f"{stem}_{int(time.time())}{target.suffix}")

    # Download with curl (handles redirects and SSL properly)
    dl_cmd = [
        "curl", "-sSL", "-o", str(target),
        "-H", f"User-Agent: {PC_USER_AGENT}",
        "-H", f"Referer: https://www.douyin.com/video/{aweme_id}",
        "--max-time", "120",
        play_url,
    ]
    proc = subprocess.run(dl_cmd, capture_output=True, timeout=130)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise DownloadError(f"视频下载失败：{err}")

    # Verify it's a video
    if target.exists():
        file_size = target.stat().st_size
        if file_size < 10000:  # Less than 10KB is suspicious
            with open(target, "rb") as f:
                head = f.read(200)
            head_text = head.decode("utf-8", errors="ignore").lower()
            if "<html" in head_text or "error" in head_text or "登录" in head_text:
                target.unlink()
                raise DownloadError(
                    "下载结果不像视频文件，可能需要登录或链接已失效。\n"
                    f"服务器返回了HTML页面（{file_size}字节）而不是视频。"
                )
    else:
        raise DownloadError("下载完成但文件不存在")

    return {
        "ok": True,
        "path": str(target),
        "filename": target.name,
        "bytes": target.stat().st_size,
        "aweme_id": aweme_id,
        "title": title,
        "play_url": play_url,
    }
