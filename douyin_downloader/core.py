import base64
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

# MiMo API
_MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
_MIMO_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
_MIMO_MODEL = "mimo-v2.5"


class DownloadError(Exception):
    pass


# ── Platform detection ──────────────────────────────────────────────────────

PLATFORM_RULES = [
    (r"(?:v\.douyin\.com|iesdouyin\.com|douyin\.com/video)", "douyin"),
    (r"(?:xiaohongshu\.com|xhslink\.com)", "xiaohongshu"),
    (r"(?:bilibili\.com|b23\.tv)", "bilibili"),
    (r"(?:youtube\.com|youtu\.be)", "youtube"),
    (r"(?:kuaishou\.com|gifshow\.com|v\.kuaishou\.com)", "kuaishou"),
    (r"(?:weixin\.qq\.com/s/|channels\.weixin\.qq\.com)", "shipinhao"),
]

# Platforms that support native subtitles via yt-dlp
SUBTITLE_PLATFORMS = {"youtube", "bilibili"}


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


def find_ffmpeg():
    return shutil.which("ffmpeg")


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


def _get_douyin_info(source_url):
    """Resolve douyin URL and return (play_url, title, aweme_id, share_url)."""
    final_url = _douyin_resolve(source_url)
    aweme_id = _douyin_extract_id(final_url)
    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    _, _, page_bytes = fetch(share_url, referer=source_url)
    page_html = page_bytes.decode("utf-8", errors="ignore")
    play_url = _douyin_extract_play_url(page_html)
    title = _douyin_extract_title(page_html)
    return play_url, title, aweme_id, share_url


def download_douyin(source_url, save_dir):
    play_url, title, aweme_id, share_url = _get_douyin_info(source_url)
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
        ytdlp, "--no-playlist", "--merge-output-format", "mp4",
        "-o", outtmpl, "--print", "after_move:filepath", url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise DownloadError("下载超时（5分钟）")

    if proc.returncode != 0:
        err = proc.stderr.strip().split("\n")[-1] if proc.stderr else "未知错误"
        raise DownloadError(f"下载失败：{err}")

    filepath = proc.stdout.strip().split("\n")[-1]
    if not filepath or not os.path.exists(filepath):
        raise DownloadError("下载完成但找不到文件")

    p = Path(filepath)
    return {"ok": True, "path": str(p), "filename": p.name,
            "bytes": p.stat().st_size, "title": p.stem.rsplit("_", 1)[0],
            "platform": platform_name}


# ── 小红书 ──────────────────────────────────────────────────────────────────

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


def _get_xhs_info(source_url):
    """Return (play_url, title, video_id)."""
    final_url, _, page_bytes = fetch(source_url, referer="https://www.xiaohongshu.com/")
    page_html = page_bytes.decode("utf-8", errors="ignore")

    play_url = None
    title = "xiaohongshu_video"

    m = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', page_html)
    if m:
        key = m.group(1)
        play_url = key if key.startswith("http") else f"https://sns-video-bd.xhscdn.com/{key}"

    if not play_url:
        m = re.search(r'"url"\s*:\s*"(https?://[^\"]*(?:\.mp4|video)[^\"]*)"', page_html)
        if m:
            play_url = m.group(1)

    if not play_url:
        m = re.search(r'<script>window\.__INITIAL_STATE__\s*=\s*({.+?})</script>', page_html, re.S)
        if m:
            try:
                data = json.loads(m.group(1).replace("undefined", "null"))
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

    aweme_id = re.search(r"/explore/(\w+)", final_url) or re.search(r"/(\w+)$", final_url)
    vid = aweme_id.group(1) if aweme_id else str(int(time.time()))
    return play_url, title, vid


def download_xiaohongshu(source_url, save_dir):
    play_url, title, vid = _get_xhs_info(source_url)
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


# ── 视频号 ──────────────────────────────────────────────────────────────────

def download_shipinhao(source_url, save_dir):
    raise DownloadError(
        "视频号暂时不支持下载。\n"
        "建议用手机端的录屏功能或者第三方微信视频号下载工具。"
    )


# ── 提取音频 ────────────────────────────────────────────────────────────────

