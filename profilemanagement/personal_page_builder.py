# -*- coding: utf-8 -*-
"""
Personal Page Builder
=====================

可视化维护 GitHub Pages 个人主页的桌面工具。

功能：
1. 左侧素材库：扫描本地素材目录里的图片 / 视频，生成缩略图（视频用 ffmpeg 抽首帧）
2. 点击或拖拽素材 -> 右侧生成一张 HTML 卡片
3. 卡片标题、描述可直接在右侧编辑
4. 点击【同步到 index.html】：
   a. 把素材复制进仓库 assets/images、assets/videos
   b. 用生成的 HTML 替换 index.html 中 PORTFOLIO_START / PORTFOLIO_END 之间的内容
   c. 依次执行 git add / commit / push

运行环境：Python 3.8+（需自带 tkinter）、Pillow；视频缩略图可选 ffmpeg。
运行方式：py -3.8 personal_page_builder.py
"""

import hashlib
import html
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:  # Pillow 缺失时降级为纯文字缩略图
    HAS_PIL = False


# ==========================================================================
# 一、配置区
# ==========================================================================

BASE_DIR = Path(__file__).resolve().parent

SOURCE_DIR = Path(r"E:\Documents\code")                             # 素材目录
REPO_DIR = Path(r"E:\Documents\GitHub\Orange-jesse.github.io")      # 本地仓库目录
INDEX_FILE = REPO_DIR / "index.html"

IMG_REL = "assets/images"      # 图片在仓库中的相对目录
VID_REL = "assets/videos"      # 视频在仓库中的相对目录

START_MARKER = "<!-- PORTFOLIO_START -->"
END_MARKER = "<!-- PORTFOLIO_END -->"

STATE_FILE = BASE_DIR / ".page_builder_state.json"    # 卡片文案的本地存档
CACHE_DIR = BASE_DIR / ".page_builder_cache"          # 视频首帧缓存

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

THUMB_W, THUMB_H = 168, 116          # 缩略图尺寸，左侧与右侧共用（共享缓存）
CONFIRM_BEFORE_PUSH = True           # 推送前是否弹确认框（会推送到公开仓库）

# 卡片排版方案：(内部标识, 下拉菜单显示名)
LAYOUTS = [("grid", "网格布局 Grid"),
           ("slider", "横向滑动 Slider"),
           ("list", "列表布局 List")]
DEFAULT_LAYOUT = "grid"

# 实时预览
PREVIEW_SCRIPT = BASE_DIR / "ppb_preview.py"          # 预览窗子进程
PREVIEW_HTML = CACHE_DIR / "preview" / "index.html"   # 预览用 HTML
PREVIEW_DEBOUNCE_MS = 300                             # 编辑标题时的刷新防抖
PREVIEW_MIN_W = 380                                   # 预览窗最小可用宽度
PREVIEW_DEFAULT_W = 560                               # 预览窗理想宽度

# 素材目录监控
WATCH_DEBOUNCE = 0.7                 # 连续文件事件合并窗口（秒）

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0     # 隐藏子进程的黑框

# 界面配色（浅色）
C_BG = "#f6f7f9"
C_PANEL = "#ffffff"
C_BORDER = "#e1e4e8"
C_TEXT = "#24292f"
C_MUTED = "#6a737d"
C_ACCENT = "#0366d6"
C_ACCENT_DARK = "#0256b8"
C_DANGER = "#d73a49"
C_STRIP = "#eef1f4"

F_UI = ("Microsoft YaHei UI", 10)
F_UI_BOLD = ("Microsoft YaHei UI", 10, "bold")
F_TITLE = ("Microsoft YaHei UI", 14, "bold")
F_SMALL = ("Microsoft YaHei UI", 9)
F_MONO = ("Consolas", 9)


# ==========================================================================
# 二、数据模型
# ==========================================================================

@dataclass
class Card:
    """一张作品卡片。source 为素材原文件，sync 时复制进仓库。"""
    source: str          # 素材绝对路径
    kind: str            # "image" / "video"
    title: str
    description: str = ""

    @property
    def path(self) -> Path:
        return Path(self.source)

    def to_dict(self) -> dict:
        return {"source": self.source, "kind": self.kind,
                "title": self.title, "description": self.description}


def format_title(filename: str) -> str:
    """把文件名转成可读标题：my_test-photo.png -> My Test Photo"""
    name = Path(filename).stem.replace("_", " ").replace("-", " ")
    return name.title()


def media_relpath(card: Card) -> str:
    """卡片素材在仓库里的引用路径（空格、中文等需 URL 编码，否则浏览器取不到）"""
    folder = IMG_REL if card.kind == "image" else VID_REL
    return "{}/{}".format(folder, quote(card.path.name, safe=""))


