# -*- coding: utf-8 -*-
"""
ppb_preview.py —— Personal Page Builder 的预览窗口子进程
=====================================================

为什么不直接在主程序里开 pywebview：
    webview.start() 会独占当前线程的事件循环，而 tkinter 同样需要主线程，
    两者无法共存于一个进程。所以预览放在子进程里跑，主程序只负责生成 HTML。

子进程做三件事：
    1. 起一个本地 HTTP 服务：/ 返回预览 HTML，/media/<name> 返回素材原文件，
       其他路径回退到仓库目录（这样 resume.pdf 之类的相对链接也能预览）
    2. 用 pywebview 打开 http://127.0.0.1:<port>/
    3. 页面内的 JS 每 450ms 请求 /_rev 比对文件修改时间，变了就自动 reload

独立运行（调试用）：
    python ppb_preview.py --html preview.html --media /path/to/assets --fallback /path/to/repo
"""

import argparse
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

# 只认本机回环地址：别的网页就算把自己的域名解析到 127.0.0.1（DNS rebinding），
# 发过来的 Host 也是那个域名，会被挡在门外。
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 即使通过 Host 校验，这几个路径也永远不给（仓库里可能藏着远端地址或令牌）
BLOCKED_NAMES = {".git", ".ssh", ".gnupg", ".env", ".netrc", ".npmrc",
                 ".pypirc", ".htpasswd", "id_rsa", "id_ed25519"}