def _ensure_video(url, save_dir, platform):
    """Download video to a temp path, return (video_path, title, cleanup_needed).
    For yt-dlp platforms, just download directly.
    For douyin/xiaohongshu, use platform-specific download.
    """
    if platform in ("bilibili", "youtube", "kuaishou"):
        ytdlp = find_yt_dlp()
        if not ytdlp:
            raise DownloadError("yt-dlp 未安装")
        target_dir = Path(save_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(target_dir / "%(title).80s_%(id)s.%(ext)s")
        cmd = [ytdlp, "--no-playlist", "--merge-output-format", "mp4",
               "-o", outtmpl, "--print", "after_move:filepath", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise DownloadError("下载超时")
        if proc.returncode != 0:
            err = proc.stderr.strip().split("\n")[-1] if proc.stderr else "未知错误"
            raise DownloadError(f"下载失败：{err}")
        filepath = proc.stdout.strip().split("\n")[-1]
        if not filepath or not os.path.exists(filepath):
            raise DownloadError("下载完成但找不到文件")
        return filepath, Path(filepath).stem, False

    elif platform == "douyin":
        play_url, title, aweme_id, share_url = _get_douyin_info(url)
        tmp_path = Path(save_dir).expanduser() / f"tmp_{int(time.time())}.mp4"
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        _, _, video_bytes = fetch(play_url, referer=share_url)
        tmp_path.write_bytes(video_bytes)
        return str(tmp_path), title, True

    elif platform == "xiaohongshu":
        play_url, title, vid = _get_xhs_info(url)
        tmp_path = Path(save_dir).expanduser() / f"tmp_{int(time.time())}.mp4"
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        _, _, video_bytes = fetch(play_url, referer="https://www.xiaohongshu.com/")
        tmp_path.write_bytes(video_bytes)
        return str(tmp_path), title, True

    else:
        raise DownloadError(f"不支持该平台的音频提取")


def extract_audio(text, save_dir=DEFAULT_SAVE_DIR):
    """提取音频为 mp3 文件。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg 未安装，请运行: brew install ffmpeg")

    source_url = extract_url(text)
    platform = detect_platform(source_url)
    if not platform:
        raise DownloadError("暂不支持该链接")

    video_path, title, need_cleanup = _ensure_video(source_url, save_dir, platform)

    # Generate output filename
    target_dir = Path(save_dir).expanduser()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", title, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:80] or "audio"
    out_path = target_dir / f"{cleaned}.mp3"
    if out_path.exists():
        out_path = out_path.with_name(f"{out_path.stem}_{int(time.time())}.mp3")

    # Extract audio
    cmd = [ffmpeg, "-i", video_path, "-vn", "-acodec", "libmp3lame",
           "-q:a", "2", "-y", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    # Cleanup temp video
    if need_cleanup and os.path.exists(video_path):
        os.remove(video_path)

    if proc.returncode != 0:
        raise DownloadError(f"音频提取失败：{proc.stderr[-200:]}")

    PLATFORM_NAMES = {"douyin": "抖音", "xiaohongshu": "小红书",
                      "bilibili": "B站", "youtube": "YouTube", "kuaishou": "快手"}

    return {"ok": True, "path": str(out_path), "filename": out_path.name,
            "bytes": out_path.stat().st_size, "title": title,
            "platform": PLATFORM_NAMES.get(platform, platform)}


# ── 提取字幕 ────────────────────────────────────────────────────────────────

def extract_subtitles(text, save_dir=DEFAULT_SAVE_DIR):
    """提取平台自带字幕（仅 YouTube / B站）。"""
    source_url = extract_url(text)
    platform = detect_platform(source_url)
    if not platform:
        raise DownloadError("暂不支持该链接")

    if platform not in SUBTITLE_PLATFORMS:
        PLATFORM_NAMES = {"douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手"}
        name = PLATFORM_NAMES.get(platform, platform)
        raise DownloadError(
            f"{name} 不支持提取字幕（平台未提供）。\n"
            f"目前支持提取字幕的平台：YouTube、B站"
        )

    ytdlp = find_yt_dlp()
    if not ytdlp:
        raise DownloadError("yt-dlp 未安装")

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / "%(title).80s_%(id)s")

    cmd = [
        ytdlp, "--no-playlist", "--skip-download",
        "--write-auto-sub", "--sub-lang", "zh.*", "--sub-format", "srt",
        "--convert-subs", "srt",
        "-o", outtmpl, "--print", "after_move:filepath",
        url if (url := source_url) else source_url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise DownloadError("字幕提取超时")

    # yt-dlp with --skip-download and --print after_move:filepath prints video filepath
    # but we need to find the subtitle file instead
    # Search for .srt files modified in the last 30 seconds
    import glob
    srt_files = sorted(
        glob.glob(str(target_dir / "*.srt")),
        key=os.path.getmtime, reverse=True
    )
    recent_srt = None
    now = time.time()
    for f in srt_files:
        if now - os.path.getmtime(f) < 60:
            recent_srt = f
            break

    if not recent_srt:
        # Try fetching subtitle content directly
        raise DownloadError(
            "没有找到字幕文件。\n"
            "可能原因：该视频没有自动生成字幕，或字幕语言不匹配。"
        )

    # Read and clean SRT content
    srt_content = Path(recent_srt).read_text(encoding="utf-8", errors="ignore")
    # Convert SRT to plain text
    plain_text = _srt_to_text(srt_content)

    PLATFORM_NAMES = {"youtube": "YouTube", "bilibili": "B站"}

    return {
        "ok": True,
        "path": recent_srt,
        "filename": Path(recent_srt).name,
        "bytes": Path(recent_srt).stat().st_size,
        "text": plain_text,
        "platform": PLATFORM_NAMES.get(platform, platform),
    }


def _srt_to_text(srt_content):
    """Convert SRT subtitle to plain text."""
    lines = []
    for line in srt_content.split("\n"):
        line = line.strip()
        # Skip sequence numbers, timestamps, empty lines
        if not line or re.match(r"^\d+$", line) or re.match(
                r"\d{2}:\d{2}:\d{2}", line) or "-->" in line:
            continue
        # Remove HTML tags
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    # Deduplicate consecutive identical lines
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return "\n".join(deduped)


# ── 提取文案 ────────────────────────────────────────────────────────────────

def extract_transcript(text, save_dir=DEFAULT_SAVE_DIR):
    """提取文案：优先用平台字幕，没有的用 MiMo V2.5 识别音频。"""
    source_url = extract_url(text)
    platform = detect_platform(source_url)
    if not platform:
        raise DownloadError("暂不支持该链接")

    # Step 1: Try subtitles for platforms that support them
    if platform in SUBTITLE_PLATFORMS:
        try:
            result = extract_subtitles(text, save_dir)
            return {
                "ok": True,
                "method": "字幕提取",
                "text": result["text"],
                "platform": result["platform"],
            }
        except DownloadError:
            pass  # Fall through to MiMo

    # Step 2: Use MiMo V2.5 for audio transcription
    if not _MIMO_API_KEY:
        raise DownloadError(
            "未配置 XIAOMI_API_KEY，无法使用语音识别。\n"
            "请在 ~/.hermes/.env 中设置 XIAOMI_API_KEY。"
        )

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg 未安装")

    # Download video and extract audio
    video_path, title, need_cleanup = _ensure_video(source_url, save_dir, platform)

    # Convert to WAV for MiMo (smaller than mp3 for base64, but wav is simpler)
    audio_path = video_path.rsplit(".", 1)[0] + ".wav"
    cmd = [ffmpeg, "-i", video_path, "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-y", audio_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if need_cleanup and os.path.exists(video_path):
        os.remove(video_path)

    if proc.returncode != 0:
        raise DownloadError("音频转换失败")

    # Read audio and encode to base64
    audio_data = Path(audio_path).read_bytes()
    audio_b64 = base64.b64encode(audio_data).decode("ascii")
    os.remove(audio_path)

    # If audio is too large (>10MB base64), warn
    if len(audio_b64) > 14_000_000:
        raise DownloadError(
            f"音频文件太大（{len(audio_data) // 1024 // 1024}MB），超出 MiMo API 限制。\n"
            "建议使用较短的视频，或手动截取需要识别的片段。"
        )

    # Call MiMo V2.5 API
    transcript = _call_mimo_asr(audio_b64)

    PLATFORM_NAMES = {"douyin": "抖音", "xiaohongshu": "小红书",
                      "bilibili": "B站", "youtube": "YouTube", "kuaishou": "快手"}

    return {
        "ok": True,
        "method": "MiMo 语音识别",
        "text": transcript,
        "title": title,
        "platform": PLATFORM_NAMES.get(platform, platform),
    }


def _call_mimo_asr(audio_b64):
    """Call MiMo V2.5 API to transcribe audio."""
    payload = {
        "model": _MIMO_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:audio/wav;base64,{audio_b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "请完整提取这段音频中的所有文字内容，保持原始顺序和完整性。"
                            "只输出文字内容本身，不要添加任何说明、总结或格式标记。"
                            "如果有多个说话人，不需要区分说话人，只需连续输出所有文字。"
                        )
                    }
                ]
            }
        ],
        "max_completion_tokens": 8192,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _MIMO_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "api-key": _MIMO_API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise DownloadError(f"MiMo API 请求失败 (HTTP {exc.code})：{body[:200]}")
    except urllib.error.URLError as exc:
        raise DownloadError(f"MiMo API 网络错误：{exc.reason}")

    # Extract content from response
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise DownloadError(f"MiMo API 返回格式异常：{json.dumps(result)[:200]}")

    if not content or not content.strip():
        # MiMo sometimes puts transcription in reasoning_content
        try:
            content = result["choices"][0]["message"]["reasoning_content"]
        except (KeyError, IndexError):
            pass

    if not content or not content.strip():
        raise DownloadError("MiMo API 返回了空内容，音频可能无法识别")

    return content.strip()


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
        raise DownloadError(f"暂不支持该链接。\n目前支持：{SUPPORTED_PLATFORMS}")
    name, func = PLATFORM_MAP[platform]
    return func(source_url, save_dir)
