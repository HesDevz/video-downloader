import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from douyin_downloader.core import (
    DEFAULT_SAVE_DIR, DownloadError,
    download_video, extract_audio, extract_subtitles, extract_srt, extract_transcript,
    SUPPORTED_PLATFORMS,
)


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(PUBLIC / "index.html", "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):
        routes = {
            "/api/download": download_video,
            "/api/audio": extract_audio,
            "/api/subtitles": extract_subtitles,
            "/api/srt": extract_srt,
            "/api/transcript": extract_transcript,
        }

        func = routes.get(self.path)
        if not func:
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body)
            result = func(payload.get("url", ""), DEFAULT_SAVE_DIR)
            self._send_json(200, result)
        except DownloadError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"服务内部错误：{exc}"})

    def _send_file(self, path, content_type):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 8787), Handler)
    print("视频下载器 running at http://localhost:8787")
    print(f"支持平台：{SUPPORTED_PLATFORMS}")
    print(f"保存到：{DEFAULT_SAVE_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