def video_mime(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return "video/{}".format("mp4" if ext in ("mp4", "m4v") else ext)


# ==========================================================================
# 三、素材扫描与缩略图
# ==========================================================================

def scan_assets(folder: Path) -> List[Path]:
    """扫描素材目录顶层的图片与视频，按修改时间倒序。"""
    if not folder.exists():
        return []
    files = []
    for p in folder.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def kind_of(path: Path) -> str:
    return "video" if path.suffix.lower() in VIDEO_EXTS else "image"


def _ffmpeg_bin() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in (r"E:\ffmpeg-8.1.2-essentials_build\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe",):
        if Path(cand).exists():
            return cand
    return None


def extract_video_frame(path: Path) -> Optional[Path]:
    """用 ffmpeg 抽取视频首帧存为 jpg，带 mtime 缓存。失败返回 None。"""
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        return None
    try:
        key = hashlib.md5(
            "{}|{}".format(path, int(path.stat().st_mtime)).encode("utf-8")
        ).hexdigest()[:16]
    except OSError:
        return None
    out = CACHE_DIR / "frames" / (key + ".jpg")
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [ffmpeg, "-y", "-ss", seek, "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=320:-2", str(out)],
                capture_output=True, timeout=20, creationflags=NO_WINDOW,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if out.exists() and out.stat().st_size > 0:
            return out
    return None


def make_thumbnail(path: Path, kind: str) -> Optional["Image.Image"]:
    """生成统一尺寸的缩略图（居中留白），失败返回 None。"""
    if not HAS_PIL:
        return None
    target = (THUMB_W, THUMB_H)
    try:
        src_path = extract_video_frame(path) if kind == "video" else path
        if src_path is None:
            return None
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            im.thumbnail((target[0] - 8, target[1] - 8), Image.LANCZOS)
            canvas = Image.new("RGB", target, (238, 241, 244))
            canvas.paste(im, ((target[0] - im.width) // 2,
                              (target[1] - im.height) // 2))
            return canvas
    except Exception:
        return None


# ==========================================================================
# 四、HTML 生成 与 index.html 替换（核心逻辑）
# ==========================================================================

GRID_STYLE = ("display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));"
              "gap:20px;margin:20px 0;")
LIST_STYLE = "display:flex;flex-direction:column;gap:16px;margin:20px 0;"
SLIDER_STYLE = ("display:flex;gap:20px;overflow-x:auto;scroll-snap-type:x mandatory;"
                "margin:20px 0;padding-bottom:14px;")
CONTAINER_STYLES = {"grid": GRID_STYLE, "list": LIST_STYLE, "slider": SLIDER_STYLE}

CARD_STYLE = ("border:1px solid #e1e4e8;border-radius:8px;padding:15px;"
              "background:#fff;box-shadow:0 2px 5px rgba(0,0,0,0.05);")
H3_STYLE = "margin-top:0;color:#0366d6;font-size:18px;"
MEDIA_STYLE = "max-width:100%;border-radius:6px;display:block;margin-bottom:10px;"
MEDIA_STYLE_FLAT = "width:100%;border-radius:6px;display:block;"
P_STYLE = "color:#666;font-size:14px;margin-bottom:0;"


def _media_markup(card: Card, src: str, title: str, style: str) -> List[str]:
    """图片 / 视频标签，列表布局与网格布局只是 style 不同。"""
    if card.kind == "video":
        return [
            '        <video controls autoplay loop muted playsinline style="{}">'.format(style),
            '            <source src="{}" type="{}">'.format(src, video_mime(card.path)),
            '        </video>',
        ]
    return ['        <img src="{}" alt="{}" loading="lazy" style="{}">'.format(src, title, style)]


def build_portfolio_html(cards: List[Card], layout: str = "grid", media_src=None) -> str:
    """
    把卡片列表渲染成一段 HTML（内联样式，与现有页面风格保持一致）。

    layout:
        grid   —— 自适应网格，默认
        slider —— 横向滑动，一屏一行，可左右拖动
        list   —— 单列列表，左图右文
    media_src:
        素材地址的取法。同步到仓库时用默认的 assets/ 相对路径；
        预览时传 lambda c: "/media/xxx"，指向本地预览服务。
    """
    if not cards:
        return '    <p style="color:#666;">还没有添加作品，敬请期待。</p>'

    layout = layout if layout in CONTAINER_STYLES else "grid"
    src_of = media_src or media_relpath
    out = ['<div style="{}">'.format(CONTAINER_STYLES[layout])]

    for card in cards:
        title = html.escape(card.title.strip() or format_title(card.path.name))
        desc = html.escape(card.description.strip())
        src = src_of(card)

        if layout == "list":
            # 左图右文，适合条目式展示
            out.append('    <div style="{}display:flex;gap:16px;align-items:flex-start;">'
                       .format(CARD_STYLE))
            out.append('        <div style="flex:0 0 260px;max-width:260px;">')
            for line in _media_markup(card, src, title, MEDIA_STYLE_FLAT):
                out.append('    ' + line)
            out.append('        </div>')
            out.append('        <div style="flex:1;min-width:0;">')
            out.append('            <h3 style="{}">{}</h3>'.format(H3_STYLE, title))
            if desc:
                out.append('            <p style="{}">{}</p>'.format(P_STYLE, desc))
            out.append('        </div>')
            out.append('    </div>')
            continue

        # grid / slider：纵向卡片，slider 额外固定宽度并吸附
        card_style = CARD_STYLE
        if layout == "slider":
            card_style += "flex:0 0 320px;scroll-snap-align:start;"
        out.append('    <div style="{}">'.format(card_style))
        out.append('        <h3 style="{}">{}</h3>'.format(H3_STYLE, title))
        out.extend(_media_markup(card, src, title, MEDIA_STYLE))
        if desc:
            out.append('        <p style="{}">{}</p>'.format(P_STYLE, desc))
        out.append('    </div>')

    out.append('</div>')
    return "\n".join(out)


def extract_block(content: str) -> str:
    """取出两个标记之间的现有内容；标记缺失时返回空串。"""
    i = content.find(START_MARKER)
    j = content.find(END_MARKER)
    if i == -1 or j == -1 or j < i:
        return ""
    return content[i + len(START_MARKER):j]


def replace_block(content: str, new_block: str) -> str:
    """
    用 new_block 替换两个标记之间的内容，标记本身保留。
    用字符串定位而非正则，避免标记里的特殊字符与贪婪匹配问题。
    """
    i = content.find(START_MARKER)
    j = content.find(END_MARKER)
    if i == -1:
        raise ValueError("index.html 中找不到标记 {}".format(START_MARKER))
    if j == -1:
        raise ValueError("index.html 中找不到标记 {}".format(END_MARKER))
    if j < i:
        raise ValueError("END 标记出现在 START 标记之前，请检查 index.html")
    head = content[:i + len(START_MARKER)]
    tail = content[j:]
    return "{}\n{}\n{}".format(head, new_block, tail)


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免写入中断损坏 index.html。"""
    tmp = path.with_name(path.name + ".tmp")
    # 用 open 而非 Path.write_text：后者 3.10 才支持 newline 参数
    with open(str(tmp), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(str(tmp), str(path))


def import_cards_from_html(content: str, source_dir: Path) -> List[Card]:
    """
    从已有的 index.html 片段反向还原卡片（仅在首次启动、无本地存档时使用），
    避免一键同步把页面上已有的作品清空。
    """
    block = extract_block(content)
    if not block.strip():
        return []

    by_name = {}
    for p in scan_assets(source_dir):
        by_name[p.name] = p

    cards = []
    segments = re.split(r'(?=<div style="border: ?1px solid)', block)
    for seg in segments:
        m_src = re.search(r'<(?:img|source)[^>]*src="([^"]+)"', seg)
        if not m_src:
            continue
        name = unquote(m_src.group(1).split("/")[-1])
        if name not in by_name:
            continue          # 本地找不到原素材，跳过
        m_title = re.search(r"<h3[^>]*>(.*?)</h3>", seg, re.S)
        m_desc = re.search(r"<p[^>]*>(.*?)</p>", seg, re.S)
        cards.append(Card(
            source=str(by_name[name]),
            kind=kind_of(by_name[name]),
            title=html.unescape(m_title.group(1)).strip() if m_title else "",
            description=html.unescape(m_desc.group(1)).strip() if m_desc else "",
        ))
    return cards


# ==========================================================================
# 五、本地状态存档
# ==========================================================================

def load_state() -> Optional[dict]:
    """返回 {"cards": [...], "layout": "grid"}；文件不存在或损坏返回 None。"""
    if not STATE_FILE.exists():
        return None
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cards = []
    for item in raw.get("cards", []):
        src = item.get("source", "")
        if not src:
            continue
        cards.append(Card(source=src, kind=item.get("kind", "image"),
                          title=item.get("title", ""),
                          description=item.get("description", "")))
    layout = raw.get("layout", DEFAULT_LAYOUT)
    if layout not in CONTAINER_STYLES:
        layout = DEFAULT_LAYOUT
    return {"cards": cards, "layout": layout}


def save_state(cards: List[Card], layout: str = DEFAULT_LAYOUT) -> None:
    data = {"version": 2, "layout": layout, "cards": [c.to_dict() for c in cards]}
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ==========================================================================
# 六、同步：复制素材 + 替换 index.html + git
# ==========================================================================

def copy_media(cards: List[Card], log) -> None:
    """把卡片素材复制进仓库，同名同大小则跳过。"""
    img_dir = REPO_DIR / IMG_REL
    vid_dir = REPO_DIR / VID_REL
    img_dir.mkdir(parents=True, exist_ok=True)
    vid_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for card in cards:
        src = card.path
        if not src.exists():
            log("!! 素材已丢失，跳过：{}".format(src.name))
            continue
        dst = (vid_dir if card.kind == "video" else img_dir) / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(str(src), str(dst))
        copied += 1
    log("素材复制完成，新增/更新 {} 个文件。".format(copied))


def run_git(args: List[str], timeout: int = 120):
    """执行 git 命令，返回 (返回码, 输出)。禁用交互式凭据输入，防止卡死。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(REPO_DIR), capture_output=True,
            encoding="utf-8", errors="replace", env=env,
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "未找到 git 命令，请确认已安装 Git 并加入 PATH。"
    except subprocess.TimeoutExpired:
        return 124, "git {} 超时（{} 秒）。".format(" ".join(args), timeout)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def git_sync(cards: List[Card], log) -> None:
    """git add -> commit -> push，无改动时跳过提交。"""
    code, out = run_git(["status", "--porcelain"])
    if code != 0:
        raise RuntimeError("git status 失败：{}".format(out))
    if not out.strip():
        log("仓库没有变化，跳过提交。")
        return

    code, out = run_git(["add", "-A"])
    if code != 0:
        raise RuntimeError("git add 失败：{}".format(out))
    log("git add 完成")

    msg = "auto: 更新作品集展示（{} 个卡片）".format(len(cards))
    code, out = run_git(["commit", "-m", msg])
    if code != 0:
        raise RuntimeError("git commit 失败：{}".format(out))
    log("git commit 完成")

    code, out = run_git(["push", "origin", "HEAD"], timeout=180)
    if code != 0:
        raise RuntimeError("git push 失败：{}".format(out))
    log("git push 完成")


def do_sync(cards: List[Card], log, layout: str = DEFAULT_LAYOUT) -> None:
    """同步主流程，运行在后台线程。"""
    if not REPO_DIR.exists():
        raise RuntimeError("仓库目录不存在：{}".format(REPO_DIR))
    if not INDEX_FILE.exists():
        raise RuntimeError("找不到 index.html：{}".format(INDEX_FILE))

    content = INDEX_FILE.read_text(encoding="utf-8")
    if START_MARKER not in content or END_MARKER not in content:
        raise RuntimeError("index.html 缺少 PORTFOLIO_START / PORTFOLIO_END 标记")

    log("开始同步，共 {} 张卡片".format(len(cards)))
    copy_media(cards, log)

    block = build_portfolio_html(cards, layout)
    atomic_write(INDEX_FILE, replace_block(content, block))
    log("index.html 已更新（{} 字节）".format(len(block)))

    git_sync(cards, log)


# ==========================================================================
# 七、实时预览（pywebview 子进程）与素材目录监控（watchdog）
# ==========================================================================

# 注入预览页的轮询脚本：文件一变就刷新，同时记住滚动位置避免"跳回顶部"
PREVIEW_BOOTSTRAP = """
<script>
(function () {
  var rev = null;
  function poll() {
    fetch('/_rev', { cache: 'no-store' })
      .then(function (r) { return r.text(); })
      .then(function (t) {
        if (rev === null) { rev = t; }
        else if (t !== rev) { location.reload(); }
      })
      .catch(function () {});
  }
  poll();
  setInterval(poll, 450);
  window.addEventListener('beforeunload', function () {
    try { sessionStorage.setItem('ppb_scroll', String(window.scrollY)); } catch (e) {}
  });
  window.addEventListener('load', function () {
    try {
      var y = sessionStorage.getItem('ppb_scroll');
      if (y) { window.scrollTo(0, parseInt(y, 10)); }
    } catch (e) {}
  });
})();
</script>
"""


def build_preview_document(index_content: str, cards: List[Card], layout: str) -> str:
    """
    以真实 index.html 为骨架，替换卡片区，并把素材地址指向本地预览服务，
    这样刚拖进来、还没同步到仓库的文件也能立刻看到。
    """
    def media_src(card: Card) -> str:
        return "/media/" + quote(card.path.name, safe="")

    block = build_portfolio_html(cards, layout, media_src)
    if index_content and START_MARKER in index_content and END_MARKER in index_content:
        doc = replace_block(index_content, block)
    else:
        doc = ('<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8">'
               '<title>预览</title></head><body style="font-family:sans-serif;'
               'max-width:800px;margin:0 auto;padding:2rem;">\n<h2>项目经历</h2>\n'
               + START_MARKER + "\n" + block + "\n" + END_MARKER
               + "\n</body></html>")

    if "</body>" in doc:
        doc = doc.replace("</body>", PREVIEW_BOOTSTRAP + "</body>", 1)
    else:
        doc += PREVIEW_BOOTSTRAP
    return doc


def free_port() -> int:
    """让系统分配一个空闲端口，避免多开时撞端口。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class PreviewController:
    """
    统一管理预览子进程。

    主进程只做两件事：把 HTML 写到 CACHE_DIR/preview/index.html、按需起停子进程。
    刷新不靠 IPC —— 预览页里的 JS 自己轮询文件版本号，改完 0.5 秒内自动重载。
    """

    def __init__(self, app):
        self.app = app
        self.proc = None
        self.port = None
        self._last_content = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def write(self, content: str) -> bool:
        """内容有变化才写盘，避免每敲一个字都触发整页重载。"""
        if content == self._last_content:
            return False
        PREVIEW_HTML.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(PREVIEW_HTML, content)
        self._last_content = content
        return True

    def start(self) -> bool:
        if self.is_running():
            return True
        if not PREVIEW_SCRIPT.exists():
            self.app.push_log("!! 找不到预览模块：{}".format(PREVIEW_SCRIPT))
            return False
        PREVIEW_HTML.parent.mkdir(parents=True, exist_ok=True)
        if not PREVIEW_HTML.exists():
            atomic_write(PREVIEW_HTML,
                         "<html><body style='font-family:sans-serif'>"
                         "<p>预览生成中…</p></body></html>")
            self._last_content = None

        self.port = free_port()
        x, y, w, h, on_top = self._geometry()
        cmd = [sys.executable, str(PREVIEW_SCRIPT),
               "--port", str(self.port),
               "--html", str(PREVIEW_HTML),
               "--media", str(SOURCE_DIR),
               "--fallback", str(REPO_DIR),
               "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
        if on_top:
            cmd.append("--ontop")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), creationflags=NO_WINDOW,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            self.app.push_log("!! 预览进程启动失败：{}".format(exc))
            return False

        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self):
        """把子进程输出转发到主界面日志，方便排查。"""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.app.push_log("[预览] " + line)

    def _geometry(self):
        """
        预览窗停靠逻辑：
            右侧空间够（>=380px）-> 贴主窗右侧，宽度按剩余空间伸缩
            右侧空间不够        -> 叠在主窗上并置顶，用户可以拖到第二块屏幕
        """
        app = self.app
        screen_w = app.winfo_screenwidth()
        screen_h = app.winfo_screenheight()
        mx, my = app.winfo_rootx(), app.winfo_rooty()
        mw, mh = app.winfo_width(), app.winfo_height()

        available = screen_w - (mx + mw) - 32
        height = min(mh, screen_h - my - 100)

        if available >= PREVIEW_MIN_W:
            width = min(PREVIEW_DEFAULT_W, available)
            x = min(mx + mw + 12, screen_w - width - 12)
            y = my
            on_top = False
        else:
            width = min(PREVIEW_DEFAULT_W, max(PREVIEW_MIN_W, screen_w - 80))
            x = max(20, screen_w - width - 20)
            y = min(my + 40, max(20, screen_h - 600))
            height = min(mh, screen_h - y - 90)
            on_top = True
        return int(x), int(y), int(width), int(height), on_top

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass


class AssetWatcher:
    """
    用 watchdog 监控素材目录。

    注意两点：
        1. 只关心素材目录这一层（recursive=False），缓存子目录不会触发
        2. 系统复制大文件会连发多个事件，这里用 Timer 合并成一次刷新
    """

    def __init__(self, folder: Path, on_change, delay: float = WATCH_DEBOUNCE):
        self.folder = str(Path(folder))
        self.on_change = on_change
        self.delay = delay
        self._observer = None
        self._timer = None
        self._lock = threading.Lock()

    def matches(self, path: str) -> bool:
        candidate = Path(path)
        if str(candidate.parent) != self.folder:
            return False
        ext = candidate.suffix.lower()
        return ext in IMAGE_EXTS or ext in VIDEO_EXTS

    def start(self) -> bool:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            return False

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if getattr(event, "is_directory", False):
                    return
                target = getattr(event, "dest_path", "") or event.src_path
                if watcher.matches(target):
                    watcher.trigger()

        self._observer = Observer()
        self._observer.daemon = True
        self._observer.schedule(_Handler(), self.folder, recursive=False)
        self._observer.start()
        return True

    def trigger(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        try:
            self.on_change()
        except Exception:
            pass

    def stop(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None


# ==========================================================================
# 八、通用 UI 组件
# ==========================================================================

class ScrollFrame(tk.Frame):
    """带竖向滚动条的容器，内容放进 self.body。"""

    def __init__(self, master, bg=C_PANEL, **kw):
        tk.Frame.__init__(self, master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def scroll(self, delta: int):
        self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")

    def to_bottom(self):
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()


def is_descendant(widget, ancestor) -> bool:
    """判断 widget 是否位于 ancestor 内部（用于拖拽落点判定）。"""
    node = widget
    while node is not None:
        if node is ancestor:
            return True
        node = getattr(node, "master", None)
    return False


# ==========================================================================
# 九、左侧：素材项 与 素材库
# ==========================================================================

class AssetItem(tk.Frame):
    """素材库中的单个缩略图（支持点击 / 拖拽）。"""

    def __init__(self, master, app, path: Path, photo):
        tk.Frame.__init__(self, master, bg=C_PANEL, bd=0,
                          highlightthickness=1, highlightbackground=C_PANEL)
        self.app = app
        self.path = path
        self.kind = kind_of(path)

        self._name = path.name if len(path.name) <= 18 else path.name[:15] + "…"
        label = "{}{}".format("▶ " if self.kind == "video" else "", self._name)

        if photo is not None:
            self.thumb = tk.Label(self, image=photo, bg=C_STRIP, bd=0)
        else:
            self.thumb = tk.Label(self, text="视频\n无预览" if self.kind == "video" else "无法预览",
                                  bg=C_STRIP, fg=C_MUTED, font=F_SMALL,
                                  width=22, height=7)
        self.thumb.pack(padx=3, pady=(3, 2))

        self.caption = tk.Label(self, text=label, bg=C_PANEL, fg=C_TEXT,
                                font=F_SMALL, anchor="w", justify="left")
        self.caption.pack(fill="x", padx=5, pady=(0, 4))

        for w in (self, self.thumb, self.caption):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.configure(cursor="hand2")

    def _paint(self, color):
        self.configure(bg=color, highlightbackground=C_ACCENT if color == C_STRIP else C_PANEL)
        self.caption.configure(bg=color)

    def _on_enter(self, _e=None):
        self._paint("#f2f7ff")

    def _on_leave(self, _e=None):
        self._paint(C_PANEL)

    def _on_press(self, event):
        self.app.begin_drag(self, event.x_root, event.y_root)

    def _on_motion(self, event):
        self.app.update_drag(event.x_root, event.y_root)

    def _on_release(self, event):
        self.app.end_drag(event.x_root, event.y_root)


class AssetPanel(tk.Frame):
    """左侧素材库面板。"""

    def __init__(self, master, app):
        tk.Frame.__init__(self, master, bg=C_PANEL)
        self.app = app
        self._photos = {}       # 保住 PhotoImage 引用，防止被回收

        header = tk.Frame(self, bg=C_PANEL)
        header.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(header, text="素材库", bg=C_PANEL, fg=C_TEXT,
                 font=F_UI_BOLD).pack(side="left")
        self.count_label = tk.Label(header, text="", bg=C_PANEL, fg=C_MUTED, font=F_SMALL)
        self.count_label.pack(side="left", padx=8)

        tk.Label(self, text=str(SOURCE_DIR), bg=C_PANEL, fg=C_MUTED,
                 font=F_SMALL, anchor="w", wraplength=300).pack(fill="x", padx=12)

        self.scroll = ScrollFrame(self, bg=C_PANEL)
        self.scroll.pack(fill="both", expand=True, padx=(8, 0), pady=8)

        tk.Button(self, text="刷新素材", command=app.refresh_assets,
                  bg="#ffffff", fg=C_TEXT, font=F_UI, relief="solid", bd=1,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=10, pady=4).pack(fill="x", padx=12, pady=(0, 12))

    def populate(self, files: List[Path]):
        self.scroll.clear()
        self._photos.clear()
        self.count_label.configure(text="{} 个".format(len(files)))

        if not files:
            tk.Label(self.scroll.body, text="素材目录中没有图片或视频",
                     bg=C_PANEL, fg=C_MUTED, font=F_SMALL).pack(pady=24)
            return

        cols = 2
        for idx, path in enumerate(files):
            photo = self.app.get_photo(path)
            item = AssetItem(self.scroll.body, self.app, path, photo)
            item.grid(row=idx // cols, column=idx % cols, padx=4, pady=4, sticky="n")
            if photo is not None:
                self._photos[str(path)] = photo   # 引用托管
        self.scroll.body.grid_columnconfigure(0, weight=1)
        self.scroll.body.grid_columnconfigure(1, weight=1)


# ==========================================================================
# 十、右侧：卡片编辑器 与 编辑区
# ==========================================================================

class CardEditor(tk.Frame):
    """单张卡片的编辑控件：缩略图 + 标题 + 描述 + 移除。"""

    def __init__(self, master, app, card: Card, index: int):
        tk.Frame.__init__(self, master, bg=C_PANEL, highlightthickness=1,
                          highlightbackground=C_BORDER)
        self.app = app
        self.card = card
        self.grid_columnconfigure(1, weight=1)

        photo = app.get_photo(card.path)
        if photo is not None:
            thumb = tk.Label(self, image=photo, bg=C_STRIP)
            app.keep_ref(photo)
        else:
            thumb = tk.Label(self, text="无预览", bg=C_STRIP, fg=C_MUTED,
                             font=F_SMALL, width=22, height=7)
        thumb.grid(row=0, column=0, rowspan=3, padx=(10, 10), pady=10, sticky="n")

        meta = tk.Frame(self, bg=C_PANEL)
        meta.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(10, 2))
        tag = "视频" if card.kind == "video" else "图片"
        tk.Label(meta, text="卡片 {}  ·  {}".format(index, tag), bg=C_PANEL,
                 fg=C_MUTED, font=F_SMALL).pack(side="left")
        tk.Label(meta, text=card.path.name, bg=C_PANEL, fg=C_MUTED,
                 font=F_SMALL).pack(side="right")

        self.title_var = tk.StringVar(value=card.title)
        self.title_entry = tk.Entry(
            self, textvariable=self.title_var, font=F_UI, bg=C_PANEL, fg=C_TEXT,
            relief="solid", bd=1, insertbackground=C_TEXT,
        )
        self.title_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=(0, 6))
        self.title_var.trace_add("write", self._on_title_change)
        self.title_entry.bind("<FocusIn>",
                              lambda e: self.title_entry.configure(highlightthickness=0))

        self.desc_text = tk.Text(
            self, height=3, wrap="word", font=F_UI, bg=C_PANEL, fg=C_TEXT,
            relief="solid", bd=1, insertbackground=C_TEXT,
        )
        self.desc_text.insert("1.0", card.description)
        self.desc_text.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 4))
        self.desc_text.bind("<KeyRelease>", self._on_desc_change)

        footer = tk.Frame(self, bg=C_PANEL)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
        tk.Button(footer, text="移除", command=lambda: app.remove_card(card),
                  bg=C_PANEL, fg=C_DANGER, font=F_SMALL, relief="flat", bd=0,
                  activeforeground=C_DANGER, activebackground=C_STRIP,
                  cursor="hand2", padx=6).pack(side="right")

    def _on_title_change(self, *_args):
        self.card.title = self.title_var.get()
        self.app.schedule_save()

    def _on_desc_change(self, _event=None):
        self.card.description = self.desc_text.get("1.0", "end-1c").strip()
        self.app.schedule_save()

    def focus_title(self):
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")


class EditorPanel(tk.Frame):
    """右侧卡片编辑区。"""

    def __init__(self, master, app):
        tk.Frame.__init__(self, master, bg=C_BG)
        self.app = app
        self._photos = []

        header = tk.Frame(self, bg=C_BG)
        header.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(header, text="作品卡片", bg=C_BG, fg=C_TEXT, font=F_UI_BOLD).pack(side="left")
        self.hint = tk.Label(header, text="点击左侧素材即可添加，拖拽也可以",
                             bg=C_BG, fg=C_MUTED, font=F_SMALL)
        self.hint.pack(side="left", padx=10)

        self.scroll = ScrollFrame(self, bg=C_BG)
        self.scroll.pack(fill="both", expand=True, padx=(10, 4), pady=(4, 8))
        self.scroll.body.configure(bg=C_BG)

    def render(self, cards: List[Card]):
        self.scroll.clear()
        self._photos.clear()

        if not cards:
            box = tk.Frame(self.scroll.body, bg=C_BG)
            box.pack(fill="both", expand=True, pady=60)
            tk.Label(box, text="还没有卡片", bg=C_BG, fg=C_TEXT, font=F_UI_BOLD).pack()
            tk.Label(box, text="从左侧点击或拖拽图片 / 视频到这里",
                     bg=C_BG, fg=C_MUTED, font=F_SMALL).pack(pady=4)
            self.hint.configure(text="")
            return

        for idx, card in enumerate(cards, start=1):
            editor = CardEditor(self.scroll.body, self.app, card, idx)
            editor.pack(fill="x", padx=4, pady=5)
        self.hint.configure(text="共 {} 张卡片".format(len(cards)))


# ==========================================================================
# 十一、主窗口
# ==========================================================================

class App(tk.Tk):

    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Personal Page Builder")
        # 按屏幕可用尺寸收敛窗口大小，避免在小屏上超出可视区域
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1240, max(960, screen_w - 60))
        win_h = min(820, max(640, screen_h - 110))
        self.geometry("{}x{}".format(win_w, win_h))
        self.minsize(900, 620)
        self.configure(bg=C_BG)

        self.cards: List[Card] = []
        self.asset_files: List[Path] = []
        self.layout = DEFAULT_LAYOUT
        self._photo_cache = {}          # Path -> PhotoImage
        self._refs = []                 # 额外引用池
        self._drag = None
        self._save_job = None
        self._queue = queue.Queue()
        self._syncing = False
        self._editors: List[CardEditor] = []

        self.preview = PreviewController(self)   # 预览子进程
        self.watcher = None                      # 素材目录监控
        self._preview_job = None                 # 预览刷新防抖
        self._preview_enabled = True             # 用户是否希望开预览
        self._saved_size = None                  # 为预览收窄前的窗口尺寸

        self._build_ui()
        self._load_initial_cards()      # 可能改写 self.layout
        self._sync_layout_widget()
        self.refresh_assets()
        self.render_cards()
        self.bind_all("<MouseWheel>", self._on_mousewheel)
        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(300, self._start_watcher)
        self.after(700, self._autostart_preview)

    # ---------------- 界面骨架 ----------------

    def _build_ui(self):
        # 顶部标题栏
        top = tk.Frame(self, bg=C_PANEL, highlightthickness=1,
                       highlightbackground=C_BORDER)
        top.pack(fill="x")
        left = tk.Frame(top, bg=C_PANEL)
        left.pack(side="left", padx=16, pady=10)
        tk.Label(left, text="Personal Page Builder", bg=C_PANEL,
                 fg=C_TEXT, font=F_TITLE).pack(anchor="w")
        tk.Label(left, text="可视化管理 GitHub Pages 个人主页",
                 bg=C_PANEL, fg=C_MUTED, font=F_SMALL).pack(anchor="w")

        right = tk.Frame(top, bg=C_PANEL)
        right.pack(side="right", padx=16)

        tk.Button(right, text="打开仓库目录", command=self.open_repo,
                  bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=10, pady=3).pack(side="right")

        self.preview_btn = tk.Button(
            right, text="预览：关", command=self.toggle_preview,
            bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
            activebackground=C_STRIP, cursor="hand2", padx=10, pady=3)
        self.preview_btn.pack(side="right", padx=(0, 8))

        layout_box = tk.Frame(right, bg=C_PANEL)
        layout_box.pack(side="right", padx=(0, 14))
        tk.Label(layout_box, text="排版", bg=C_PANEL, fg=C_MUTED,
                 font=F_SMALL).pack(side="left", padx=(0, 6))
        self.layout_combo = ttk.Combobox(
            layout_box, values=[name for _key, name in LAYOUTS],
            state="readonly", width=16, font=F_SMALL)
        self.layout_combo.set(LAYOUTS[0][1])
        self.layout_combo.pack(side="left")
        self.layout_combo.bind("<<ComboboxSelected>>", self._on_layout_selected)

        # 中间左右分栏
        paned = tk.PanedWindow(self, orient="horizontal", bg=C_BORDER,
                               sashwidth=5, sashrelief="flat", bd=0)
        paned.pack(fill="both", expand=True, padx=10, pady=10)

        left_wrap = tk.Frame(paned, bg=C_PANEL, highlightthickness=1,
                             highlightbackground=C_BORDER)
        self.asset_panel = AssetPanel(left_wrap, self)
        self.asset_panel.pack(fill="both", expand=True)
        paned.add(left_wrap, width=380, minsize=300, stretch="never")

        right_wrap = tk.Frame(paned, bg=C_BG, highlightthickness=1,
                              highlightbackground=C_BORDER)
        self.editor_panel = EditorPanel(right_wrap, self)
        self.editor_panel.pack(fill="both", expand=True)
        paned.add(right_wrap, minsize=460, stretch="always")

        # 底部：日志 + 同步按钮
        bottom = tk.Frame(self, bg=C_PANEL, highlightthickness=1,
                          highlightbackground=C_BORDER)
        bottom.pack(fill="x", side="bottom")

        self.log_text = tk.Text(bottom, height=5, font=F_MONO, bg="#fbfbfb",
                                fg=C_TEXT, relief="flat", bd=0, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True, padx=12, pady=(8, 10))
        self.log_text.configure(state="disabled")

        action = tk.Frame(bottom, bg=C_PANEL)
        action.pack(side="right", padx=12, pady=10)
        self.sync_btn = tk.Button(
            action, text="同步到 index.html", command=self.on_sync_click,
            bg=C_ACCENT, fg="#ffffff", font=("Microsoft YaHei UI", 11, "bold"),
            activebackground=C_ACCENT_DARK, activeforeground="#ffffff",
            relief="flat", bd=0, cursor="hand2", padx=22, pady=10,
        )
        self.sync_btn.pack()
        self.status_label = tk.Label(action, text="", bg=C_PANEL, fg=C_MUTED,
                                     font=F_SMALL)
        self.status_label.pack(pady=(6, 0))

        self.log("准备就绪。素材目录：{}".format(SOURCE_DIR))
        self.log("目标仓库：{}".format(REPO_DIR))

    # ---------------- 数据加载 ----------------

    def _load_initial_cards(self):
        state = load_state()
        if state is not None:
            self.cards = state["cards"]
            self.layout = state["layout"]
            self.log("已从本地存档恢复 {} 张卡片，排版：{}。".format(
                len(self.cards), self.layout))
            return

        # 首次运行：从 index.html 反向导入，避免覆盖已有内容
        if INDEX_FILE.exists():
            try:
                content = INDEX_FILE.read_text(encoding="utf-8")
            except OSError:
                content = ""
            imported = import_cards_from_html(content, SOURCE_DIR) if content else []
            if imported:
                self.cards = imported
                self.log("首次运行：已从 index.html 导入 {} 张现有卡片。".format(len(imported)))
                save_state(self.cards, self.layout)
                return
            if extract_block(content).strip():
                messagebox.showwarning(
                    "提示",
                    "index.html 中已有作品内容，但无法自动识别成可编辑卡片。\n"
                    "点击同步会用当前卡片列表覆盖该区域，请确认后再操作。",
                )
        self.log("首次运行：卡片列表为空，点击左侧素材开始添加。")

    def refresh_assets(self):
        self.asset_files = scan_assets(SOURCE_DIR)
        self.asset_panel.populate(self.asset_files)
        self.log("素材扫描完成，共 {} 个文件。".format(len(self.asset_files)))

    # ---------------- 缩略图缓存 ----------------

    def get_photo(self, path: Path):
        key = str(path)
        if key in self._photo_cache:
            return self._photo_cache[key]
        image = make_thumbnail(path, kind_of(path))
        if image is None:
            return None
        photo = ImageTk.PhotoImage(image)
        self._photo_cache[key] = photo
        return photo

    def keep_ref(self, photo):
        self._refs.append(photo)

    # ---------------- 卡片操作 ----------------

    def render_cards(self):
        self.editor_panel.render(self.cards)
        self._sync_status()

    def add_card(self, path: Path):
        for card in self.cards:
            if card.source == str(path):
                self.set_status("该素材已经在卡片列表里了")
                return
        card = Card(source=str(path), kind=kind_of(path),
                    title=format_title(path.name), description="")
        self.cards.append(card)
        self.render_cards()
        self.editor_panel.scroll.to_bottom()
        self.set_status("已添加：{}".format(path.name))
        self.schedule_save()
        self.touch_preview(immediate=True)

    def remove_card(self, card: Card):
        self.cards = [c for c in self.cards if c is not card]
        self.render_cards()
        self.set_status("已移除：{}".format(card.path.name))
        self.schedule_save()
        self.touch_preview(immediate=True)

    # ---------------- 拖拽状态机 ----------------

    def begin_drag(self, item: AssetItem, x_root: int, y_root: int):
        self._drag = {"item": item, "x": x_root, "y": y_root, "moved": False}

    def update_drag(self, x_root: int, y_root: int):
        if not self._drag:
            return
        dx = abs(x_root - self._drag["x"])
        dy = abs(y_root - self._drag["y"])
        if dx > 4 or dy > 4:
            self._drag["moved"] = True

    def end_drag(self, x_root: int, y_root: int):
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        item = drag["item"]
        if not drag["moved"]:
            self.add_card(item.path)        # 单击即添加
            return
        target = self.winfo_containing(x_root, y_root)
        if target is not None and is_descendant(target, self.editor_panel):
            self.add_card(item.path)
        else:
            self.set_status("拖拽取消：请放到右侧编辑区")

    # ---------------- 状态存档（防抖） ----------------

    def schedule_save(self):
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self._save_now)
        self.touch_preview()             # 文案改动 -> 预览防抖刷新

    def _save_now(self):
        self._save_job = None
        try:
            save_state(self.cards, self.layout)
        except OSError as exc:
            self.log("!! 保存本地存档失败：{}".format(exc))
        self._sync_status()

    def _sync_status(self):
        n = len(self.cards)
        text = "{} 张卡片".format(n)
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)

    def set_status(self, text: str):
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)

    # ---------------- 实时预览 ----------------

    def current_index_content(self) -> str:
        """每次都读最新文件，这样你在外部改 index.html 预览也会跟上。"""
        try:
            return INDEX_FILE.read_text(encoding="utf-8")
        except OSError:
            return ""

    def touch_preview(self, immediate: bool = False):
        """
        卡片增删或文案变化后重写预览 HTML。
        打字场景走防抖（300ms），增删卡片走立即刷新。
        """
        if not self.preview.is_running():
            return
        if immediate:
            if self._preview_job is not None:
                self.after_cancel(self._preview_job)
                self._preview_job = None
            self._refresh_preview_now()
            return
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(PREVIEW_DEBOUNCE_MS, self._refresh_preview_now)

    def _refresh_preview_now(self):
        self._preview_job = None
        if not self.preview.is_running():
            return
        doc = build_preview_document(self.current_index_content(),
                                     self.cards, self.layout)
        if self.preview.write(doc):
            self.set_status("预览已刷新")

    def start_preview(self, quiet: bool = False):
        """先写一份最新 HTML，再拉起预览子进程。"""
        if self.preview.is_running():
            return
        self._make_room_for_preview()
        doc = build_preview_document(self.current_index_content(),
                                     self.cards, self.layout)
        self.preview.write(doc)
        if self.preview.start():
            self._preview_enabled = True
            if not quiet:
                self.log("实时预览已打开（端口 {}）。".format(self.preview.port))
        else:
            self._preview_enabled = False
            if self._saved_size is not None:      # 启动失败就把窗口尺寸还原
                self.geometry("{}x{}".format(*self._saved_size))
                self._saved_size = None
            self.log("!! 实时预览启动失败：请确认已安装 pywebview（py -3.8 -m pip install pywebview）。")
        self._update_preview_btn()

    def _make_room_for_preview(self):
        """
        单屏时分栏放不下：把主窗收窄一点给预览腾位置，关闭预览时再还原。
        收窄也没用（主窗已被用户挪到屏幕右半边）就保持原样，预览改为叠放置顶。
        """
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        mx, mw = self.winfo_rootx(), self.winfo_width()
        if screen_w - (mx + mw) - 32 >= PREVIEW_MIN_W:
            return
        target = int(max(900, screen_w - PREVIEW_DEFAULT_W - 40))
        if target >= mw:
            return
        if self._saved_size is None:
            self._saved_size = (mw, self.winfo_height())
        self.geometry("{}x{}".format(target, self.winfo_height()))
        self.update_idletasks()
        self.log("已收窄主窗口，为预览腾出位置。")

    def stop_preview(self):
        self.preview.stop()
        if self._saved_size is not None:
            self.geometry("{}x{}".format(*self._saved_size))
            self._saved_size = None
        self._preview_enabled = False
        self._update_preview_btn()
        self.log("实时预览已关闭。")

    def toggle_preview(self):
        if self.preview.is_running():
            self.stop_preview()
        else:
            self.start_preview()

    def _autostart_preview(self):
        if self._preview_enabled and not self.preview.is_running():
            self.start_preview()

    def _update_preview_btn(self):
        running = self.preview.is_running()
        self.preview_btn.configure(
            text="预览：开" if running else "预览：关",
            bg="#e8f2fe" if running else "#ffffff",
            fg=C_ACCENT if running else C_TEXT)

    # ---------------- 排版切换 ----------------

    def _on_layout_selected(self, _event=None):
        label = self.layout_combo.get()
        key = next((k for k, name in LAYOUTS if name == label), None)
        if key is None or key == self.layout:
            return
        self.layout = key
        self.schedule_save()
        self._refresh_preview_now()          # 排版切换立即生效，不等防抖
        self.log("排版已切换为：{}".format(label))
        self.set_status("排版：{}".format(key))

    def _sync_layout_widget(self):
        for key, name in LAYOUTS:
            if key == self.layout:
                self.layout_combo.set(name)
                return

    # ---------------- 素材目录监控 ----------------

    def _start_watcher(self):
        watcher = AssetWatcher(SOURCE_DIR,
                               lambda: self._queue.put(("refresh", None)))
        if watcher.start():
            self.watcher = watcher
            self.log("已监控素材目录：往文件夹里丢新文件，左侧会自动刷新。")
        else:
            self.log("!! 未能开启目录监控（缺少 watchdog），可点「刷新素材」手动更新。")

    # ---------------- 关闭 ----------------

    def on_close(self):
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self.preview.stop()
        self.destroy()

    # ---------------- 日志 ----------------

    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def push_log(self, message: str):
        """供后台线程 / 子进程调用：丢进队列，由主线程真正写控件。"""
        self._queue.put(("log", message))

    # ---------------- 滚轮路由 ----------------

    def _on_mousewheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        node = widget
        while node is not None:
            if isinstance(node, ScrollFrame):
                if isinstance(widget, tk.Text):
                    return          # 文本区自己滚
                node.scroll(event.delta)
                return
            node = getattr(node, "master", None)

    # ---------------- 同步 ----------------

    def on_sync_click(self):
        if self._syncing:
            return
        if not self.cards:
            if not messagebox.askyesno("确认", "当前没有任何卡片，同步会清空页面上的作品区，继续吗？"):
                return
        elif CONFIRM_BEFORE_PUSH:
            ok = messagebox.askyesno(
                "确认同步",
                "即将：\n"
                "1. 复制 {} 个素材到仓库\n"
                "2. 覆盖 index.html 中 PORTFOLIO 标记之间的内容\n"
                "3. 提交并推送到 GitHub（公开可见）\n\n"
                "确认继续？".format(len(self.cards)),
            )
            if not ok:
                return

        self._syncing = True
        self.sync_btn.configure(state="disabled", text="同步中…", bg="#8fb8e8")
        self.set_status("正在同步…")
        snapshot = list(self.cards)          # 传副本给线程
        layout = self.layout
        thread = threading.Thread(target=self._sync_worker,
                                  args=(snapshot, layout), daemon=True)
        thread.start()

    def _sync_worker(self, cards: List[Card], layout: str):
        """后台线程：只做 I/O，通过队列把消息交给主线程。"""
        def log(msg):
            self._queue.put(("log", msg))
        try:
            do_sync(cards, log, layout)
            self._queue.put(("done", "同步完成，1~2 分钟后可刷新网页查看。"))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "refresh":
                    self.refresh_assets()
                    self.log("检测到素材目录变化，已自动刷新素材库。")
                elif kind == "done":
                    self.log(payload)
                    self._finish_sync(payload, success=True)
                elif kind == "error":
                    self.log("同步失败：{}".format(payload))
                    messagebox.showerror("同步失败", payload)
                    self._finish_sync("同步失败", success=False)
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _finish_sync(self, message: str, success: bool):
        self._syncing = False
        self.sync_btn.configure(state="normal", text="同步到 index.html", bg=C_ACCENT)
        self.set_status(message)

    def open_repo(self):
        if not REPO_DIR.exists():
            messagebox.showerror("错误", "仓库目录不存在：{}".format(REPO_DIR))
            return
        if IS_WINDOWS:
            os.startfile(str(REPO_DIR))
        else:
            self.log("请手动打开：{}".format(REPO_DIR))


def main():
    if not HAS_PIL:
        print("[提示] 未安装 Pillow，缩略图将降级为文字占位。")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
