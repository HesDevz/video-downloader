import base64
import glob
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

# MiMo API (loaded from .env by server)
_MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
_MIMO_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
_MIMO_MODEL = "mimo-v2.5"


def load_env():
    """Load XIAOMI_API_KEY from ~/.hermes/.env if not already set."""
    global _MIMO_API_KEY
    if _MIMO_API_KEY:
        return
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "XIAOMI_API_KEY" and val:
                _MIMO_API_KEY = val
                os.environ["XIAOMI_API_KEY"] = val
                break


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

SUBTITLE_PLATFORMS = {"youtube", "bilibili"}

PLATFORM_NAMES = {
    "douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站",
    "youtube": "YouTube", "kuaishou": "快手", "shipinhao": "视频号",
}


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


def safe_filename(title, vid, ext="mp4", max_len=80):
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", title, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:max_len] or "video"
    return f"{cleaned}_{vid}.{ext}"


def find_yt_dlp():
    return shutil.which("yt-dlp")


def find_ffmpeg():
    return shutil.which("ffmpeg")


def _run_ytdlp(cmd, timeout=300):
    """Run yt-dlp command, return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        raise DownloadError("下载超时（5分钟）")


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


# ── yt-dlp 通用下载器 ──────────────────────────────────────────────────────

def download_ytdlp(url, save_dir, platform_name):
    ytdlp = find_yt_dlp()
    if not ytdlp:
        raise DownloadError("yt-dlp 未安装，请运行: brew install yt-dlp")
    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / "%(title).80s_%(id)s.%(ext)s")

    cmd = [ytdlp, "--no-playlist", "--merge-output-format", "mp4",
           "-o", outtmpl, "--print", "after_move:filepath", url]
    rc, stdout, stderr = _run_ytdlp(cmd)
    if rc != 0:
        raise DownloadError(f"下载失败：{stderr.strip().split(chr(10))[-1]}")

    filepath = stdout.strip().split("\n")[-1]
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
        raise DownloadError("没有在小红书页面找到视频地址。\n可能原因：链接是图文笔记、链接已失效、或需要登录。")
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


def download_shipinhao(source_url, save_dir):
    raise DownloadError("视频号暂时不支持下载。建议用手机端录屏。")


# ── 提取音频 ────────────────────────────────────────────────────────────────

def extract_audio(text, save_dir=DEFAULT_SAVE_DIR):
    """提取音频为 mp3 文件。yt-dlp 平台只拉音频流，不下载视频。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg 未安装，请运行: brew install ffmpeg")

    source_url = extract_url(text)
    platform = detect_platform(source_url)
    if not platform:
        raise DownloadError("暂不支持该链接")

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    if platform in ("bilibili", "youtube", "kuaishou"):
        # yt-dlp: 只下载音频流，不下载视频
        ytdlp = find_yt_dlp()
        if not ytdlp:
            raise DownloadError("yt-dlp 未安装")
        outtmpl = str(target_dir / "%(title).80s_%(id)s.%(ext)s")
        cmd = [
            ytdlp, "--no-playlist", "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", outtmpl, "--print", "after_move:filepath", source_url,
        ]
        rc, stdout, stderr = _run_ytdlp(cmd)
        if rc != 0:
            raise DownloadError(f"音频提取失败：{stderr.strip().split(chr(10))[-1]}")
        filepath = stdout.strip().split("\n")[-1]
        if not filepath or not os.path.exists(filepath):
            raise DownloadError("提取完成但找不到文件")
        p = Path(filepath)
        return {"ok": True, "path": str(p), "filename": p.name,
                "bytes": p.stat().st_size, "title": p.stem.rsplit("_", 1)[0],
                "platform": PLATFORM_NAMES.get(platform, platform)}

    # 抖音/小红书: 下载视频 → ffmpeg 提取 → 删除视频
    if platform == "douyin":
        play_url, title, aweme_id, share_url = _get_douyin_info(source_url)
        tmp_video = target_dir / f"_tmp_{int(time.time())}.mp4"
        _, _, video_bytes = fetch(play_url, referer=share_url)
        tmp_video.write_bytes(video_bytes)
        vid = aweme_id
    elif platform == "xiaohongshu":
        play_url, title, vid = _get_xhs_info(source_url)
        tmp_video = target_dir / f"_tmp_{int(time.time())}.mp4"
        _, _, video_bytes = fetch(play_url, referer="https://www.xiaohongshu.com/")
        tmp_video.write_bytes(video_bytes)
    else:
        raise DownloadError(f"不支持该平台的音频提取")

    out_path = target_dir / safe_filename(title, vid, ext="mp3")
    if out_path.exists():
        out_path = out_path.with_name(f"{out_path.stem}_{int(time.time())}.mp3")

    cmd = [ffmpeg, "-i", str(tmp_video), "-vn", "-acodec", "libmp3lame",
           "-q:a", "2", "-y", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    tmp_video.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise DownloadError(f"音频提取失败：{proc.stderr[-200:]}")

    return {"ok": True, "path": str(out_path), "filename": out_path.name,
            "bytes": out_path.stat().st_size, "title": title,
            "platform": PLATFORM_NAMES.get(platform, platform)}


# ── 提取字幕 ────────────────────────────────────────────────────────────────

def _fetch_subtitles_raw(source_url, save_dir):
    """Fetch subtitle file via yt-dlp, return (srt_content, filepath, platform)."""
    platform = detect_platform(source_url)
    if not platform:
        raise DownloadError("暂不支持该链接")
    if platform not in SUBTITLE_PLATFORMS:
        name = PLATFORM_NAMES.get(platform, platform)
        raise DownloadError(f"{name} 不支持提取字幕（平台未提供）。\n目前支持：YouTube、B站")

    ytdlp = find_yt_dlp()
    if not ytdlp:
        raise DownloadError("yt-dlp 未安装")

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    # 先列出可用字幕
    list_cmd = [ytdlp, "--list-subs", "--no-playlist", source_url]
    rc, list_out, list_err = _run_ytdlp(list_cmd, timeout=60)

    # 优先选中文字幕，其次英文字幕，最后自动
    sub_lang = "zh"
    if rc == 0 and "zh" not in list_out:
        if "en" in list_out:
            sub_lang = "en"
        else:
            # 用第一个可用语言
            for line in list_out.splitlines():
                if line and not line.startswith("Available") and not line.startswith("Language"):
                    parts = line.split()
                    if parts:
                        sub_lang = parts[0]
                        break

    outtmpl = str(target_dir / "%(title).80s_%(id)s")

    # 下载字幕（优先自动字幕）
    cmd = [
        ytdlp, "--no-playlist", "--skip-download",
        "--write-sub", "--write-auto-sub",
        "--sub-lang", sub_lang, "--sub-format", "srt",
        "--convert-subs", "srt",
        "-o", outtmpl,
        source_url,
    ]
    rc, stdout, stderr = _run_ytdlp(cmd, timeout=120)
    if rc != 0:
        err_line = stderr.strip().split("\n")[-1] if stderr else "未知错误"
        raise DownloadError(f"字幕下载失败：{err_line}")

    # 找最近生成的 .srt 文件
    srt_files = sorted(glob.glob(str(target_dir / "*.srt")), key=os.path.getmtime, reverse=True)
    now = time.time()
    recent_srt = None
    for f in srt_files:
        if now - os.path.getmtime(f) < 120:
            recent_srt = f
            break

    if not recent_srt:
        raise DownloadError(
            "没有找到字幕文件。\n"
            "可能原因：该视频没有字幕（包括自动生成字幕）。"
        )

    srt_content = Path(recent_srt).read_text(encoding="utf-8", errors="ignore")
    return srt_content, recent_srt, platform


def extract_subtitles(text, save_dir=DEFAULT_SAVE_DIR):
    """提取字幕为纯文本（去掉时间线）。"""
    source_url = extract_url(text)
    srt_content, filepath, platform = _fetch_subtitles_raw(source_url, save_dir)
    return {
        "ok": True, "text": _srt_to_text(srt_content),
        "path": filepath, "filename": Path(filepath).name,
        "bytes": Path(filepath).stat().st_size,
        "title": Path(filepath).stem.rsplit("_", 1)[0],
        "platform": PLATFORM_NAMES.get(platform, platform),
    }


def extract_srt(text, save_dir=DEFAULT_SAVE_DIR):
    """提取 SRT 字幕（带时间线）。"""
    source_url = extract_url(text)
    srt_content, filepath, platform = _fetch_subtitles_raw(source_url, save_dir)
    return {
        "ok": True, "text": srt_content,
        "path": filepath, "filename": Path(filepath).name,
        "bytes": Path(filepath).stat().st_size,
        "title": Path(filepath).stem.rsplit("_", 1)[0],
        "platform": PLATFORM_NAMES.get(platform, platform),
    }


def _srt_to_text(srt_content):
    """Convert SRT to plain text, deduplicate consecutive lines."""
    lines = []
    for line in srt_content.split("\n"):
        line = line.strip()
        if not line or re.match(r"^\d+$", line) or re.match(r"\d{2}:\d{2}:\d{2}", line) or "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
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

    # Step 1: 有字幕的平台先试字幕
    if platform in SUBTITLE_PLATFORMS:
        try:
            srt_content, filepath, plat = _fetch_subtitles_raw(source_url, save_dir)
            return {
                "ok": True, "method": "字幕提取",
                "text": _srt_to_text(srt_content),
                "title": Path(filepath).stem.rsplit("_", 1)[0],
                "platform": PLATFORM_NAMES.get(plat, plat),
            }
        except DownloadError:
            pass  # 字幕不可用，走 MiMo

    # Step 2: MiMo V2.5 语音识别
    if not _MIMO_API_KEY:
        raise DownloadError(
            "未配置 XIAOMI_API_KEY，无法使用语音识别。\n"
            "请在 ~/.hermes/.env 中设置 XIAOMI_API_KEY。"
        )

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg 未安装")

    target_dir = Path(save_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    # 下载音频（yt-dlp 平台只拉音频流）
    if platform in ("bilibili", "youtube", "kuaishou"):
        ytdlp = find_yt_dlp()
        if not ytdlp:
            raise DownloadError("yt-dlp 未安装")
        tmp_audio = target_dir / f"_tmp_{int(time.time())}.m4a"
        cmd = [ytdlp, "--no-playlist", "-x", "--audio-format", "m4a",
               "-o", str(tmp_audio), source_url]
        rc, stdout, stderr = _run_ytdlp(cmd)
        if rc != 0:
            raise DownloadError(f"音频下载失败：{stderr.strip().split(chr(10))[-1]}")
        audio_src = tmp_audio
        title = tmp_audio.stem.rsplit("_", 1)[0]
        # 从 stdout 获取实际文件名
        actual = stdout.strip().split("\n")[-1]
        if actual and os.path.exists(actual):
            audio_src = Path(actual)
            title = audio_src.stem.rsplit("_", 1)[0]
    else:
        # 抖音/小红书：下载视频 → 转 wav
        if platform == "douyin":
            play_url, title, aweme_id, share_url = _get_douyin_info(source_url)
            tmp_video = target_dir / f"_tmp_{int(time.time())}.mp4"
            _, _, video_bytes = fetch(play_url, referer=share_url)
            tmp_video.write_bytes(video_bytes)
        elif platform == "xiaohongshu":
            play_url, title, vid = _get_xhs_info(source_url)
            tmp_video = target_dir / f"_tmp_{int(time.time())}.mp4"
            _, _, video_bytes = fetch(play_url, referer="https://www.xiaohongshu.com/")
            tmp_video.write_bytes(video_bytes)
        else:
            raise DownloadError("不支持该平台")

        audio_src = target_dir / f"_tmp_audio_{int(time.time())}.wav"
        cmd = [ffmpeg, "-i", str(tmp_video), "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", "-y", str(audio_src)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        tmp_video.unlink(missing_ok=True)

    # 转成 wav 给 MiMo（如果还不是 wav）
    wav_path = target_dir / f"_tmp_mimo_{int(time.time())}.wav"
    cmd = [ffmpeg, "-i", str(audio_src), "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-y", str(wav_path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    # 清理临时音频
    if str(audio_src) != str(wav_path):
        Path(audio_src).unlink(missing_ok=True)

    if not wav_path.exists() or wav_path.stat().st_size < 1000:
        raise DownloadError("音频转换失败")

    # Base64 编码
    audio_data = wav_path.read_bytes()
    wav_path.unlink(missing_ok=True)
    audio_b64 = base64.b64encode(audio_data).decode("ascii")

    if len(audio_b64) > 14_000_000:
        raise DownloadError(
            f"音频太大（{len(audio_data) // 1024 // 1024}MB），超出 MiMo API 限制。\n"
            "建议使用较短的视频。"
        )

    # 调 MiMo API
    transcript = _call_mimo_asr(audio_b64)

    return {
        "ok": True, "method": "MiMo 语音识别",
        "text": transcript, "title": title,
        "platform": PLATFORM_NAMES.get(platform, platform),
    }


def _call_mimo_asr(audio_b64):
    payload = {
        "model": _MIMO_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": f"data:audio/wav;base64,{audio_b64}"}},
                {"type": "text", "text": (
                    "请完整提取这段音频中的所有文字内容，保持原始顺序和完整性。"
                    "只输出文字内容本身，不要添加任何说明、总结或格式标记。"
                    "如果有多个说话人，不需要区分说话人，只需连续输出所有文字。"
                )},
            ],
        }],
        "max_completion_tokens": 8192,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_MIMO_API_URL, data=data, headers={
        "Content-Type": "application/json", "api-key": _MIMO_API_KEY,
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise DownloadError(f"MiMo API 请求失败 (HTTP {exc.code})：{body[:200]}")
    except urllib.error.URLError as exc:
        raise DownloadError(f"MiMo API 网络错误：{exc.reason}")

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise DownloadError(f"MiMo API 返回格式异常：{json.dumps(result)[:200]}")

    if not content or not content.strip():
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