def _path_blocked(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    for part in parts:
        low = part.lower()
        if low in BLOCKED_NAMES or low.startswith(".env"):
            return True
    return False


def _content_type(path: Path) -> str:
    ctype = mimetypes.guess_type(str(path))[0]
    if ctype is None:
        ext = path.suffix.lower()
        ctype = {".mp4": "video/mp4", ".webm": "video/webm",
                 ".m4v": "video/mp4", ".mov": "video/quicktime"}.get(
                     ext, "application/octet-stream")
    if ctype.startswith("text/") or ctype in ("application/javascript",):
        ctype += "; charset=utf-8"
    return ctype


def make_handler(html_path: Path, media_dir: Path, fallback_dir: Path):
    """
    生成给预览窗用的请求处理器。

    除了返回页面和素材，还兼作一条反向通道：
        预览页里的 JS 用 POST /api/event 上报"改了哪个字""卡片拖到哪了"，
        主程序用 GET /api/events?since=N 增量取走，避免跨进程直接操作控件。
    """
    events = []                       # [(seq, event), ...]
    counter = {"seq": 0}
    lock = threading.Lock()
    MAX_EVENTS = 400                  # 主程序没在跑时也不无限堆积

    class Handler(BaseHTTPRequestHandler):
        server_version = "PPBPreview/1.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass                                  # 静音，避免刷屏

        # ---- 访问控制 ----
        def _host_name(self) -> str:
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return ""
            if host.startswith("["):              # [::1]:8080
                return host.split("]")[0].lstrip("[")
            return host.rsplit(":", 1)[0] if ":" in host else host

        def _allowed_origin(self) -> bool:
            """跨站来的请求一律不认：预览页自己不会带 Origin。"""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = urlparse(origin).hostname or ""
            return host.lower() in ALLOWED_HOSTS

        def _guard(self) -> bool:
            """Host / Origin 双重校验；不通过时直接回 403。"""
            if self._host_name() not in ALLOWED_HOSTS or not self._allowed_origin():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return False
            return True

        # ---- 基础响应 ----
        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._guard():
                return
            parsed = urlparse(self.path)
            route = parsed.path

            # 1. 版本号：页面靠它判断要不要刷新
            if route == "/_rev":
                try:
                    rev = str(html_path.stat().st_mtime_ns)
                except OSError:
                    rev = "0"
                self._send(200, rev.encode("ascii"), "text/plain; charset=utf-8")
                return

            # 2. 预览页本体
            if route in ("/", "/index.html"):
                try:
                    body = html_path.read_bytes()
                except OSError:
                    body = "<h1>预览尚未生成</h1>".encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return

            # 3. 增量拉取编辑事件（主程序轮询这个接口）
            if route == "/api/events":
                try:
                    since = int(parse_qs(parsed.query).get("since", ["0"])[0] or 0)
                except ValueError:
                    since = 0
                with lock:
                    seq = counter["seq"]
                    pending = [dict(ev) for s, ev in events if s > since]
                body = json.dumps({"seq": seq, "events": pending},
                                  ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return

            # 4. 素材文件（未同步到仓库的原图 / 原视频也能显示）
            if route.startswith("/media/"):
                rel = route[len("/media/"):]
                if _path_blocked(rel):
                    self._send(403, b"forbidden", "text/plain; charset=utf-8")
                    return
                target = (media_dir / unquote(rel)).resolve()
                if self._inside(target, media_dir):
                    self._send_file(target)
                else:
                    self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return

            # 5. 其他路径回退到仓库目录（resume.pdf 等）
            rel = route.lstrip("/")
            if _path_blocked(rel):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            target = (fallback_dir / unquote(rel)).resolve()
            if self._inside(target, fallback_dir):
                self._send_file(target)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        # ---- 预览页 -> 主程序的事件通道 ----
        def do_POST(self):
            if not self._guard():
                return
            route = urlparse(self.path).path
            if route != "/api/event":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 64 * 1024:
                self._send(400, b"bad length", "text/plain; charset=utf-8")
                return
            raw = self.rfile.read(length)          # 必须读完，否则连接状态错乱
            try:
                event = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, b"bad json", "text/plain; charset=utf-8")
                return
            if not isinstance(event, dict):
                self._send(400, b"bad event", "text/plain; charset=utf-8")
                return

            with lock:
                counter["seq"] += 1
                events.append((counter["seq"], event))
                if len(events) > MAX_EVENTS:
                    del events[:len(events) - MAX_EVENTS]
                seq = counter["seq"]
            body = json.dumps({"ok": True, "seq": seq}).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")

        @staticmethod
        def _inside(child: Path, parent: Path) -> bool:
            try:
                child.relative_to(parent.resolve())
                return True
            except ValueError:
                return False

        def _send_file(self, path: Path):
            if not path.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            size = path.stat().st_size
            ctype = _content_type(path)

            range_header = self.headers.get("Range", "")
            if range_header.startswith("bytes=") and size > 0:
                self._send_range(path, size, ctype, range_header[6:])
                return

            try:
                body = path.read_bytes()
            except OSError:
                self._send(500, b"read error", "text/plain; charset=utf-8")
                return
            self._send(200, body, ctype)

        def _send_range(self, path: Path, size: int, ctype: str, spec: str):
            """支持单段 Range，视频拖动进度条需要 206 响应。"""
            try:
                start_s, _, end_s = spec.partition("-")
                if start_s.strip() == "":
                    start = max(0, size - int(end_s))
                    end = size - 1
                else:
                    start = int(start_s)
                    end = int(end_s) if end_s.strip() else size - 1
            except ValueError:
                self._send(416, b"bad range", "text/plain; charset=utf-8")
                return
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            length = end - start + 1
            try:
                with open(str(path), "rb") as fh:
                    fh.seek(start)
                    chunk = fh.read(length)
            except OSError:
                self._send(500, b"read error", "text/plain; charset=utf-8")
                return
            self._send(206, chunk, ctype, {
                "Content-Range": "bytes {}-{}/{}".format(start, end, size),
                "Accept-Ranges": "bytes",
            })

    return Handler


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def serve(port: int, html_path: Path, media_dir: Path, fallback_dir: Path,
          block: bool = True):
    """启动 HTTP 服务，返回 server 实例。"""
    handler = make_handler(html_path, media_dir, fallback_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Personal Page Builder 预览窗")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--html", required=True, help="预览 HTML 文件路径")
    parser.add_argument("--media", required=True, help="素材目录")
    parser.add_argument("--fallback", default="", help="仓库目录（相对链接回退）")
    parser.add_argument("--title", default="实时预览")
    parser.add_argument("--x", type=int, default=-1)
    parser.add_argument("--y", type=int, default=-1)
    parser.add_argument("--w", type=int, default=560)
    parser.add_argument("--h", type=int, default=860)
    parser.add_argument("--ontop", action="store_true", help="窗口置顶（空间不够时用）")
    parser.add_argument("--headless", action="store_true", help="只起服务，不开窗（调试用）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    html_path = Path(args.html).resolve()
    media_dir = Path(args.media).resolve()
    fallback_dir = Path(args.fallback).resolve() if args.fallback else media_dir

    if args.headless:
        serve(args.port, html_path, media_dir, fallback_dir)
        return 0

    try:
        import webview
    except ImportError:
        sys.stderr.write("缺少 pywebview，请执行：python -m pip install pywebview\n")
        return 2

    httpd = serve(args.port, html_path, media_dir, fallback_dir, block=False)
    port = httpd.server_address[1]

    kwargs = {"width": args.w, "height": args.h, "resizable": True,
              "text_select": True, "background_color": "#ffffff",
              "on_top": bool(args.ontop)}
    if args.x >= 0 and args.y >= 0:
        kwargs.update({"x": args.x, "y": args.y})
    window = webview.create_window(args.title, "http://127.0.0.1:{}/".format(port),
                                   **kwargs)
    try:
        webview.start()
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
