# -*- coding: utf-8 -*-
"""
Personal Page Builder
=====================

可视化维护 GitHub Pages 个人主页的桌面工具。任何人都可以用它编辑自己的主页：
首次启动会让你指定「素材目录」和「本地仓库目录」，之后所有路径都来自那份配置，
程序本身不含任何个人目录、账号或密钥。

功能：
1. 左侧素材库：扫描素材目录里的图片 / 视频，生成缩略图（视频用 ffmpeg 抽首帧）
2. 点击或拖拽素材 -> 右侧生成一张 HTML 卡片
3. 卡片标题、描述可直接在右侧编辑，也能在预览窗里直接改字、拖动排序
4. 点击【同步到页面】：
   a. 把素材复制进仓库 assets/images、assets/videos
      （图片按「图片压缩」设置重新编码成 WebP / JPEG，体积大幅下降，访客加载更快）
   b. 用生成的 HTML 替换页面中 PORTFOLIO_START / PORTFOLIO_END 之间的内容
   c. 依次执行 git add / commit / push

运行环境：Python 3.8+（需自带 tkinter）、Pillow、pywebview、watchdog；
视频缩略图需要 ffmpeg（可选）。
运行方式：Windows 双击 run.bat；其他系统 `python3 personal_page_builder.py`

配置与数据都放在用户目录 ~/.personal_page_builder 里（可用环境变量 PPB_DATA_DIR 改位置），
不会写进程序目录，也不会进你的 git 仓库。
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
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
    HAS_PIL = True
except Exception:  # Pillow 缺失时降级为纯文字缩略图
    HAS_PIL = False


# ==========================================================================
# 一、配置区
# ==========================================================================

BASE_DIR = Path(__file__).resolve().parent

# 配置、存档、缓存统一放用户目录，程序文件夹可以只读、也可以直接打包分发
DATA_DIR = Path(os.environ.get("PPB_DATA_DIR") or (Path.home() / ".personal_page_builder"))
CONFIG_FILE = DATA_DIR / "config.json"
SETTINGS_FILE = DATA_DIR / "settings.json"      # 压缩等偏好，跨仓库共用
STATE_FILE = DATA_DIR / "state.json"            # 运行时按仓库改写（每个仓库一份）
BACKUP_DIR = DATA_DIR / "backup"                # 改页面文案前的备份（按仓库分开）
CACHE_DIR = DATA_DIR / "cache"                  # 视频首帧 / 压缩结果 / 预览页缓存

# 下面这些路径在 apply_config() 里按用户配置重新赋值，函数运行时读到的都是最新值
SOURCE_DIR = Path.home()                 # 素材目录
REPO_DIR = Path.home()                   # 本地仓库目录
INDEX_FILE = REPO_DIR / "index.html"     # 要替换的作品区所在文件
GIT_REMOTE = "origin"
GIT_BRANCH = ""

# 用户配置：全部由首次启动的引导界面填写，代码里不预置任何个人路径
DEFAULT_CONFIG = {
    "source_dir": "",      # 素材目录（图片、视频所在文件夹）
    "repo_dir": "",        # 本地仓库目录（git clone 下来的 GitHub Pages 仓库）
    "index_file": "index.html",   # 相对仓库根目录的页面文件
    "remote": "origin",    # 推送到哪个远端
    "branch": "",          # 留空 = 推送到当前分支的同名远端分支
    "ffmpeg_path": "",     # 可选，留空则用 PATH 里的 ffmpeg
}
CONFIG = dict(DEFAULT_CONFIG)

IMG_REL = "assets/images"      # 图片在仓库中的相对目录
VID_REL = "assets/videos"      # 视频在仓库中的相对目录

START_MARKER = "<!-- PORTFOLIO_START -->"
END_MARKER = "<!-- PORTFOLIO_END -->"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

THUMB_W, THUMB_H = 168, 116          # 左侧素材库缩略图尺寸
ROW_THUMB = (132, 92)                # 右侧卡片行缩略图尺寸
CONFIRM_BEFORE_PUSH = True           # 推送前是否弹确认框（会推送到公开仓库）

# 图片压缩：同步到仓库时把图片重新编码，直接决定访客打开页面的速度。
# 本地素材原图不动，仓库里只放压缩后的版本。
DEFAULT_SETTINGS = {
    "compress": True,      # 是否在同步时压缩图片
    "format": "webp",      # webp（体积最小）/ jpeg（兼容性最好）
    "max_width": 1600,     # 长边上限（像素），0 表示不缩放、只压体积
    "quality": 82,         # 有损压缩质量，60~95
}
COMPRESS_FORMATS = [("webp", "WebP（体积最小，推荐）"),
                    ("jpeg", "JPEG（兼容性最好）")]
COMPRESS_WIDTHS = [(1280, "1280 px（手机浏览）"),
                   (1600, "1600 px（推荐）"),
                   (1920, "1920 px（大屏）"),
                   (2560, "2560 px（高分屏）"),
                   (0, "保持原尺寸，只压体积")]

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
# 只有 Windows 的 Popen 认 creationflags，其他平台必须干脆不传这个参数
SPAWN_KW = {"creationflags": NO_WINDOW} if IS_WINDOWS else {}

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


# --------------------------------------------------------------------------
# 配置的读写与生效
# --------------------------------------------------------------------------

def normalize_config(raw: Optional[dict]) -> dict:
    """收敛外部输入，避免路径里混进引号、反斜杠、空串把后续流程搞崩。"""
    raw = raw if isinstance(raw, dict) else {}
    cfg = dict(DEFAULT_CONFIG)

    for key in ("source_dir", "repo_dir"):
        value = raw.get(key)
        cfg[key] = str(Path(value).expanduser()) if isinstance(value, str) and value.strip() else ""

    index_file = str(raw.get("index_file") or "").strip().replace("\\", "/").lstrip("/")
    cfg["index_file"] = index_file or "index.html"
    cfg["remote"] = str(raw.get("remote") or "").strip() or "origin"
    cfg["branch"] = str(raw.get("branch") or "").strip()
    cfg["ffmpeg_path"] = str(raw.get("ffmpeg_path") or "").strip()
    return cfg


def load_config() -> dict:
    raw = {}
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    return normalize_config(raw)


def harden_permissions() -> None:
    """
    把数据目录收紧成「只有本人可读写」。

    POSIX 上 chmod 700/600；Windows 的用户目录本身默认只有本人和管理员能进，
    再调 icacls 反而容易把权限改坏，所以不在那里动手。
    """
    if IS_WINDOWS:
        return
    try:
        os.chmod(str(DATA_DIR), 0o700)
    except OSError:
        pass
    for path in (CONFIG_FILE, SETTINGS_FILE):
        try:
            if path.exists():
                os.chmod(str(path), 0o600)
        except OSError:
            pass


def cleanup_temp_files(root: Path, older_than: float = 60.0) -> int:
    """
    清掉上次被强制结束（任务管理器 / kill -9）时留下的 *.tmp。
    只删超过 1 分钟的，避免误删正在写入的那一个。
    """
    now = time.time()
    removed = 0
    try:
        candidates = list(root.rglob("*.tmp"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if now - path.stat().st_mtime < older_than:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def save_config(cfg: dict) -> dict:
    """写入 config.json。内容没变化时不动文件（避免重复写、改动时间）。"""
    cfg = normalize_config(cfg)
    try:
        write_json(CONFIG_FILE, cfg)
    except OSError as exc:
        raise RuntimeError("保存配置失败：{}".format(exc))
    harden_permissions()
    return cfg


def repo_state_dir(repo_dir: Path) -> Path:
    """每个仓库一份存档和备份，换仓库不会串数据。"""
    key = hashlib.md5(str(repo_dir).lower().encode("utf-8")).hexdigest()[:10]
    return DATA_DIR / "repos" / key


def apply_config(cfg: dict) -> dict:
    """把配置落到模块级路径上；所有函数运行时读到的都是这里写的最新值。"""
    global CONFIG, SOURCE_DIR, REPO_DIR, INDEX_FILE, STATE_FILE, BACKUP_DIR
    global GIT_REMOTE, GIT_BRANCH
    cfg = normalize_config(cfg)
    CONFIG = cfg

    SOURCE_DIR = Path(cfg["source_dir"]) if cfg["source_dir"] else Path.home()
    REPO_DIR = Path(cfg["repo_dir"]) if cfg["repo_dir"] else Path.home()
    INDEX_FILE = REPO_DIR / cfg["index_file"]
    GIT_REMOTE = cfg["remote"] or "origin"
    GIT_BRANCH = cfg["branch"]

    state_dir = repo_state_dir(REPO_DIR)
    STATE_FILE = state_dir / "state.json"
    BACKUP_DIR = state_dir / "backup"
    for folder in (DATA_DIR, state_dir, BACKUP_DIR, CACHE_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    harden_permissions()
    cleanup_temp_files(DATA_DIR)
    return cfg


def config_problems(cfg: dict) -> List[str]:
    """返回「现在还不能开工」的原因；空列表表示配置可用。"""
    cfg = normalize_config(cfg)
    problems = []
    if not cfg["repo_dir"]:
        problems.append("还没有指定本地仓库目录")
    else:
        repo = Path(cfg["repo_dir"])
        if not repo.is_dir():
            problems.append("仓库目录不存在：{}".format(repo))
        elif not (repo / cfg["index_file"]).is_file():
            problems.append("仓库里找不到页面文件：{}".format(cfg["index_file"]))
    if not cfg["source_dir"]:
        problems.append("还没有指定素材目录")
    elif not Path(cfg["source_dir"]).is_dir():
        problems.append("素材目录不存在：{}".format(cfg["source_dir"]))
    return problems


def index_has_markers(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return START_MARKER in content and END_MARKER in content


def insert_markers(path: Path, repo_dir: Optional[Path] = None) -> bool:
    """
    给页面补上 PORTFOLIO 标记（放在 </body> 前），原文件先备份一份。

    备份写到数据目录（repos/<指纹>/backup/），不往仓库里塞 .bak ——
    否则同步时的 git add -A 会把它一起提交到公开仓库。
    已经有两处标记时什么都不做。
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if START_MARKER in content and END_MARKER in content:
        return True
    if START_MARKER in content or END_MARKER in content:
        return False                    # 只坏了一半，交给用户手动处理更安全
    try:
        backup_dir = repo_state_dir(repo_dir or path.parent) / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(backup_dir / (path.name + ".bak")))
    except OSError:
        pass
    block = (START_MARKER
             + '\n    <p style="color:#666;">还没有添加作品，敬请期待。</p>\n'
             + END_MARKER + "\n")
    pos = content.lower().rfind("</body>")
    new_content = (content[:pos] + block + content[pos:]) if pos != -1 else (content + "\n" + block)
    atomic_write(path, new_content)
    return True


def open_in_file_manager(path: Path) -> bool:
    """在系统文件管理器里打开目录，跨平台。"""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False



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


@dataclass
class Section:
    """
    一个展示栏目（页面上的一"栏"）。每个栏目有自己的标题、排版和卡片列表。
    标题留空则页面上不输出标题——首页那一栏就复用 index.html 里已有的「项目经历」大标题。
    """
    title: str = ""
    layout: str = DEFAULT_LAYOUT
    cards: List[Card] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "layout": self.layout,
                "cards": [c.to_dict() for c in self.cards]}

    @classmethod
    def from_dict(cls, raw: dict) -> "Section":
        layout = raw.get("layout", DEFAULT_LAYOUT)
        if layout not in CONTAINER_STYLES:
            layout = DEFAULT_LAYOUT
        cards = []
        for item in raw.get("cards", []):
            src = item.get("source", "")
            if not src:
                continue
            cards.append(Card(source=src, kind=item.get("kind", "image"),
                              title=item.get("title", ""),
                              description=item.get("description", "")))
        return cls(title=raw.get("title", ""), layout=layout, cards=cards)


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
    """优先用设置里指定的 ffmpeg，其次找 PATH；都没有就跳过视频缩略图。"""
    configured = CONFIG.get("ffmpeg_path") or ""
    if configured and Path(configured).exists():
        return configured
    return shutil.which("ffmpeg")


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
                capture_output=True, timeout=20, **SPAWN_KW,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if out.exists() and out.stat().st_size > 0:
            return out
    return None


def make_thumbnail(path: Path, kind: str, size=None) -> Optional["Image.Image"]:
    """生成统一尺寸的缩略图（居中留白），失败返回 None。"""
    if not HAS_PIL:
        return None
    target = size or (THUMB_W, THUMB_H)
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
# 栏目标题：比卡片标题大一号，用深色与蓝色卡片标题区分层级
SECTION_TITLE_STYLE = "margin:34px 0 12px;color:#24292f;font-size:20px;"
SECTION_NOTE_STYLE = "color:#8a9199;font-size:13px;margin:2px 0 14px;"
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
    return ['        <img src="{}" alt="{}" loading="lazy" decoding="async" style="{}">'.format(
        src, title, style)]


def _attrs(editable: bool, **kwargs) -> str:
    """
    可编辑模式下给元素打上定位属性，预览页的 JS 靠它知道"这个字属于第几栏第几张卡"。
    editable=False（同步到仓库）时返回空串，导出的 HTML 里不会出现这些痕迹。
    """
    if not editable:
        return ""
    return "".join(' data-ppb-{}="{}"'.format(key.replace("_", "-"), value)
                   for key, value in kwargs.items())


def _render_cards_block(items, layout: str, src_of,
                        editable: bool = False, sec_index: int = 0) -> List[str]:
    """
    把一个栏目里的卡片渲染成「一个容器 + N 张卡片」。

    items 是 [(在 section.cards 里的真实下标, Card), ...] ——
    带上真实下标，预览页回传修改时才能准确落到某一张卡上。
    """
    layout = layout if layout in CONTAINER_STYLES else DEFAULT_LAYOUT
    # 空容器不能用 CSS :empty 判断 —— 字符串拼接会留下换行文本节点，
    # 这里直接打一个类，拖动后由 JS 增删
    empty_class = ' class="ppb-drop-empty"' if editable and not items else ''
    out = ['<div style="{}"{}{}>'.format(
        CONTAINER_STYLES[layout], _attrs(editable, cards=sec_index), empty_class)]

    for card_index, card in items:
        title = html.escape(card.title.strip() or format_title(card.path.name))
        desc = html.escape(card.description.strip())
        src = src_of(card)
        mark = _attrs(editable, sec=sec_index, card=card_index)

        if layout == "list":
            # 左图右文，适合条目式展示
            out.append('    <div style="{}display:flex;gap:16px;align-items:flex-start;"{}>'
                       .format(CARD_STYLE, mark))
            out.append('        <div style="flex:0 0 260px;max-width:260px;">')
            for line in _media_markup(card, src, title, MEDIA_STYLE_FLAT):
                out.append('    ' + line)
            out.append('        </div>')
            out.append('        <div style="flex:1;min-width:0;">')
            out.append('            <h4 style="{}"{}>{}</h4>'.format(
                H3_STYLE, _attrs(editable, field="title"), title))
            if desc:
                out.append('            <p style="{}"{}>{}</p>'.format(
                    P_STYLE, _attrs(editable, field="desc"), desc))
            elif editable:
                # 描述为空时也留一个空段落，预览里点它就能补写
                out.append('            <p class="ppb-empty-desc" style="{}"{}></p>'.format(
                    P_STYLE, _attrs(editable, field="desc")))
            out.append('        </div>')
            out.append('    </div>')
            continue

        # grid / slider：纵向卡片，slider 额外固定宽度并吸附
        card_style = CARD_STYLE
        if layout == "slider":
            card_style += "flex:0 0 320px;scroll-snap-align:start;"
        out.append('    <div style="{}"{}>'.format(card_style, mark))
        out.append('        <h4 style="{}"{}>{}</h4>'.format(
            H3_STYLE, _attrs(editable, field="title"), title))
        out.extend(_media_markup(card, src, title, MEDIA_STYLE))
        if desc:
            out.append('        <p style="{}"{}>{}</p>'.format(
                P_STYLE, _attrs(editable, field="desc"), desc))
        elif editable:
            out.append('        <p class="ppb-empty-desc" style="{}"{}></p>'.format(
                P_STYLE, _attrs(editable, field="desc")))
        out.append('    </div>')

    out.append('</div>')
    return out


def build_portfolio_html(sections: List[Section], media_src=None,
                         editable: bool = False) -> str:
    """
    把多个栏目渲染成一段 HTML。

    每个栏目 = 可选标题 + 一个卡片容器；栏目的排版互相独立，
    所以同一页面上可以既有滑动栏，又有网格 / 列表。
    media_src:
        素材地址的取法。同步到仓库时用默认 assets/ 相对路径；
        预览时传 lambda c: "/media/xxx"，指向本地预览服务。
    editable:
        预览用。会给标题 / 描述 / 卡片打上 data-ppb-* 属性并保留空占位，
        方便在预览窗里直接点着改、拖着排；导出的页面始终是干净 HTML。
    """
    src_of = media_src or media_relpath
    out = []

    for sec_index, section in enumerate(sections):
        items = [(i, c) for i, c in enumerate(section.cards) if c.source]
        title = section.title.strip()

        if not items and not editable:
            continue                       # 空栏目不输出，避免页面上留个小标题
        if title:
            out.append('<h3 style="{}"{}>{}</h3>'.format(
                SECTION_TITLE_STYLE,
                _attrs(editable, sec=sec_index, field="section-title"),
                html.escape(title)))
        elif editable:
            # 预览里给一行极淡的占位，同步出去的页面上不会出现
            out.append('<h3 class="ppb-empty-title" style="{}"{}></h3>'.format(
                SECTION_TITLE_STYLE,
                _attrs(editable, sec=sec_index, field="section-title")))
        out.extend(_render_cards_block(items, section.layout, src_of,
                                       editable, sec_index))

    if not out:
        return '    <p style="color:#666;">还没有添加作品，敬请期待。</p>'
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
        raise ValueError("页面里找不到标记 {}".format(START_MARKER))
    if j == -1:
        raise ValueError("页面里找不到标记 {}".format(END_MARKER))
    if j < i:
        raise ValueError("END 标记出现在 START 标记之前，请检查 index.html")
    head = content[:i + len(START_MARKER)]
    tail = content[j:]
    return "{}\n{}\n{}".format(head, new_block, tail)


def split_index_parts(content: str):
    """拆成「标记前 / 标记区 / 标记后」，页面文案只在前后两段里改。"""
    i = content.find(START_MARKER)
    j = content.find(END_MARKER)
    if i == -1 or j == -1 or j < i:
        return content, "", ""
    return content[:i], content[i:j + len(END_MARKER)], content[j + len(END_MARKER):]


def replace_page_text(content: str, old: str, new: str) -> Optional[str]:
    """
    替换作品区之外的一处纯文本（页面大标题、个人简介、技能条目等）。

    宁可不动，也不误伤：只有原文在前后两段之一里"恰好出现一次"才替换。
    如果同一句话在页面上出现多处（比如卡片标题刚好同名），返回 None 让调用方放弃。
    """
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or old == new:
        return None

    head, block, tail = split_index_parts(content)
    replacement = html.escape(new, quote=False)

    # 原文里可能有 & < >（页面里写成 &amp; 之类），两种形式都试一次
    for pattern in (old, html.escape(old, quote=False)):
        if not pattern:
            continue
        if head.count(pattern) == 1 and tail.count(pattern) == 0:
            return head.replace(pattern, replacement, 1) + block + tail
        if tail.count(pattern) == 1 and head.count(pattern) == 0:
            return head + block + tail.replace(pattern, replacement, 1)
    return None


def read_text_safely(path: Path) -> Optional[str]:
    """读文本；文件不存在、没权限、编码坏了都返回 None，绝不抛异常。"""
    try:
        with open(str(path), "r", encoding="utf-8", errors="strict") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def atomic_write(path: Path, text: str, skip_if_same: bool = True) -> bool:
    """
    先写临时文件再整体替换，避免写入中断把原文件截断。

    skip_if_same=True 时，内容与磁盘上完全一致就直接返回 False（不写、不动修改时间），
    这样重复触发的保存不会产生任何磁盘写入 —— 配置文件、存档、预览页都用它。

    返回 True 表示这次真的写了。
    """
    if skip_if_same and read_text_safely(path) == text:
        return False

    tmp = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    try:
        # 用 open 而非 Path.write_text：后者 3.10 才支持 newline 参数
        with open(str(tmp), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())        # 落盘后再替换，断电也不会留下半截文件
        os.replace(str(tmp), str(path))  # 同盘 rename 是原子操作
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return True


def write_json(path: Path, data) -> bool:
    """统一的 JSON 落盘：原子替换 + 内容相同则跳过。返回是否真的写了。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return atomic_write(path, text)


def import_cards_from_html(content: str, source_dir: Path) -> List[Card]:
    """
    从已有的 index.html 片段反向还原卡片（仅在首次启动、无本地存档时使用），
    避免一键同步把页面上已有的作品清空。
    """
    block = extract_block(content)
    if not block.strip():
        return []

    by_name = {}
    by_stem = {}
    for p in scan_assets(source_dir):
        by_name[p.name] = p
        by_stem.setdefault(p.stem.lower(), []).append(p)

    cards = []
    segments = re.split(r'(?=<div style="border: ?1px solid)', block)
    for seg in segments:
        m_src = re.search(r'<(?:img|source)[^>]*src="([^"]+)"', seg)
        if not m_src:
            continue
        name = unquote(m_src.group(1).split("/")[-1])
        target = by_name.get(name)
        if target is None:
            # 压缩后页面引用的是 a.webp，而本地原图是 a.png —— 按主名兜底找回
            same_stem = by_stem.get(Path(name).stem.lower(), [])
            if len(same_stem) == 1:
                target = same_stem[0]
        if target is None:
            continue          # 本地找不到原素材，跳过
        m_title = re.search(r"<h[34][^>]*>(.*?)</h[34]>", seg, re.S)
        m_desc = re.search(r"<p[^>]*>(.*?)</p>", seg, re.S)
        cards.append(Card(
            source=str(target),
            kind=kind_of(target),
            title=html.unescape(m_title.group(1)).strip() if m_title else "",
            description=html.unescape(m_desc.group(1)).strip() if m_desc else "",
        ))
    return cards


# ==========================================================================
# 五、本地状态存档
# ==========================================================================

STATE_WARNINGS: List[str] = []      # load_state 的异常情况，交给界面显示


def quarantine_file(path: Path, reason: str) -> Optional[Path]:
    """
    把读不懂的文件改名留存（state.json -> state.corrupt-20261106-202530.json），
    而不是让它被下一次保存直接覆盖掉。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name("{}.corrupt-{}{}".format(path.stem, stamp, path.suffix))
    try:
        os.replace(str(path), str(target))
    except OSError:
        return None
    STATE_WARNINGS.append(
        "{} 已损坏（{}），原文件保留为 {}，本次从页面重新生成。".format(
            path.name, reason, target.name))
    return target


def load_state() -> Optional[List[Section]]:
    """
    读取本地存档，返回栏目列表；文件不存在或损坏返回 None。

    兼容旧版（v1/v2 只有一层卡片 + 全局 layout）：自动升级成一个栏目，
    标题留空以复用页面里已有的「项目经历」大标题。
    """
    if not STATE_FILE.exists():
        return None

    text = read_text_safely(STATE_FILE)
    if text is None:
        quarantine_file(STATE_FILE, "读不出来")
        return None
    if not text.strip():
        quarantine_file(STATE_FILE, "内容是空的")
        return None

    try:
        raw = json.loads(text)
    except ValueError:
        quarantine_file(STATE_FILE, "不是合法的 JSON")
        return None
    if not isinstance(raw, dict):
        quarantine_file(STATE_FILE, "结构不对")
        return None

    if "sections" in raw:                       # v3
        try:
            return [Section.from_dict(item) for item in raw.get("sections", [])]
        except (AttributeError, TypeError, ValueError):
            quarantine_file(STATE_FILE, "栏目数据不完整")
            return None

    # v1 / v2 迁移
    try:
        legacy = Section.from_dict({"title": "",
                                    "layout": raw.get("layout", DEFAULT_LAYOUT),
                                    "cards": raw.get("cards", [])})
    except (AttributeError, TypeError, ValueError):
        quarantine_file(STATE_FILE, "卡片数据不完整")
        return None
    return [legacy]


def save_state(sections: List[Section]) -> bool:
    """
    保存存档。内容与磁盘一致时不写（防止重复触发导致反复写盘）。
    返回 True 表示这次真的落盘了。
    """
    data = {"version": 3, "sections": [s.to_dict() for s in sections]}
    return write_json(STATE_FILE, data)


def count_cards(sections: List[Section]) -> int:
    return sum(len(s.cards) for s in sections)


def all_cards(sections: List[Section]) -> List[Card]:
    return [c for s in sections for c in s.cards]


# ---------------- 图片压缩选项的读写 ----------------

def normalize_settings(raw: Optional[dict]) -> dict:
    """把外部传入的选项收敛成合法值，避免脏数据让同步崩在中途。"""
    cfg = dict(DEFAULT_SETTINGS)
    raw = raw if isinstance(raw, dict) else {}
    if "compress" in raw:
        cfg["compress"] = bool(raw.get("compress"))
    fmt = str(raw.get("format", cfg["format"])).lower()
    cfg["format"] = fmt if fmt in ("webp", "jpeg") else DEFAULT_SETTINGS["format"]
    try:
        width = int(raw.get("max_width", cfg["max_width"]))
    except (TypeError, ValueError):
        width = DEFAULT_SETTINGS["max_width"]
    cfg["max_width"] = max(0, min(width, 8000))
    try:
        quality = int(raw.get("quality", cfg["quality"]))
    except (TypeError, ValueError):
        quality = DEFAULT_SETTINGS["quality"]
    cfg["quality"] = max(40, min(quality, 98))
    return cfg


def load_settings() -> dict:
    raw = {}
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    return normalize_settings(raw)


def save_settings(settings: dict) -> dict:
    cfg = normalize_settings(settings)
    try:
        write_json(SETTINGS_FILE, cfg)       # 内容没变就不写
        harden_permissions()
    except OSError:
        pass
    return cfg


def describe_settings(cfg: dict) -> str:
    """一句人类可读的说明，用在界面提示和日志里。"""
    if not cfg.get("compress"):
        return "关闭，同步时按原图上传（页面会更大）"
    return "{}，长边 ≤ {} px，质量 {}".format(
        str(cfg.get("format", "webp")).upper(),
        cfg.get("max_width") or "不限",
        cfg.get("quality", 82))



# ==========================================================================
# 六、图片压缩 与 同步：复制素材 + 替换 index.html + git
# ==========================================================================

def human_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return "{:.1f} MB".format(num_bytes / 1048576.0)
    if num_bytes >= 1024:
        return "{:.0f} KB".format(num_bytes / 1024.0)
    return "{} B".format(int(num_bytes))


def compressed_name(name: str, fmt: str) -> str:
    """压缩件文件名：只换扩展名，方便和原图对照。"""
    stem = Path(name).stem or "image"
    return "{}.{}".format(stem, "webp" if fmt == "webp" else "jpg")


def _optim_cache_path(src: Path, cfg: dict) -> Path:
    """缓存文件名和「源文件 + 参数」绑定，源图或参数一变就重新压。"""
    try:
        st = src.stat()
        stamp = "{}:{}".format(int(st.st_mtime), st.st_size)
    except OSError:
        stamp = "0:0"
    raw = "{}|{}|{}|{}|{}".format(src, stamp, cfg["format"],
                                  cfg["max_width"], cfg["quality"])
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    ext = ".webp" if cfg["format"] == "webp" else ".jpg"
    return CACHE_DIR / "optimized" / (key + ext)


def optimize_image(src: Path, cfg: dict) -> Optional[Path]:
    """
    按当前选项把一张图片压成 WebP / JPEG，结果缓存到 .page_builder_cache/optimized。

    返回压缩后的文件路径；以下情况返回 None，由调用方回退成原图：
    Pillow 缺失、多帧动图（压成静图会丢帧）、文件读不出来、编码失败。
    压缩结果不保证更小（原图也许已经很省），体积比较交给调用方判断。
    """
    if not HAS_PIL:
        return None
    cfg = normalize_settings(cfg)
    out = _optim_cache_path(src, cfg)
    if out.exists() and out.stat().st_size > 0:
        return out

    max_side = int(cfg["max_width"] or 0)
    quality = int(cfg["quality"])
    try:
        with Image.open(str(src)) as raw:
            if getattr(raw, "is_animated", False) and getattr(raw, "n_frames", 1) > 1:
                return None                 # 动图压成静图会丢帧
            has_alpha = (raw.mode in ("RGBA", "LA", "PA")
                         or (raw.mode == "P" and "transparency" in raw.info))
            im = ImageOps.exif_transpose(raw)   # 手机照片的方向信息落成真实像素
        if max_side > 0 and max(im.size) > max_side:
            scale = max_side / float(max(im.size))
            im = im.resize((max(1, int(round(im.width * scale))),
                            max(1, int(round(im.height * scale)))), Image.LANCZOS)
        out.parent.mkdir(parents=True, exist_ok=True)
        if cfg["format"] == "webp":
            im.convert("RGBA" if has_alpha else "RGB").save(
                str(out), "WEBP", quality=quality, method=4)
        else:
            if has_alpha:                   # JPEG 没有透明通道，用白底合成
                rgba = im.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                im = canvas
            im.convert("RGB").save(str(out), "JPEG", quality=quality,
                                   optimize=True, progressive=True)
    except Exception:
        try:
            if out.exists():
                out.unlink()
        except OSError:
            pass
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def estimate_image_savings(cards: List[Card], cfg: dict) -> dict:
    """压缩试算：只生成缓存文件，不动仓库，用来在设置面板里看体积变化。"""
    cfg = normalize_settings(cfg)
    before = after = 0
    count = skipped = 0
    for card in cards:
        if card.kind != "image":
            continue
        try:
            size = card.path.stat().st_size
        except OSError:
            continue
        count += 1
        before += size
        opt = optimize_image(card.path, cfg)
        if opt is None or opt.stat().st_size >= size:
            after += size
            skipped += 1
        else:
            after += opt.stat().st_size
    return {"count": count, "before": before, "after": after,
            "skipped": skipped, "saved": before - after}


def copy_media(cards: List[Card], log, settings: Optional[dict] = None) -> dict:
    """
    把卡片素材放进仓库，返回 {素材路径: 仓库内相对 URL}。

    开启压缩时，图片只上传压缩后的版本（原图留在素材目录，不重复进仓库），
    文件名可能从 .png 变成 .webp —— 所以页面里的引用必须用这份映射，
    不能再用原来的文件名。
    """
    cfg = normalize_settings(settings)
    img_dir = REPO_DIR / IMG_REL
    vid_dir = REPO_DIR / VID_REL
    img_dir.mkdir(parents=True, exist_ok=True)
    vid_dir.mkdir(parents=True, exist_ok=True)

    mapping = {}
    copied = 0
    compressed = 0
    before = after = 0

    # 先统一分配文件名：不同原图压成同名（a.png / a.jpg -> a.webp）时自动加编号。
    # 分配只和源路径、参数有关，跟卡片顺序无关 —— 拖动排序不会让仓库文件改名。
    desired = {}
    for card in cards:
        if card.source in desired:
            continue
        if card.kind == "video" or not cfg["compress"]:
            desired[card.source] = card.path.name
        else:
            desired[card.source] = compressed_name(card.path.name, cfg["format"])

    by_base = {}
    for source, name in desired.items():
        by_base.setdefault(name.lower(), []).append(source)

    names = {}
    for sources in by_base.values():
        sources.sort()
        base = desired[sources[0]]
        stem, suffix = os.path.splitext(base)
        for index, source in enumerate(sources):
            names[source] = base if index == 0 else "{}-{}{}".format(stem, index + 1, suffix)

    def put(src_file: Path, dst: Path) -> int:
        """内容没变就不复制，避免每次同步都动一遍仓库。"""
        try:
            if (dst.exists()
                    and dst.stat().st_size == src_file.stat().st_size
                    and dst.stat().st_mtime >= src_file.stat().st_mtime):
                return 0
        except OSError:
            pass
        shutil.copy2(str(src_file), str(dst))
        return 1

    def record(card: Card, folder: str, target_dir: Path,
               name: str, src_file: Path) -> int:
        try:
            moved = put(src_file, target_dir / name)
        except OSError as exc:
            log("!! 复制失败，跳过 {}：{}".format(src_file.name, exc))
            return 0
        mapping[card.source] = "{}/{}".format(folder, quote(name, safe=""))
        return moved

    for card in cards:
        if card.source in mapping:          # 同一张素材挂了多张卡片
            continue
        src = card.path
        if not src.exists():
            log("!! 素材已丢失，跳过：{}".format(src.name))
            continue
        try:
            size = src.stat().st_size
        except OSError:
            size = 0

        if card.kind == "video" or not cfg["compress"]:
            video = card.kind == "video"
            copied += record(card, VID_REL if video else IMG_REL,
                             vid_dir if video else img_dir, names[card.source], src)
            if not video:
                before += size
                after += size
            continue

        opt = optimize_image(src, cfg)
        if opt is None or size == 0 or opt.stat().st_size >= size:
            # 压不动，或原图本来就比压缩结果更小 —— 原样上传更划算。
            # 名字沿用分配好的主名，但扩展名必须还原成原图的，否则 MIME 对不上。
            name = os.path.splitext(names[card.source])[0] + src.suffix.lower()
            copied += record(card, IMG_REL, img_dir, name, src)
            before += size
            after += size
            continue

        opt_size = opt.stat().st_size
        copied += record(card, IMG_REL, img_dir, names[card.source], opt)
        compressed += 1
        before += size
        after += opt_size
        log("  压缩 {}：{} -> {}（省 {:.0f}%）".format(
            src.name, human_size(size), human_size(opt_size),
            (1 - opt_size / float(size)) * 100))

    if cfg["compress"]:
        if before:
            log("图片体积：{} -> {}，共省 {}（{:.0f}%），{} 张压成 {}。".format(
                human_size(before), human_size(after),
                human_size(before - after), (before - after) * 100.0 / before,
                compressed, cfg["format"].upper()))
        else:
            log("没有需要压缩的图片。")
    log("素材复制完成，新增/更新 {} 个文件。".format(copied))
    return mapping



def run_git(args: List[str], timeout: int = 120):
    """执行 git 命令，返回 (返回码, 输出)。禁用交互式凭据输入，防止卡死。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(REPO_DIR), capture_output=True,
            encoding="utf-8", errors="replace", env=env,
            timeout=timeout, **SPAWN_KW,
        )
    except FileNotFoundError:
        return 127, "未找到 git 命令，请确认已安装 Git 并加入 PATH。"
    except subprocess.TimeoutExpired:
        return 124, "git {} 超时（{} 秒）。".format(" ".join(args), timeout)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def git_sync(card_count: int, log) -> None:
    """git add -> commit -> push，无改动时跳过提交。"""
    code, out = run_git(["status", "--porcelain"])
    if code != 0:
        raise RuntimeError(
            "git status 失败：{}\n请确认仓库目录是一个 git 仓库（git clone 下来的目录）。".format(out))
    if not out.strip():
        log("仓库没有变化，跳过提交。")
        return

    code, out = run_git(["add", "-A"])
    if code != 0:
        raise RuntimeError("git add 失败：{}".format(out))
    log("git add 完成")

    msg = "auto: 更新作品集展示（{} 个卡片）".format(card_count)
    code, out = run_git(["commit", "-m", msg])
    if code != 0:
        raise RuntimeError("git commit 失败：{}".format(out))
    log("git commit 完成")

    code, out = run_git(["remote"])
    remotes = out.split() if code == 0 else []
    if GIT_REMOTE not in remotes:
        raise RuntimeError(
            "仓库里没有名为 {} 的远端，现有远端：{}。\n"
            "请在「设置」里改成正确的远端名，或先执行 git remote add {} <仓库地址>。".format(
                GIT_REMOTE, "、".join(remotes) or "（无）", GIT_REMOTE))

    target = "HEAD:{}".format(GIT_BRANCH) if GIT_BRANCH else "HEAD"
    code, out = run_git(["push", GIT_REMOTE, target], timeout=180)
    if code != 0:
        raise RuntimeError("git push 失败：{}".format(out))
    log("git push 完成（{} -> {}）".format(GIT_REMOTE, GIT_BRANCH or "当前分支"))


def do_sync(sections: List[Section], log, settings: Optional[dict] = None) -> None:
    """同步主流程，运行在后台线程。"""
    if not REPO_DIR.exists():
        raise RuntimeError("仓库目录不存在：{}".format(REPO_DIR))
    if not INDEX_FILE.exists():
        raise RuntimeError("仓库里找不到页面文件：{}".format(INDEX_FILE))

    cfg = normalize_settings(settings if settings is not None else load_settings())
    content = INDEX_FILE.read_text(encoding="utf-8")
    if START_MARKER not in content or END_MARKER not in content:
        raise RuntimeError("{} 缺少 PORTFOLIO_START / PORTFOLIO_END 标记".format(
            CONFIG["index_file"]))

    cards = all_cards(sections)
    log("开始同步：{} 个栏目，{} 张卡片".format(len(sections), len(cards)))
    log("图片压缩：{}".format(describe_settings(cfg)))
    mapping = copy_media(cards, log, cfg)

    block = build_portfolio_html(
        sections,
        lambda card: mapping.get(card.source, media_relpath(card)))
    if atomic_write(INDEX_FILE, replace_block(content, block)):
        log("{} 已更新（{} 字节）".format(CONFIG["index_file"], len(block)))
    else:
        log("{} 内容与上次一致，没有改动。".format(CONFIG["index_file"]))

    git_sync(len(cards), log)


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


# 可视化编辑：只注入预览页，绝不进真实 index.html
# 1) 文字点一下就能改，失焦时把新值 POST 给主程序
# 2) 卡片按住左上角把手可以拖动，松手把新的分组顺序整份回传
PREVIEW_EDITOR = r"""
<style>
[data-ppb-field]{outline:1px dashed transparent;outline-offset:2px;border-radius:3px;
  transition:outline-color .12s,background-color .12s;}
[data-ppb-field]:hover{outline-color:#bdd7f5;background-color:rgba(3,102,214,.035);}
[data-ppb-field]:focus{outline:1px solid #0366d6;background-color:#f5faff;}
[data-ppb-page]{outline:1px dashed transparent;outline-offset:3px;border-radius:3px;
  transition:outline-color .12s,background-color .12s;}
[data-ppb-page]:hover{outline-color:#cfe0f5;background-color:rgba(3,102,214,.03);}
[data-ppb-page]:focus{outline:1px solid #0366d6;background-color:#f5faff;}
.ppb-empty-title:empty{min-height:1.5em;}
.ppb-empty-title:empty::before{content:"点击这里输入栏目标题（留空则不显示）";
  color:#d3dae2;font-size:14px;font-weight:normal;}
.ppb-empty-desc:empty{min-height:1.5em;}
.ppb-empty-desc:empty::before{content:"点击添加描述";color:#ccd3db;font-size:13px;}
.ppb-drop-empty{min-height:92px;border:1px dashed #d4dbe4;border-radius:8px;
  background:#fafbfc;}
.ppb-drop-empty::before{content:"把卡片拖到这里";display:block;width:100%;
  text-align:center;color:#b9c2cc;font-size:13px;padding:34px 0;}
.ppb-handle{position:absolute;left:-10px;top:-10px;width:22px;height:22px;line-height:20px;
  text-align:center;border-radius:50%;background:#fff;border:1px solid #c6d2de;
  color:#5b6b7c;font-size:13px;cursor:grab;display:none;z-index:6;user-select:none;
  box-shadow:0 2px 6px rgba(15,35,60,.14);}
[data-ppb-card]:hover>.ppb-handle{display:block;}
.ppb-handle:hover{background:#eef5ff;border-color:#8fb8e8;color:#0366d6;}
.ppb-dragging{opacity:.42;}
body.ppb-dragging-active,body.ppb-dragging-active *{cursor:grabbing!important;}
#ppb-tip{position:fixed;right:16px;bottom:16px;z-index:99998;max-width:380px;
  padding:10px 13px;border-radius:8px;background:rgba(23,35,49,.93);color:#fff;
  font:12px/1.75 "Microsoft YaHei UI",sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.22);
  transition:opacity .6s;}
#ppb-tip b{color:#8fc4ff;}
#ppb-tip .ppb-x{cursor:pointer;margin-left:8px;opacity:.65;}
#ppb-tip .ppb-x:hover{opacity:1;}
</style>
<script>
(function () {
  'use strict';
  var EDIT_URL = '/api/event';

  function post(event) {
    try {
      fetch(EDIT_URL, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(event)
      })['catch'](function () {});
    } catch (e) {}
  }

  // ---------- 1. 文字：单击就地修改 ----------
  function bindText() {
    var nodes = document.querySelectorAll('[data-ppb-field]');
    Array.prototype.forEach.call(nodes, function (el) {
      var before = el.textContent;
      el.setAttribute('contenteditable', 'true');
      el.setAttribute('spellcheck', 'false');

      el.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); el.blur(); }
        else if (ev.key === 'Escape') { el.textContent = before; el.blur(); }
      });

      el.addEventListener('paste', function (ev) {
        ev.preventDefault();
        var text = ((ev.clipboardData || window.clipboardData).getData('text') || '');
        document.execCommand('insertText', false, text.replace(/\s+/g, ' '));
      });

      el.addEventListener('focusout', function () {
        var value = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (value === before.trim()) {
          if (el.textContent !== value) { el.textContent = value; }
          return;
        }
        before = value;
        el.textContent = value;
        var cardEl = el.closest('[data-ppb-card]');
        var secEl = el.closest('[data-ppb-sec]');
        post({
          type: 'text',
          field: el.getAttribute('data-ppb-field'),
          sec: secEl ? parseInt(secEl.getAttribute('data-ppb-sec'), 10) : 0,
          card: cardEl ? parseInt(cardEl.getAttribute('data-ppb-card'), 10) : null,
          value: value
        });
      });
    });
  }

  // ---------- 1b. 页面文案：作品区之外的大标题、简介、技能条目 ----------
  // 只处理"纯文本、无子标签"的元素：带 <strong> 之类的段落没法靠纯文本唯一定位，
  // 强行改容易把标签结构弄乱，这类内容留给 index.html 手工编辑。
  var PAGE_TEXT_SELECTOR = 'h1,h2,h3,h4,h5,p,li,figcaption,blockquote,td,th,dd,dt';

  function bindPageText() {
    var nodes = document.querySelectorAll(PAGE_TEXT_SELECTOR);
    Array.prototype.forEach.call(nodes, function (el) {
      if (el.getAttribute('data-ppb-field')) { return; }   // 栏目 / 卡片文字已由上面接管
      if (el.closest('[data-ppb-cards]')) { return; }
      if (el.children.length > 0) { return; }
      if (el.closest('nav, a, button')) { return; }

      var before = el.textContent;
      if (before.trim().length < 2) { return; }

      el.setAttribute('data-ppb-page', '1');
      el.setAttribute('contenteditable', 'true');
      el.setAttribute('spellcheck', 'false');

      el.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); el.blur(); }
        else if (ev.key === 'Escape') { el.textContent = before; el.blur(); }
      });

      el.addEventListener('paste', function (ev) {
        ev.preventDefault();
        var text = ((ev.clipboardData || window.clipboardData).getData('text') || '');
        document.execCommand('insertText', false, text.replace(/\s+/g, ' '));
      });

      el.addEventListener('focusout', function () {
        var value = (el.textContent || '').replace(/\s+/g, ' ').trim();
        var oldValue = before.trim();
        before = value;
        if (value === oldValue) {
          if (el.textContent !== value) { el.textContent = value; }
          return;
        }
        el.textContent = value;
        post({type: 'page-text', old: oldValue, value: value});
      });
    });
  }

  // ---------- 2. 卡片：拖动排序 / 跨栏目移动 ----------
  var drag = null;

  function decorateCards() {
    var cards = document.querySelectorAll('[data-ppb-card]');
    Array.prototype.forEach.call(cards, function (card) {
      if (card.getAttribute('data-ppb-handled') === '1') { return; }
      card.setAttribute('data-ppb-handled', '1');
      card.style.position = 'relative';
      var handle = document.createElement('span');
      handle.className = 'ppb-handle';
      handle.textContent = '\u2802';
      handle.title = '按住拖动，调整位置或移到别的栏目';
      handle.addEventListener('pointerdown', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        beginDrag(card);
      });
      card.appendChild(handle);
    });
  }

  function beginDrag(card) {
    drag = {card: card, moved: false};
    card.classList.add('ppb-dragging');
    card.style.pointerEvents = 'none';        // 让 elementFromPoint 指向下面的卡片
    document.body.classList.add('ppb-dragging-active');
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    document.addEventListener('pointercancel', onUp);
  }

  function onMove(ev) {
    if (!drag || !drag.card.parentNode) { return; }
    drag.moved = true;
    var under = document.elementFromPoint(ev.clientX, ev.clientY);
    if (!under || !under.closest) { return; }
    var container = under.closest('[data-ppb-cards]');
    if (!container) { return; }
    var target = under.closest('[data-ppb-card]');
    if (target === drag.card) { return; }

    if (target && target.parentNode === container) {
      var rect = target.getBoundingClientRect();
      var horizontal = container.scrollWidth > container.clientWidth + 4;
      var before = horizontal
        ? ev.clientX < rect.left + rect.width / 2
        : ev.clientY < rect.top + rect.height / 2;
      container.insertBefore(drag.card, before ? target : target.nextSibling);
      container.classList.remove('ppb-drop-empty');
    } else if (!target) {
      container.appendChild(drag.card);
      container.classList.remove('ppb-drop-empty');
    }
  }

  // 按当前 DOM 顺序重新编号，保证拖动 / 跨栏之后改文字仍指向正确的卡片
  function renumber() {
    var containers = document.querySelectorAll('[data-ppb-cards]');
    Array.prototype.forEach.call(containers, function (c) {
      var sec = c.getAttribute('data-ppb-cards');
      var items = c.querySelectorAll('[data-ppb-card]');
      c.classList.toggle('ppb-drop-empty', items.length === 0);
      Array.prototype.forEach.call(items, function (el, i) {
        el.setAttribute('data-ppb-sec', sec);
        el.setAttribute('data-ppb-card', String(i));
      });
    });
  }

  function onUp() {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    document.removeEventListener('pointercancel', onUp);
    document.body.classList.remove('ppb-dragging-active');
    if (!drag) { return; }
    var card = drag.card, moved = drag.moved;
    card.classList.remove('ppb-dragging');
    card.style.pointerEvents = '';
    drag = null;
    if (!moved) { return; }

    // 先用旧编号把整体顺序报上去（此时子元素仍带原 sec/card），再重编号。
    // 每张卡带上拖动前的 (栏目, 卡片) 编号对：跨栏之后新栏目号已经变了，
    // 只有旧编号才能唯一指回主程序里对应的那张卡。
    var groups = [];
    Array.prototype.forEach.call(document.querySelectorAll('[data-ppb-cards]'),
      function (c) {
        var list = [];
        Array.prototype.forEach.call(c.querySelectorAll('[data-ppb-card]'),
          function (el) {
            list.push([
              parseInt(el.getAttribute('data-ppb-sec'), 10),
              parseInt(el.getAttribute('data-ppb-card'), 10)
            ]);
          });
        groups.push({sec: parseInt(c.getAttribute('data-ppb-cards'), 10), cards: list});
      });
    post({type: 'order', groups: groups});
    renumber();
  }

  // ---------- 3. 一次性提示 ----------
  function showTip() {
    try { if (sessionStorage.getItem('ppb_tip_off') === '1') { return; } } catch (e) {}
    var tip = document.createElement('div');
    tip.id = 'ppb-tip';
    tip.innerHTML = '<b>可视化编辑</b>　单击文字可直接修改 · 按住卡片左上角的圆点拖动，' +
                    '可调整顺序或拖到别的栏目';
    var x = document.createElement('span');
    x.className = 'ppb-x';
    x.textContent = '\u00d7';
    x.addEventListener('click', function () {
      if (tip.parentNode) { tip.parentNode.removeChild(tip); }
      try { sessionStorage.setItem('ppb_tip_off', '1'); } catch (e) {}
    });
    tip.appendChild(x);
    document.body.appendChild(tip);
    setTimeout(function () { tip.style.opacity = '0'; }, 12000);
    setTimeout(function () {
      if (tip.parentNode) { tip.parentNode.removeChild(tip); }
    }, 12800);
  }

  function init() {
    decorateCards();
    bindText();
    bindPageText();
    showTip();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>
"""


def build_preview_document(index_content: str, sections: List[Section]) -> str:
    """
    以真实 index.html 为骨架，替换栏目区，并把素材地址指向本地预览服务，
    这样刚拖进来、还没同步到仓库的文件也能立刻看到。
    """
    def media_src(card: Card) -> str:
        return "/media/" + quote(card.path.name, safe="")

    block = build_portfolio_html(sections, media_src, editable=True)
    if index_content and START_MARKER in index_content and END_MARKER in index_content:
        doc = replace_block(index_content, block)
    else:
        doc = ('<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8">'
               '<title>预览</title></head><body style="font-family:sans-serif;'
               'max-width:800px;margin:0 auto;padding:2rem;">\n<h2>项目经历</h2>\n'
               + START_MARKER + "\n" + block + "\n" + END_MARKER
               + "\n</body></html>")

    if "</body>" in doc:
        doc = doc.replace("</body>", PREVIEW_EDITOR + PREVIEW_BOOTSTRAP + "</body>", 1)
    else:
        doc += PREVIEW_EDITOR + PREVIEW_BOOTSTRAP
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
        self._event_seq = 0        # 已经取走的事件序号
        self._poll_gen = 0         # 防止重开预览后旧轮询线程继续跑

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def write(self, content: str, force: bool = False) -> bool:
        """
        内容有变化才写盘，避免每敲一个字都触发整页重载。
        force=True 时即使内容相同也重写（更新 mtime 让页面刷新回滚）。
        """
        if content == self._last_content and not force:
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
                cmd, cwd=str(BASE_DIR), **SPAWN_KW,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            self.app.push_log("!! 预览进程启动失败：{}".format(exc))
            return False

        threading.Thread(target=self._pump, daemon=True).start()

        # 预览页里的编辑动作靠轮询取回：子进程 -> 这里 -> 主线程队列
        self._event_seq = 0
        self._poll_gen += 1
        threading.Thread(target=self._poll_events,
                         args=(self._poll_gen,), daemon=True).start()
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

    def _poll_events(self, gen: int):
        """
        每 0.35 秒问一次：预览页有没有新的编辑事件。
        取到后丢进主线程队列，绝不在这里碰任何 Tk 控件。
        """
        while self._poll_gen == gen and self.is_running():
            url = "http://127.0.0.1:{}/api/events?since={}".format(
                self.port, self._event_seq)
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (OSError, ValueError):
                time.sleep(0.8)          # 服务还没起来或已退出，稍后再试
                continue
            except Exception:
                time.sleep(0.8)
                continue

            for event in data.get("events") or []:
                self.app.push_event(event)
            try:
                self._event_seq = int(data.get("seq") or self._event_seq)
            except (TypeError, ValueError):
                pass
            time.sleep(0.35)

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

        self.source_label = tk.Label(self, text=str(SOURCE_DIR), bg=C_PANEL, fg=C_MUTED,
                                     font=F_SMALL, anchor="w", wraplength=300)
        self.source_label.pack(fill="x", padx=12)

        self.scroll = ScrollFrame(self, bg=C_PANEL)
        self.scroll.pack(fill="both", expand=True, padx=(8, 0), pady=8)

        tk.Button(self, text="刷新素材", command=app.refresh_assets,
                  bg="#ffffff", fg=C_TEXT, font=F_UI, relief="solid", bd=1,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=10, pady=4).pack(fill="x", padx=12, pady=(0, 12))

    def set_source(self, folder: Path):
        """换素材目录后刷新顶部那行路径。"""
        self.source_label.configure(text=str(folder))

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
# 十、右侧：栏目编辑器（栏目 = 标题 + 排版 + 卡片列表）
# ==========================================================================

class CardEditor(tk.Frame):
    """栏目里的一张卡片：缩略图 + 标题 + 描述 + 移除。"""

    def __init__(self, master, app, card: Card, section: Section, index: int):
        tk.Frame.__init__(self, master, bg=C_PANEL)
        self.app = app
        self.card = card
        self.section = section
        self._silent = False          # 由程序回填时不要反过来改写数据
        self.grid_columnconfigure(1, weight=1)

        photo = app.get_photo(card.path, ROW_THUMB)
        if photo is not None:
            thumb = tk.Label(self, image=photo, bg=C_STRIP)
            app.keep_ref(photo)
        else:
            thumb = tk.Label(self, text="无预览", bg=C_STRIP, fg=C_MUTED,
                             font=F_SMALL, width=16, height=5)
        thumb.grid(row=0, column=0, rowspan=3, padx=(0, 10), pady=(6, 6), sticky="n")

        meta = tk.Frame(self, bg=C_PANEL)
        meta.grid(row=0, column=1, sticky="ew", pady=(6, 2))
        tag = "视频" if card.kind == "video" else "图片"
        tk.Label(meta, text="{}. {}".format(index, tag), bg=C_PANEL,
                 fg=C_MUTED, font=F_SMALL).pack(side="left")
        tk.Label(meta, text=card.path.name, bg=C_PANEL, fg=C_MUTED,
                 font=F_SMALL).pack(side="right")

        self.title_var = tk.StringVar(value=card.title)
        self.title_entry = tk.Entry(
            self, textvariable=self.title_var, font=F_SMALL, bg=C_PANEL, fg=C_TEXT,
            relief="solid", bd=1, insertbackground=C_TEXT,
        )
        self.title_entry.grid(row=1, column=1, sticky="ew", pady=(0, 4))
        self.title_var.trace_add("write", self._on_title_change)

        self.desc_text = tk.Text(
            self, height=2, wrap="word", font=F_SMALL, bg=C_PANEL, fg=C_TEXT,
            relief="solid", bd=1, insertbackground=C_TEXT,
        )
        self.desc_text.insert("1.0", card.description)
        self.desc_text.grid(row=2, column=1, sticky="ew", pady=(0, 4))
        self.desc_text.bind("<KeyRelease>", self._on_desc_change)

        tk.Button(self, text="移除", command=lambda: app.remove_card(card),
                  bg=C_PANEL, fg=C_DANGER, font=F_SMALL, relief="flat", bd=0,
                  activeforeground=C_DANGER, activebackground=C_STRIP,
                  cursor="hand2", padx=6).grid(row=3, column=0, columnspan=2,
                                               sticky="e", pady=(0, 4))

        # 点卡片任意处也算选中这一栏，避免"点了卡片却加到别的栏"
        for widget in (self, thumb, meta, self.title_entry, self.desc_text):
            widget.bind("<Button-1>", lambda e: app.set_active_section(section), add="+")

    def _on_title_change(self, *_args):
        if self._silent:
            return
        self.card.title = self.title_var.get()
        self.app.schedule_save()

    def _on_desc_change(self, _event=None):
        if self._silent:
            return
        self.card.description = self.desc_text.get("1.0", "end-1c").strip()
        self.app.schedule_save()

    def refresh(self):
        """预览窗里改过这张卡的文字，把新值抹回控件（不触发保存）。"""
        self._silent = True
        try:
            if self.title_var.get() != self.card.title:
                self.title_var.set(self.card.title)
            current = self.desc_text.get("1.0", "end-1c")
            if current != self.card.description:
                self.desc_text.delete("1.0", "end")
                self.desc_text.insert("1.0", self.card.description)
        finally:
            self._silent = False

    def focus_title(self):
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")


class SectionEditor(tk.Frame):
    """
    一个栏目：顶部是栏目标题 + 排版下拉 + 删除，下面是这个栏目的卡片。
    点一下栏目任意位置即把它设为"当前栏目"，之后点左侧素材就加到这一栏。
    """

    def __init__(self, master, app, section: Section, index: int):
        tk.Frame.__init__(self, master, bg=C_PANEL, highlightthickness=2,
                          highlightbackground=C_BORDER)
        self.app = app
        self.section = section
        self.index = index
        self._silent = False

        # ---- 栏目头 ----
        head = tk.Frame(self, bg="#f8fafc")
        head.pack(fill="x")

        self.badge = tk.Label(head, text="{:02d}".format(index), bg="#f8fafc",
                              fg=C_ACCENT, font=(F_UI_BOLD[0], 11, "bold"))
        self.badge.pack(side="left", padx=(10, 8), pady=8)

        self.title_var = tk.StringVar(value=section.title)
        self.title_entry = tk.Entry(
            head, textvariable=self.title_var, font=F_UI_BOLD, bg="#ffffff",
            fg=C_TEXT, relief="solid", bd=1, insertbackground=C_TEXT,
            width=1,          # 只留最小请求宽度，实际宽度靠 expand 撑开，
        )                     # 否则窗口一窄就会把右边的按钮挤出面板
        self.title_entry.pack(side="left", fill="x", expand=True, pady=8)
        self.title_var.trace_add("write", self._on_title_change)

        # 下拉框自带"网格布局/横向滑动"字样，不再单独加标签，给标题框让出宽度
        self.layout_combo = ttk.Combobox(
            head, values=[name for _k, name in LAYOUTS], state="readonly",
            width=13, font=F_SMALL)
        self.layout_combo.set(self._layout_label())
        self.layout_combo.pack(side="left", padx=(8, 4), pady=8)
        self.layout_combo.bind("<<ComboboxSelected>>", self._on_layout_change)

        # 栏目排序（卡片顺序在预览里拖，整栏顺序在这里调）
        tk.Button(head, text="↑", command=lambda: app.move_section(section, -1),
                  bg="#f8fafc", fg=C_MUTED, font=F_SMALL, relief="flat", bd=0,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=3).pack(side="left")
        tk.Button(head, text="↓", command=lambda: app.move_section(section, 1),
                  bg="#f8fafc", fg=C_MUTED, font=F_SMALL, relief="flat", bd=0,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=3).pack(side="left")

        tk.Button(head, text="删除", command=lambda: app.remove_section(section),
                  bg="#f8fafc", fg=C_DANGER, font=F_SMALL, relief="flat", bd=0,
                  activeforeground=C_DANGER, activebackground=C_STRIP,
                  cursor="hand2", padx=6).pack(side="left", padx=(4, 6))

        # 标题为空时的说明（第一栏复用页面上的「项目经历」大标题）
        self.note = tk.Label(self, bg=C_PANEL, fg=C_MUTED, font=F_SMALL,
                             anchor="w", text=self._note_text())
        self.note.pack(fill="x", padx=12, pady=(6, 0))

        # ---- 卡片区 ----
        self.body = tk.Frame(self, bg=C_PANEL)
        self.body.pack(fill="x", padx=12, pady=(4, 10))
        if section.cards:
            for i, card in enumerate(section.cards, start=1):
                row = CardEditor(self.body, app, card, section, i)
                row.pack(fill="x", pady=(0, 6))
                app.editor_panel.register_card(card, row)
                tk.Frame(self.body, bg=C_BORDER, height=1).pack(fill="x", pady=(0, 6))
        else:
            tk.Label(self.body, text="这一栏还没有内容，点左侧素材添加（或拖进来）",
                     bg=C_PANEL, fg="#9aa4af", font=F_SMALL).pack(pady=14)

        # 整块可点击 -> 设为当前栏目
        for widget in (self, head, self.badge, self.note, self.body, self.title_entry):
            widget.bind("<Button-1>", self._activate, add="+")

        self.set_active(app.active_section is section)

    # ---- 行为 ----

    def _activate(self, _event=None):
        self.app.set_active_section(self.section)

    def set_active(self, active: bool):
        self.configure(highlightbackground=C_ACCENT if active else C_BORDER)

    def _layout_label(self) -> str:
        for key, name in LAYOUTS:
            if key == self.section.layout:
                return name
        return LAYOUTS[0][1]

    def _note_text(self) -> str:
        if self.section.title.strip():
            return "这一栏在页面上显示为大标题「{}」".format(self.section.title.strip())
        return "标题留空则不显示栏目标题（第一栏可直接用页面上已有的「项目经历」）"

    def _on_title_change(self, *_args):
        if self._silent:
            return
        self.section.title = self.title_var.get()
        self.note.configure(text=self._note_text())
        self.app.schedule_save()

    def refresh(self):
        """预览窗里改过栏目标题，把新值抹回输入框（不触发保存）。"""
        self._silent = True
        try:
            if self.title_var.get() != self.section.title:
                self.title_var.set(self.section.title)
            self.note.configure(text=self._note_text())
        finally:
            self._silent = False

    def _on_layout_change(self, _event=None):
        label = self.layout_combo.get()
        for key, name in LAYOUTS:
            if name == label:
                self.section.layout = key
                break
        self.app.schedule_save()
        self.app.refresh_preview_now()       # 排版切换立即生效
        self.app.set_status("第 {} 栏排版：{}".format(self.index, self.section.layout))

    def focus_title(self):
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")


class EditorPanel(tk.Frame):
    """右侧栏目编辑区。"""

    def __init__(self, master, app):
        tk.Frame.__init__(self, master, bg=C_BG)
        self.app = app
        # 预览窗里改完文字后，按对象反查控件做"原地刷新"，不整块重建
        self.section_map = {}
        self.card_map = {}

        header = tk.Frame(self, bg=C_BG)
        header.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(header, text="页面栏目", bg=C_BG, fg=C_TEXT, font=F_UI_BOLD).pack(side="left")
        self.hint = tk.Label(header, text="", bg=C_BG, fg=C_MUTED, font=F_SMALL)
        self.hint.pack(side="left", padx=10)

        self.scroll = ScrollFrame(self, bg=C_BG)
        self.scroll.pack(fill="both", expand=True, padx=(10, 4), pady=(4, 8))
        self.scroll.body.configure(bg=C_BG)

    def register_card(self, card: Card, editor):
        self.card_map[id(card)] = editor

    def register_section(self, section: Section, editor):
        self.section_map[id(section)] = editor

    def refresh_card(self, card: Card) -> bool:
        editor = self.card_map.get(id(card))
        if editor is None:
            return False
        editor.refresh()
        return True

    def refresh_section(self, section: Section) -> bool:
        editor = self.section_map.get(id(section))
        if editor is None:
            return False
        editor.refresh()
        return True

    def render(self, sections: List[Section]):
        self.scroll.clear()
        self.section_map = {}
        self.card_map = {}
        sections = list(sections)

        if not sections:
            box = tk.Frame(self.scroll.body, bg=C_BG)
            box.pack(fill="both", expand=True, pady=50)
            tk.Label(box, text="还没有栏目", bg=C_BG, fg=C_TEXT,
                     font=F_UI_BOLD).pack()
            tk.Label(box, text="点右上角「＋ 新增栏目」建一个滑动展示栏",
                     bg=C_BG, fg=C_MUTED, font=F_SMALL).pack(pady=4)
            tk.Button(box, text="＋ 新增栏目", command=self.app.add_section,
                      bg=C_ACCENT, fg="#ffffff", font=F_UI, relief="flat", bd=0,
                      activebackground=C_ACCENT_DARK, activeforeground="#ffffff",
                      cursor="hand2", padx=18, pady=7).pack(pady=12)
            self.hint.configure(text="")
            return

        for idx, section in enumerate(sections, start=1):
            editor = SectionEditor(self.scroll.body, self.app, section, idx)
            editor.pack(fill="x", padx=4, pady=6)
            self.register_section(section, editor)
        self.hint.configure(text="{} 个栏目 · {} 张卡片".format(
            len(sections), count_cards(sections)))


# ==========================================================================
# 十一、图片压缩设置面板
# ==========================================================================

class CompressDialog(tk.Toplevel):
    """
    图片压缩设置。只影响【同步】时写进仓库的图片，本地素材原图始终不动。
    保存后写进 SETTINGS_FILE，下次打开自动带出。
    """

    def __init__(self, app):
        tk.Toplevel.__init__(self, app)
        self.app = app
        self.title("图片压缩")
        self.configure(bg=C_PANEL)
        self.resizable(False, False)
        self.transient(app)

        cfg = normalize_settings(app.settings)
        self.compress_var = tk.BooleanVar(value=cfg["compress"])
        self.quality_var = tk.IntVar(value=cfg["quality"])
        self.format_var = tk.StringVar(value=self._label_of(COMPRESS_FORMATS, cfg["format"]))
        self.width_var = tk.StringVar(value=self._label_of(COMPRESS_WIDTHS, cfg["max_width"]))

        body = tk.Frame(self, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=22, pady=(18, 6))

        tk.Label(body, text="图片压缩", bg=C_PANEL, fg=C_TEXT,
                 font=F_TITLE).pack(anchor="w")
        tk.Label(body, text="浏览器要下载完整张图才显示出来。图片压小，访客打开页面更快、\n"
                            "手机流量也更省；素材目录里的原图不会被改动。",
                 bg=C_PANEL, fg=C_MUTED, font=F_SMALL, justify="left").pack(
            anchor="w", pady=(4, 12))

        self.enable_box = tk.Checkbutton(
            body, text="同步时压缩图片（推荐）", variable=self.compress_var,
            command=self._sync_enabled, bg=C_PANEL, fg=C_TEXT, font=F_UI_BOLD,
            activebackground=C_PANEL, activeforeground=C_TEXT,
            selectcolor="#ffffff", anchor="w", bd=0, highlightthickness=0)
        self.enable_box.pack(anchor="w")

        form = tk.Frame(body, bg=C_PANEL)
        form.pack(fill="x", pady=(8, 0))
        form.columnconfigure(1, weight=1)

        def add_row(r, text, factory):
            tk.Label(form, text=text, bg=C_PANEL, fg=C_TEXT, font=F_UI).grid(
                row=r, column=0, sticky="w", pady=6, padx=(0, 12))
            widget = factory()
            widget.grid(row=r, column=1, sticky="we", pady=6)
            return widget

        self.format_box = add_row(0, "输出格式", lambda: ttk.Combobox(
            form, textvariable=self.format_var, state="readonly", width=26,
            values=[label for _, label in COMPRESS_FORMATS]))
        self.width_box = add_row(1, "长边上限", lambda: ttk.Combobox(
            form, textvariable=self.width_var, state="readonly", width=26,
            values=[label for _, label in COMPRESS_WIDTHS]))
        self.quality_scale = add_row(2, "压缩质量", lambda: tk.Scale(
            form, from_=60, to=95, orient="horizontal", variable=self.quality_var,
            showvalue=True, resolution=1, bg=C_PANEL, fg=C_TEXT,
            highlightthickness=0, troughcolor=C_STRIP, sliderrelief="flat",
            activebackground=C_ACCENT, length=240, font=F_SMALL))

        self.hint = tk.Label(body, text="", bg=C_PANEL, fg=C_MUTED,
                             font=F_SMALL, justify="left", wraplength=420)
        self.hint.pack(anchor="w", pady=(6, 0))
        self.est_label = tk.Label(body, text="", bg=C_PANEL, fg=C_ACCENT,
                                  font=F_SMALL, justify="left", wraplength=420)
        self.est_label.pack(anchor="w", pady=(4, 0))

        foot = tk.Frame(self, bg=C_PANEL)
        foot.pack(fill="x", padx=22, pady=(4, 16))
        tk.Button(foot, text="保存", command=self._save, bg=C_ACCENT, fg="#ffffff",
                  font=F_UI_BOLD, relief="flat", bd=0, cursor="hand2",
                  activebackground=C_ACCENT_DARK, activeforeground="#ffffff",
                  padx=18, pady=6).pack(side="right")
        tk.Button(foot, text="取消", command=self.destroy, bg="#ffffff", fg=C_TEXT,
                  font=F_UI, relief="solid", bd=1, activebackground=C_STRIP,
                  cursor="hand2", padx=14, pady=5).pack(side="right", padx=(0, 8))
        self.est_btn = tk.Button(
            foot, text="测算当前素材", command=self._estimate, bg="#e8f2fe",
            fg=C_ACCENT, font=F_UI, relief="solid", bd=1, cursor="hand2",
            activebackground="#d6e8fd", padx=12, pady=5)
        self.est_btn.pack(side="left")

        self._sync_enabled()
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center()

    # ---- 小工具 ----

    @staticmethod
    def _label_of(pairs, value) -> str:
        for key, label in pairs:
            if key == value:
                return label
        return pairs[0][1]

    @staticmethod
    def _value_of(pairs, label, default):
        for key, text in pairs:
            if text == label:
                return key
        return default

    def _center(self):
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        x = self.app.winfo_rootx() + max(0, (self.app.winfo_width() - w) // 2)
        y = self.app.winfo_rooty() + max(0, (self.app.winfo_height() - h) // 3)
        self.geometry("+{}+{}".format(x, y))

    def _sync_enabled(self):
        """关掉压缩时，格式 / 尺寸 / 质量都没有意义，灰掉免得误解。"""
        on = bool(self.compress_var.get())
        state = "readonly" if on else "disabled"
        self.format_box.configure(state=state)
        self.width_box.configure(state=state)
        self.quality_scale.configure(state="normal" if on else "disabled")
        self.est_btn.configure(state="normal" if on else "disabled")
        self.hint.configure(text=(
            "WebP 比 JPEG 再小约 25%，主流浏览器都支持；质量 80~85 肉眼几乎看不出差别，"
            "长边 1600 px 在笔记本上已经足够清晰。"
            if on else "关闭后同步会按原图上传，页面体积可能达到几十 MB。"))

    def _collect(self) -> dict:
        return normalize_settings({
            "compress": bool(self.compress_var.get()),
            "format": self._value_of(COMPRESS_FORMATS, self.format_var.get(), "webp"),
            "max_width": self._value_of(COMPRESS_WIDTHS, self.width_var.get(), 1600),
            "quality": int(self.quality_var.get()),
        })

    def _estimate(self):
        cards = all_cards(self.app.sections)
        if not cards:
            self.est_label.configure(text="当前还没有卡片，先添加素材再试算。")
            return
        cfg = self._collect()
        self.est_btn.configure(state="disabled", text="测算中…")
        self.est_label.configure(text="正在压缩试算，第一次会慢一些…")

        def work():
            try:
                result = estimate_image_savings(cards, cfg)
                result["ok"] = True
            except Exception as exc:           # 试算失败不影响正常使用
                result = {"ok": False, "error": str(exc)}
            self.app._queue.put(("savings", (self, result)))

        threading.Thread(target=work, daemon=True).start()

    def show_savings(self, result: dict):
        self.est_btn.configure(state="normal", text="测算当前素材")
        if not result.get("ok"):
            self.est_label.configure(text="测算失败：{}".format(result.get("error")))
            return
        if not result.get("count"):
            self.est_label.configure(text="当前卡片里没有图片（视频不参与压缩）。")
            return
        before = result["before"]
        saved = result["saved"]
        pct = int(round(saved * 100.0 / before)) if before else 0
        text = "{} 张图片：{} → {}，省 {}（{}%）".format(
            result["count"], human_size(before), human_size(result["after"]),
            human_size(saved), pct)
        if result.get("skipped"):
            text += "\n其中 {} 张已经足够小，会按原图上传。".format(result["skipped"])
        self.est_label.configure(text=text)

    def _save(self):
        cfg = save_settings(self._collect())
        self.app.settings = cfg
        self.app._update_compress_btn()
        self.app.set_status("图片压缩设置已保存")
        self.app.log("图片压缩：{}".format(describe_settings(cfg)))
        self.destroy()


# ==========================================================================
# 十一、隐私声明（首次运行必须确认）
# ==========================================================================

PRIVACY_VERSION = "1"                       # 声明有实质变化时加一，下次启动会重新征询
PRIVACY_FILE = DATA_DIR / "privacy.json"     # 只记录「已确认哪个版本、什么时候」
PRIVACY_DOC = BASE_DIR / "PRIVACY.md"


def privacy_accepted() -> bool:
    """当前版本的声明是否已经确认过。"""
    text = read_text_safely(PRIVACY_FILE)
    if not text:
        return False
    try:
        raw = json.loads(text)
    except ValueError:
        return False
    return isinstance(raw, dict) and str(raw.get("version")) == PRIVACY_VERSION


def accept_privacy() -> bool:
    """记下已确认的版本与时间。写不进去也不拦着用，下次启动会再问一遍。"""
    try:
        wrote = write_json(PRIVACY_FILE, {
            "version": PRIVACY_VERSION,
            "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        harden_permissions()
        return True if wrote or PRIVACY_FILE.exists() else False
    except OSError:
        return False


def privacy_summary() -> str:
    """对话框先显示这几条，看细节可以展开完整声明。"""
    data_dir = "~/.personal_page_builder"
    return (
        "Personal Page Builder 完全在你自己的电脑上运行。开始前请了解：\n"
        "\n"
        "一、不联网收集信息\n"
        "没有账号、遥测或使用统计；只有你点「同步到页面」时由本机 git 推送。\n"
        "\n"
        "二、只读你指定的两个目录\n"
        "素材目录（只扫第一层）与仓库目录；不读浏览器数据、剪贴板，不做全盘扫描。\n"
        "\n"
        "三、只写三个地方\n"
        "数据目录 " + data_dir + "、你仓库里的页面与 assets、同目录临时文件（写完即改名）。\n"
        "不写注册表、系统目录、开机启动项，也不碰桌面和文档。\n"
        "\n"
        "四、配置不会被重复写，也不会被写坏\n"
        "保存前先比对内容，一致就不落盘；写入走「临时文件 + 整文件替换」。\n"
        "\n"
        "五、预览只在本机\n"
        "只绑 127.0.0.1 随机端口，只接受本机访问，拒绝 .git、.env 等敏感路径。\n"
        "\n"
        "六、随时可以反悔\n"
        "退出后删除 " + data_dir + " 目录，本地数据即全部清除。"
    )


def privacy_full_text() -> str:
    """把 PRIVACY.md 转成适合纯文本框阅读的排版（去掉 Markdown 记号）。"""
    raw = read_text_safely(PRIVACY_DOC)
    if not raw or not raw.strip():
        return privacy_summary() + "\n\n（程序目录里没有找到 PRIVACY.md。）"

    lines = []
    for line in raw.splitlines():
        s = line.rstrip()
        stripped = s.strip()
        if stripped and set(stripped) <= set("-|: "):      # 表格分隔行
            continue
        s = s.replace("**", "").replace("`", "")
        if s.lstrip().startswith("#"):
            s = s.lstrip().lstrip("#").strip()
        if s.startswith("|"):                              # 表格行改成一行一条
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            s = "    " + " · ".join(cells)
        lines.append(s)

    out = []
    blank = 0
    for s in lines:
        if s.strip():
            blank = 0
            out.append(s)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


class PrivacyDialog(tk.Toplevel):
    """
    隐私与数据安全声明窗口。

    首次运行时必须点「同意并继续」才进主界面；点「不同意」则直接退出程序。
    view_only=True 时不显示同意/拒绝，只供在设置里重新查看。
    """

    def __init__(self, master, view_only: bool = False):
        tk.Toplevel.__init__(self, master)
        self.accepted = False
        self.view_only = view_only
        self._showing_full = False

        self.title("隐私与数据安全声明")
        self.configure(bg=C_PANEL)
        self.resizable(True, True)
        self.minsize(620, 460)
        if master is not None and master.winfo_viewable():
            self.transient(master)

        head = tk.Frame(self, bg=C_PANEL)
        head.pack(fill="x", padx=24, pady=(18, 6))
        tk.Label(head, text="隐私与数据安全声明", bg=C_PANEL, fg=C_TEXT,
                 font=F_TITLE).pack(anchor="w")
        subtitle = ("先在本地看完，再决定要不要继续。以后可以在「设置」里重新查看。"
                    if not view_only else
                    "当前版本 v{} 的完整说明。".format(PRIVACY_VERSION))
        tk.Label(head, text=subtitle, bg=C_PANEL, fg=C_MUTED, font=F_SMALL,
                 justify="left", wraplength=660).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=24, pady=(4, 0))

        self.text = tk.Text(
            body, wrap="word", width=74, height=22,
            font=("Microsoft YaHei UI", 10), bg="#ffffff", fg=C_TEXT,
            relief="solid", bd=1, padx=14, pady=12,
            spacing1=1, spacing3=3, insertwidth=0, cursor="arrow")
        self.text.tag_configure("heading",
                                font=("Microsoft YaHei UI", 11, "bold"),
                                spacing1=8, spacing3=2)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self._show(privacy_summary())

        foot = tk.Frame(self, bg=C_PANEL)
        foot.pack(fill="x", padx=24, pady=(10, 16))

        self.more_btn = tk.Button(
            foot, text="阅读完整声明", command=self._toggle_full,
            bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
            activebackground=C_STRIP, cursor="hand2", padx=12, pady=5)
        self.more_btn.pack(side="left")

        tk.Button(foot, text="打开 PRIVACY.md", command=self._open_doc,
                  bg="#ffffff", fg=C_MUTED, font=F_SMALL, relief="solid", bd=1,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=12, pady=5).pack(side="left", padx=(8, 0))

        if view_only:
            tk.Button(foot, text="关闭", command=self.destroy,
                      bg="#ffffff", fg=C_TEXT, font=F_UI, relief="solid", bd=1,
                      activebackground=C_STRIP, cursor="hand2",
                      padx=18, pady=6).pack(side="right")
        else:
            tk.Button(foot, text="同意并继续", command=self._accept,
                      bg=C_ACCENT, fg="#ffffff",
                      font=("Microsoft YaHei UI", 10, "bold"),
                      activebackground=C_ACCENT_DARK, activeforeground="#ffffff",
                      relief="flat", bd=0, cursor="hand2",
                      padx=20, pady=7).pack(side="right")
            tk.Button(foot, text="不同意，退出", command=self._decline,
                      bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
                      activebackground=C_STRIP, cursor="hand2",
                      padx=12, pady=6).pack(side="right", padx=(0, 8))
            self.protocol("WM_DELETE_WINDOW", self._decline)   # 关窗口等同于不同意

        self.bind("<Escape>", lambda _e: (self.destroy() if view_only else self._decline()))
        self._center(760, 700)
        self.grab_set()
        self.lift()
        self.focus_force()

    # ---- 内部 ----

    def _show(self, content: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line in content.splitlines():
            self.text.insert("end", line + "\n")
        # 给「一、」「1. 」这类小标题加粗，长文档才分得清层级
        for index, line in enumerate(content.splitlines(), start=1):
            if re.match(r"^[一二三四五六七八九十]+、", line.strip()):
                self.text.tag_add("heading",
                                  "{}.0".format(index), "{}.end".format(index))
        if content.startswith("隐私与数据安全声明"):
            self.text.tag_add("heading", "1.0", "1.end")
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _toggle_full(self):
        self._showing_full = not self._showing_full
        if self._showing_full:
            self._show(privacy_full_text())
            self.more_btn.configure(text="回到摘要")
        else:
            self._show(privacy_summary())
            self.more_btn.configure(text="阅读完整声明")

    def _open_doc(self):
        if not PRIVACY_DOC.exists():
            messagebox.showwarning("找不到文件",
                                   "程序目录里没有 PRIVACY.md。", parent=self)
            return
        if not open_in_file_manager(PRIVACY_DOC):
            messagebox.showinfo("无法自动打开",
                                "请手动打开：{}".format(PRIVACY_DOC), parent=self)

    def _accept(self):
        self.accepted = True
        self.destroy()

    def _decline(self):
        self.accepted = False
        self.destroy()

    def _center(self, w: int, h: int):
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        w = min(w, screen_w - 80)
        h = min(h, screen_h - 120)
        self.geometry("{}x{}+{}+{}".format(
            w, h, max(0, (screen_w - w) // 2), max(0, (screen_h - h) // 3)))


# ==========================================================================
# 十二、首次启动引导 / 目录设置
# ==========================================================================

class SetupDialog(tk.Toplevel):
    """
    指定「素材目录」和「本地仓库目录」的界面。

    master 可以是主窗口，也可以是首次启动时临时建的隐藏根窗口 —— 那时还没有主界面。
    这里只负责收集路径、检查目录、写 config.json；真正生效由调用方 apply_config()。
    """

    def __init__(self, master, config: dict, first_run: bool = False):
        tk.Toplevel.__init__(self, master)
        self.saved = False
        self.first_run = first_run
        cfg = normalize_config(config)

        self.title("欢迎使用 Personal Page Builder" if first_run else "设置")
        self.configure(bg=C_PANEL)
        self.resizable(False, False)
        if not first_run:
            self.transient(master)

        self.source_var = tk.StringVar(value=cfg["source_dir"])
        self.repo_var = tk.StringVar(value=cfg["repo_dir"])
        self.index_var = tk.StringVar(value=cfg["index_file"])
        self.remote_var = tk.StringVar(value=cfg["remote"])
        self.branch_var = tk.StringVar(value=cfg["branch"])
        self.ffmpeg_var = tk.StringVar(value=cfg["ffmpeg_path"])

        body = tk.Frame(self, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=24, pady=(18, 4))

        tk.Label(body, text=("首次使用：指定两个目录" if first_run else "目录与推送设置"),
                 bg=C_PANEL, fg=C_TEXT, font=F_TITLE).pack(anchor="w")
        if first_run:
            intro = ("告诉程序素材放在哪、要改哪个仓库，就可以开始了。\n"
                     "这些设置只存在本机的用户目录（~/.personal_page_builder）里，不会上传。")
        else:
            intro = "改完保存会立刻切换工作目录；每个仓库的编辑进度分开保存，互不影响。"
        tk.Label(body, text=intro, bg=C_PANEL, fg=C_MUTED, font=F_SMALL,
                 justify="left", wraplength=520).pack(anchor="w", pady=(4, 10))

        form = tk.Frame(body, bg=C_PANEL)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def pick_dir(var):
            initial = var.get().strip() or str(Path.home())
            chosen = filedialog.askdirectory(parent=self, initialdir=initial,
                                             mustexist=True, title="选择目录")
            if chosen:
                var.set(str(Path(chosen)))

        def add_field(row, label, var, hint, browse=False, width=46):
            tk.Label(form, text=label, bg=C_PANEL, fg=C_TEXT, font=F_UI).grid(
                row=row, column=0, sticky="w", pady=(7, 0), padx=(0, 10))
            entry = tk.Entry(form, textvariable=var, font=F_UI, relief="solid", bd=1,
                             bg="#ffffff", fg="#24292f", width=width,
                             highlightthickness=0)
            entry.grid(row=row, column=1, sticky="we", pady=(7, 0))
            if browse:
                tk.Button(form, text="浏览…", command=lambda: pick_dir(var),
                          bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
                          activebackground=C_STRIP, cursor="hand2",
                          padx=8, pady=1).grid(row=row, column=2, padx=(8, 0), pady=(7, 0))
            tk.Label(form, text=hint, bg=C_PANEL, fg=C_MUTED, font=F_SMALL,
                     justify="left", wraplength=520).grid(
                row=row + 1, column=1, columnspan=2, sticky="w", pady=(1, 0))
            return entry

        add_field(0, "素材目录", self.source_var,
                  "放图片 / 视频的文件夹，左侧素材库就是它。", browse=True)
        add_field(2, "仓库目录", self.repo_var,
                  "本地 git 仓库（git clone 下来的个人主页），同步会提交并推送这里。", browse=True)
        add_field(4, "页面文件", self.index_var,
                  "相对仓库根目录的路径，默认 index.html。只会改其中 PORTFOLIO 标记之间的内容。")
        add_field(6, "远端名", self.remote_var,
                  "一般就是 origin；推送目标由该远端指向的仓库决定。")
        add_field(8, "分支", self.branch_var,
                  "留空 = 推送到当前分支在远端的同名分支；填 main 之类则强制推到该分支。")
        add_field(10, "ffmpeg", self.ffmpeg_var,
                  "可选。视频首帧缩略图需要它，留空则自动从 PATH 里找。", browse=True)

        self.status = tk.Label(body, text="", bg=C_PANEL, fg=C_MUTED, font=F_SMALL,
                               justify="left", wraplength=520)
        self.status.pack(anchor="w", pady=(8, 0))

        foot = tk.Frame(self, bg=C_PANEL)
        foot.pack(fill="x", padx=24, pady=(4, 16))
        tk.Button(foot, text=("开始使用" if first_run else "保存"), command=self._save,
                  bg=C_ACCENT, fg="#ffffff", font=F_UI_BOLD, relief="flat", bd=0,
                  cursor="hand2", activebackground=C_ACCENT_DARK,
                  activeforeground="#ffffff", padx=18, pady=6).pack(side="right")
        tk.Button(foot, text="取消", command=self.destroy, bg="#ffffff", fg=C_TEXT,
                  font=F_UI, relief="solid", bd=1, activebackground=C_STRIP,
                  cursor="hand2", padx=14, pady=5).pack(side="right", padx=(0, 8))
        tk.Button(foot, text="检查目录", command=self._check_clicked, bg="#e8f2fe",
                  fg=C_ACCENT, font=F_UI, relief="solid", bd=1, cursor="hand2",
                  activebackground="#d6e8fd", padx=12, pady=5).pack(side="left")
        tk.Button(foot, text="隐私声明", command=self._show_privacy, bg="#ffffff",
                  fg=C_MUTED, font=F_SMALL, relief="solid", bd=1, cursor="hand2",
                  activebackground=C_STRIP, padx=10, pady=5).pack(side="left", padx=(8, 0))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._center()
        self.grab_set()
        self.lift()
        self.focus_force()

    # ---- 小工具 ----

    def _center(self):
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        parent = self.master
        if parent is not None and parent.winfo_viewable():
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
        else:
            x = max(0, (self.winfo_screenwidth() - w) // 2)
            y = max(0, (self.winfo_screenheight() - h) // 3)
        self.geometry("+{}+{}".format(x, y))

    def _show_privacy(self):
        """在设置里重新查看隐私声明（只读，不改已确认状态）。"""
        try:
            self.grab_release()
        except tk.TclError:
            pass
        dialog = PrivacyDialog(self, view_only=True)
        self.wait_window(dialog)
        try:
            self.grab_set()
            self.lift()
        except tk.TclError:
            pass

    def _collect(self) -> dict:
        return normalize_config({
            "source_dir": self.source_var.get().strip(),
            "repo_dir": self.repo_var.get().strip(),
            "index_file": self.index_var.get().strip(),
            "remote": self.remote_var.get().strip(),
            "branch": self.branch_var.get().strip(),
            "ffmpeg_path": self.ffmpeg_var.get().strip(),
        })

    @staticmethod
    def _git_remotes(repo: Path):
        """列出仓库已有远端；git 不可用或不是仓库时返回 None。"""
        try:
            proc = subprocess.run(["git", "remote"], cwd=str(repo),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=15, **SPAWN_KW)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.split()

    def _check_clicked(self) -> bool:
        cfg = self._collect()
        problems = config_problems(cfg)
        if problems:
            self.status.configure(fg=C_DANGER,
                                  text="\n".join("· " + p for p in problems))
            return False

        notes = []
        repo = Path(cfg["repo_dir"])
        index_path = repo / cfg["index_file"]
        if not (repo / ".git").exists():
            notes.append("这个目录里没有 .git，同步时无法提交；如果仓库在上层目录，请选那一层。")
        if not index_has_markers(index_path):
            notes.append("页面里还没有 PORTFOLIO 标记，保存时会问你要不要自动插入。")
        remotes = self._git_remotes(repo)
        if remotes is not None and cfg["remote"] not in remotes:
            notes.append("仓库现有远端：{}，和你填的「{}」对不上。".format(
                "、".join(remotes) or "（无）", cfg["remote"]))

        if notes:
            self.status.configure(fg="#b26a00", text="\n".join("· " + n for n in notes))
        else:
            self.status.configure(fg="#1a7f37", text="目录检查通过，可以保存。")
        return True

    def _save(self):
        cfg = self._collect()
        problems = config_problems(cfg)
        if problems:
            messagebox.showerror("还不能保存", "\n".join(problems), parent=self)
            return

        repo = Path(cfg["repo_dir"])
        index_path = repo / cfg["index_file"]
        if not index_has_markers(index_path):
            if not messagebox.askyesno(
                    "页面缺少标记",
                    "{} 里没有 PORTFOLIO_START / PORTFOLIO_END 标记。\n\n"
                    "程序只会替换这两个标记之间的内容，所以得先把标记插进去。\n"
                    "现在自动插入吗？会在 </body> 前插入两行注释。\n"
                    "原文件会先备份到你本机的数据目录（不会放进仓库）。".format(
                        cfg["index_file"]), parent=self):
                self.status.configure(fg=C_DANGER,
                                      text="还没插入标记，同步时无法定位作品区。请手动加或重新保存。")
                return
            if not insert_markers(index_path, repo):
                messagebox.showerror(
                    "插入失败",
                    "写入 {} 失败，可能是文件只读或没有权限。".format(index_path),
                    parent=self)
                return

        if not (repo / ".git").exists():
            if not messagebox.askyesno(
                    "不是 git 仓库",
                    "{} 里没有 .git 目录。\n"
                    "你仍然可以用它生成 HTML，但同步的最后一步需要 git 才能提交推送。\n\n"
                    "仍然保存吗？".format(repo), parent=self):
                return

        save_config(cfg)
        self.saved = True
        self.destroy()


# ==========================================================================
# 十三、主窗口
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

        self.sections: List[Section] = []
        self.active_section: Optional[Section] = None
        self.asset_files: List[Path] = []
        self._photo_cache = {}          # (Path, size) -> PhotoImage
        self._refs = []                 # 额外引用池
        self._drag = None
        self._save_job = None
        self._queue = queue.Queue()
        self._syncing = False

        self.preview = PreviewController(self)   # 预览子进程
        self.watcher = None                      # 素材目录监控
        self._preview_job = None                 # 预览刷新防抖
        self._preview_enabled = True             # 用户是否希望开预览
        self._saved_size = None                  # 为预览收窄前的窗口尺寸
        self._index_backed_up = False            # 本次会话是否已备份过页面
        self.settings = load_settings()          # 图片压缩等选项
        self.config = load_config()              # 用户自己指定的目录与远端

        self._build_ui()
        self._load_initial_cards()
        self.refresh_assets()
        self.render_sections()
        self._update_compress_btn()
        self._update_header()
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
        self.subtitle = tk.Label(left, text="可视化管理 GitHub Pages 个人主页",
                                 bg=C_PANEL, fg=C_MUTED, font=F_SMALL)
        self.subtitle.pack(anchor="w")

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

        self.add_section_btn = tk.Button(
            right, text="＋ 新增栏目", command=self.add_section,
            bg="#e8f2fe", fg=C_ACCENT, font=("Microsoft YaHei UI", 9, "bold"),
            relief="solid", bd=1, activebackground="#d6e8fd",
            cursor="hand2", padx=12, pady=3)
        self.add_section_btn.pack(side="right", padx=(0, 8))

        self.compress_btn = tk.Button(
            right, text="图片压缩", command=self.open_compress_dialog,
            bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
            activebackground=C_STRIP, cursor="hand2", padx=10, pady=3)
        self.compress_btn.pack(side="right", padx=(0, 8))

        tk.Button(right, text="⚙ 设置", command=self.open_settings,
                  bg="#ffffff", fg=C_TEXT, font=F_SMALL, relief="solid", bd=1,
                  activebackground=C_STRIP, cursor="hand2",
                  padx=10, pady=3).pack(side="right", padx=(0, 8))

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
        self.paned = paned

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
            action, text="同步到页面", command=self.on_sync_click,
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
        self.log("配置文件：{}".format(CONFIG_FILE))

    # ---------------- 数据加载 ----------------

    def _load_initial_cards(self):
        del STATE_WARNINGS[:]
        state = load_state()
        for warning in STATE_WARNINGS:        # 存档损坏、被隔离之类的情况
            self.log("!! " + warning)
        del STATE_WARNINGS[:]
        if state is not None:
            self.sections = state
            self.active_section = self.sections[0] if self.sections else None
            self.log("已从本地存档恢复 {} 个栏目、{} 张卡片。".format(
                len(self.sections), count_cards(self.sections)))
            return

        # 首次运行：从 index.html 反向导入，避免覆盖已有内容
        if INDEX_FILE.exists():
            try:
                content = INDEX_FILE.read_text(encoding="utf-8")
            except OSError:
                content = ""
            imported = import_cards_from_html(content, SOURCE_DIR) if content else []
            if imported:
                section = Section(title="", layout=DEFAULT_LAYOUT, cards=imported)
                self.sections = [section]
                self.active_section = section
                self.log("首次运行：已从页面导入 {} 张现有卡片。".format(len(imported)))
                save_state(self.sections)
                return
            block_text = extract_block(content)
            if "<img" in block_text or "<video" in block_text:
                messagebox.showwarning(
                    "提示",
                    "{} 里已经有作品内容，但认不出对应的本地素材，没法自动变成卡片。\n"
                    "点击同步会用当前栏目覆盖这段区域，请确认后再操作。".format(
                        CONFIG["index_file"]),
                )
        # 空文档也先给一个栏目，免得用户不知道从哪开始
        first = Section(title="", layout=DEFAULT_LAYOUT)
        self.sections = [first]
        self.active_section = first
        self.log("首次运行：已建好第 1 个栏目，点击左侧素材开始添加。")

    def refresh_assets(self):
        self.asset_panel.set_source(SOURCE_DIR)
        self.asset_files = scan_assets(SOURCE_DIR)
        self.asset_panel.populate(self.asset_files)
        self.log("素材扫描完成，共 {} 个文件。".format(len(self.asset_files)))

    # ---------------- 缩略图缓存 ----------------

    def get_photo(self, path: Path, size=None):
        """按 (路径, 尺寸) 缓存缩略图，左侧素材库和右侧卡片行各用各的尺寸。"""
        size = size or (THUMB_W, THUMB_H)
        key = (str(path), size)
        if key in self._photo_cache:
            return self._photo_cache[key]
        image = make_thumbnail(path, kind_of(path), size)
        if image is None:
            return None
        photo = ImageTk.PhotoImage(image)
        self._photo_cache[key] = photo
        return photo

    def keep_ref(self, photo):
        self._refs.append(photo)

    # ---------------- 栏目操作 ----------------

    def render_sections(self):
        # 重建面板前记住滚动位置，改完文字回来不会跳回顶部
        yview = None
        try:
            yview = self.editor_panel.scroll.canvas.yview()
        except Exception:
            pass
        self.editor_panel.render(self.sections)
        if yview:
            try:
                self.editor_panel.scroll.canvas.yview_moveto(yview[0])
            except Exception:
                pass
        self._sync_status()

    def set_active_section(self, section: Section):
        """当前栏目：点左侧素材默认加到这里；边框高亮提示。"""
        if section is self.active_section:
            return
        self.active_section = section
        for editor in self.editor_panel.scroll.body.winfo_children():
            if isinstance(editor, SectionEditor):
                editor.set_active(editor.section is section)

    def section_of(self, card: Card) -> Optional[Section]:
        for section in self.sections:
            if any(c is card for c in section.cards):
                return section
        return None

    def add_section(self):
        """新增一个滑动展示栏，并选中它。"""
        section = Section(title="新栏目", layout="slider")
        self.sections.append(section)
        self.active_section = section
        self.render_sections()
        self.editor_panel.scroll.to_bottom()
        self.set_status("已新增栏目「{}」".format(section.title))
        self.log("新增栏目：{}（默认横向滑动，可在栏目里改标题和排版）".format(section.title))
        self.schedule_save()
        self.touch_preview(immediate=True)
        # 让用户直接开始敲标题
        for editor in self.editor_panel.scroll.body.winfo_children():
            if isinstance(editor, SectionEditor) and editor.section is section:
                editor.focus_title()
                break

    def move_section(self, section: Section, delta: int):
        """整栏上移 / 下移（预览里只能拖卡片，栏目的顺序在这里调）。"""
        try:
            i = self.sections.index(section)
        except ValueError:
            return
        j = i + delta
        if j < 0 or j >= len(self.sections):
            self.set_status("已经是第 1 栏了" if delta < 0 else "已经是最后一栏了")
            return
        self.sections[i], self.sections[j] = self.sections[j], self.sections[i]
        self.active_section = section
        self.render_sections()
        self.schedule_save()
        self.touch_preview(immediate=True)
        self.set_status("栏目已{}移".format("上" if delta < 0 else "下"))
        self.log("栏目「{}」{}移到第 {} 位".format(
            section.title.strip() or "未命名", "上" if delta < 0 else "下", j + 1))

    def remove_section(self, section: Section):
        n = len(section.cards)
        label = section.title.strip() or "未命名栏目"
        if not messagebox.askyesno(
                "删除栏目", "确定删除「{}」吗？{}".format(
                    label, "（含 {} 张卡片）".format(n) if n else "")):
            return
        self.sections = [s for s in self.sections if s is not section]
        if self.active_section is section:
            self.active_section = self.sections[0] if self.sections else None
        self.render_sections()
        self.set_status("已删除栏目「{}」".format(label))
        self.schedule_save()
        self.touch_preview(immediate=True)

    # ---------------- 卡片操作 ----------------

    def add_card(self, path: Path, section: Optional[Section] = None):
        target = section or self.active_section
        if target is None:
            target = self.sections[-1] if self.sections else None
        if target is None:                       # 一个栏目都没有时先建一个
            target = Section(title="新栏目", layout="slider")
            self.sections.append(target)
        for card in target.cards:                # 只在同一栏里判重
            if card.source == str(path):
                self.set_status("这一栏里已经有该素材了")
                return
        card = Card(source=str(path), kind=kind_of(path),
                    title=format_title(path.name), description="")
        target.cards.append(card)
        self.active_section = target
        self.render_sections()
        self.editor_panel.scroll.to_bottom()
        self.set_status("已添加到第 {} 栏：{}".format(
            self.sections.index(target) + 1, path.name))
        self.schedule_save()
        self.touch_preview(immediate=True)

    def remove_card(self, card: Card):
        section = self.section_of(card)
        if section is None:
            return
        section.cards = [c for c in section.cards if c is not card]
        self.render_sections()
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
            self.add_card(item.path)        # 单击即添加到当前栏目
            return

        target = self.winfo_containing(x_root, y_root)
        section = self._section_under(target)
        if section is not None:             # 拖到哪一栏就加到哪一栏
            self.add_card(item.path, section)
        elif target is not None and is_descendant(target, self.editor_panel):
            self.add_card(item.path)        # 落在空白处 -> 当前栏目
        else:
            self.set_status("拖拽取消：请放到右侧某个栏目里")

    def _section_under(self, widget) -> Optional[Section]:
        """从落点控件向上找它属于哪个栏目。"""
        node = widget
        while node is not None:
            if isinstance(node, SectionEditor):
                return node.section
            node = getattr(node, "master", None)
        return None

    # ---------------- 状态存档（防抖） ----------------

    def schedule_save(self):
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self._save_now)
        self.touch_preview()             # 文案改动 -> 预览防抖刷新

    def _save_now(self):
        self._save_job = None
        try:
            wrote = save_state(self.sections)
        except OSError as exc:
            self.log("!! 保存本地存档失败：{}".format(exc))
            self.set_status("自动保存失败，详见日志")
            return
        self._sync_status(time.strftime("%H:%M:%S") if wrote else None)

    def _sync_status(self, saved_at: Optional[str] = None):
        text = "{} 个栏目 · {} 张卡片".format(
            len(self.sections), count_cards(self.sections))
        if saved_at:
            text = "已保存 {} · ".format(saved_at) + text
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)

    def set_status(self, text: str):
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)

    # ---------------- 实时预览 ----------------

    # ---------------- 预览里的可视化编辑 ----------------

    def push_event(self, event: dict):
        """预览轮询线程回调：只入队，绝不在子线程里碰 Tk 控件。"""
        self._queue.put(("event", event))

    def apply_preview_event(self, event: dict):
        """把预览窗里的编辑动作落回数据模型。"""
        try:
            kind = event.get("type")
            if kind == "text":
                self._apply_text_event(event)
            elif kind == "order":
                self._apply_order_event(event)
            elif kind == "page-text":
                self._apply_page_text_event(event)
            else:
                return
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self.log("!! 已忽略一条预览编辑事件：{}".format(exc))
            # 页面上已经显示改了、数据却没接受：强制重画一次让它回滚，避免"看着改了其实没存"
            self.refresh_preview_now(force=True)
            self.set_status("这处改动未被接受，已还原")
            return
        self._save_now()

    def _apply_text_event(self, event: dict):
        sec_index = int(event["sec"])
        if not 0 <= sec_index < len(self.sections):
            raise ValueError("栏目下标越界")
        section = self.sections[sec_index]
        field = event.get("field")
        value = (event.get("value") or "").strip()

        if field == "section-title":
            if section.title == value:
                return
            section.title = value
            self.editor_panel.refresh_section(section)
            self.log("预览里改了栏目标题：{}".format(value or "（留空）"))
            self.set_status("栏目标题已更新")
            return

        card_index = event.get("card")
        if card_index is None:
            raise ValueError("缺少卡片下标")
        card_index = int(card_index)
        if not 0 <= card_index < len(section.cards):
            raise ValueError("卡片下标越界")
        card = section.cards[card_index]
        if field == "title":
            card.title = value
        elif field == "desc":
            card.description = value
        else:
            raise ValueError("未知字段 {}".format(field))
        self.editor_panel.refresh_card(card)
        self.log("预览里改了「{}」的{}".format(
            card.path.name, "标题" if field == "title" else "描述"))
        self.set_status("卡片文字已更新")

    def _apply_page_text_event(self, event: dict):
        """
        改作品区之外的页面文字（大标题、简介、技能条目…）。
        直接写回 index.html，并留一份改动前的备份；匹配不唯一就放弃，绝不猜。
        """
        old = (event.get("old") or "").strip()
        value = (event.get("value") or "").strip()
        if not old:
            raise ValueError("缺少原文，无法定位")

        content = self.current_index_content()
        if not content:
            raise ValueError("读不到 {}".format(CONFIG["index_file"]))
        updated = replace_page_text(content, old, value)
        if updated is None:
            raise ValueError(
                "「{}」在页面上不止一处（或带格式），不敢自动替换，"
                "请到 {} 里手改".format(old[:18], CONFIG["index_file"]))

        self._backup_index_once()
        atomic_write(INDEX_FILE, updated)
        self.log("页面文字：「{}」→「{}」（已写入 {}）".format(
            old[:24], value[:24] or "（空）", CONFIG["index_file"]))
        self.set_status("页面文字已写入 {}".format(CONFIG["index_file"]))
        # 这里不重写预览：页面上的 DOM 已经是新文字，重写只会多闪一次

    def _backup_index_once(self):
        """本次会话第一次动 index.html 前留个底，方便后悔。"""
        if self._index_backed_up:
            return
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(INDEX_FILE), str(BACKUP_DIR / "index.before-text-edit.html"))
            self._index_backed_up = True
            self.log("已备份改动前的页面到：{}".format(BACKUP_DIR))
        except OSError as exc:
            self.log("!! 备份页面失败（继续执行）：{}".format(exc))

    def _apply_order_event(self, event: dict):
        """预览里拖动卡片后回传整页分组顺序，按旧编号重组，避免下标错位。"""
        groups = event.get("groups") or []
        if not groups:
            raise ValueError("排序事件为空")

        pool = {}
        for sec_index, section in enumerate(self.sections):
            for card_index, card in enumerate(section.cards):
                pool[(sec_index, card_index)] = card

        seen = set()
        plan = []
        for group in groups:
            sec_index = int(group["sec"])
            if not 0 <= sec_index < len(self.sections):
                raise ValueError("排序指向的栏目不存在")
            cards = []
            for ref in group.get("cards") or []:
                # 新格式是 [拖动前的栏目号, 拖动前的卡片号]；旧格式只有卡片号
                if isinstance(ref, (list, tuple)) and len(ref) == 2:
                    key = (int(ref[0]), int(ref[1]))
                else:
                    key = (sec_index, int(ref))
                if key not in pool:
                    raise ValueError("排序指向的卡片不存在")
                if key in seen:
                    raise ValueError("同一张卡片出现了两次")
                seen.add(key)
                cards.append(pool[key])
            plan.append((sec_index, cards))

        if len(seen) != len(pool):
            raise ValueError("有卡片在排序结果里丢了")

        for sec_index, cards in plan:
            self.sections[sec_index].cards = cards

        self.render_sections()
        self.log("预览里调整了卡片顺序（{} 个栏目）".format(len(plan)))
        self.set_status("卡片顺序已更新")

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
            self.refresh_preview_now()
            return
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(PREVIEW_DEBOUNCE_MS, self.refresh_preview_now)

    def refresh_preview_now(self, force: bool = False):
        self._preview_job = None
        if not self.preview.is_running():
            return
        doc = build_preview_document(self.current_index_content(), self.sections)
        if self.preview.write(doc, force=force):
            self.set_status("预览已刷新")

    def start_preview(self, quiet: bool = False):
        """先写一份最新 HTML，再拉起预览子进程。"""
        if self.preview.is_running():
            return
        self._make_room_for_preview()
        doc = build_preview_document(self.current_index_content(), self.sections)
        self.preview.write(doc)
        if self.preview.start():
            self._preview_enabled = True
            if not quiet:
                self.log("实时预览已打开（端口 {}）。".format(self.preview.port))
                self.log("预览窗里可以直接改文字、拖卡片调顺序，改动会立刻存到本地存档。")
        else:
            self._preview_enabled = False
            if self._saved_size is not None:      # 启动失败就把窗口尺寸还原
                self.geometry("{}x{}".format(*self._saved_size))
                self._saved_size = None
            self.log("!! 实时预览启动失败：请确认已安装 pywebview（python -m pip install pywebview）。")
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
        # 右侧编辑区被挤太窄时，把素材栏也收一点
        try:
            left_pane = self.nametowidget(self.paned.panes()[0])
            if self.winfo_width() - left_pane.winfo_width() < 500:
                self.paned.sash_place(0, 320, 0)
        except Exception:
            pass
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
        total = count_cards(self.sections)
        cfg = normalize_settings(self.settings)
        if cfg["compress"]:
            media_note = "图片压成 {}，长边 ≤ {} px".format(
                cfg["format"].upper(), cfg["max_width"] or "不限")
        else:
            media_note = "按原图上传"
        if total == 0:
            if not messagebox.askyesno("确认", "当前没有任何卡片，同步会清空页面上的作品区，继续吗？"):
                return
        elif CONFIRM_BEFORE_PUSH:
            ok = messagebox.askyesno(
                "确认同步",
                "即将：\n"
                "1. 复制 {} 个素材到仓库（{}）\n"
                "2. 覆盖 {} 中 PORTFOLIO 标记之间的内容（{} 个栏目）\n"
                "3. 提交并推送到 GitHub（公开可见）\n\n"
                "确认继续？".format(total, media_note, CONFIG["index_file"],
                                   len(self.sections)),
            )
            if not ok:
                return

        self._syncing = True
        self.sync_btn.configure(state="disabled", text="同步中…", bg="#8fb8e8")
        self.set_status("正在同步…")
        snapshot = [Section(title=s.title, layout=s.layout, cards=list(s.cards))
                    for s in self.sections]          # 深拷贝给线程
        thread = threading.Thread(target=self._sync_worker,
                                  args=(snapshot, cfg), daemon=True)
        thread.start()

    def _sync_worker(self, sections: List[Section], settings: dict):
        """后台线程：只做 I/O，通过队列把消息交给主线程。"""
        def log(msg):
            self._queue.put(("log", msg))
        try:
            do_sync(sections, log, settings)
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
                elif kind == "event":
                    self.apply_preview_event(payload)
                elif kind == "savings":
                    dialog, result = payload
                    try:
                        if dialog.winfo_exists():
                            dialog.show_savings(result)
                    except tk.TclError:
                        pass            # 设置面板已关掉，结果直接丢弃
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
        self.sync_btn.configure(state="normal", text="同步到页面", bg=C_ACCENT)
        self.set_status(message)

    # ---------------- 图片压缩 ----------------

    def _update_compress_btn(self):
        """按钮上直接显示当前压缩策略，不用点开也能看到。"""
        cfg = normalize_settings(self.settings)
        if cfg["compress"]:
            size = "{}px".format(cfg["max_width"]) if cfg["max_width"] else "原尺寸"
            self.compress_btn.configure(
                text="图片压缩：{}·{}".format(cfg["format"].upper(), size))
        else:
            self.compress_btn.configure(text="图片压缩：关")

    def open_compress_dialog(self):
        CompressDialog(self)

    # ---------------- 目录设置 ----------------

    def _update_header(self):
        """标题下面一行显示当前在编辑哪个仓库，换仓库后要跟着变。"""
        name = REPO_DIR.name or str(REPO_DIR)
        self.subtitle.configure(text="正在编辑：{} · {}".format(name, CONFIG["index_file"]))

    def open_settings(self):
        dialog = SetupDialog(self, self.config, first_run=False)
        self.wait_window(dialog)
        if dialog.saved:
            self.reload_config()

    def reload_config(self):
        """换了素材目录 / 仓库 / 远端之后，把界面和数据整体换到新配置上。"""
        cfg = load_config()
        if config_problems(cfg):
            messagebox.showerror("配置有问题", "\n".join(config_problems(cfg)))
            return
        apply_config(cfg)
        self.config = cfg

        # 先停掉和旧目录绑定的东西
        was_previewing = self.preview.is_running()
        self.preview.stop()
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._preview_job = None
        self._index_backed_up = False
        self._photo_cache.clear()
        self._refs.clear()

        self.sections = []
        self.active_section = None
        self._load_initial_cards()
        self.refresh_assets()
        self.render_sections()
        self._update_header()
        self._update_compress_btn()
        self._start_watcher()
        if was_previewing:
            self.start_preview(quiet=True)
        self.log("已切换工作目录：素材 {} / 仓库 {}".format(SOURCE_DIR, REPO_DIR))
        self.set_status("设置已更新")

    def open_repo(self):
        if not REPO_DIR.exists():
            messagebox.showerror("错误", "仓库目录不存在：{}".format(REPO_DIR))
            return
        if not open_in_file_manager(REPO_DIR):
            self.log("请手动打开：{}".format(REPO_DIR))


def main():
    if not HAS_PIL:
        print("[提示] 未安装 Pillow，缩略图将降级为文字占位。")

    root = tk.Tk()
    root.withdraw()

    # 1. 隐私声明：首次运行（或声明版本更新后）必须先确认，否则不继续
    if not privacy_accepted():
        dialog = PrivacyDialog(root)
        root.wait_window(dialog)
        if not dialog.accepted:
            messagebox.showinfo(
                "已退出",
                "没有确认隐私声明，程序不会继续运行。\n"
                "你的素材和仓库文件都没有被改动。\n\n"
                "想先了解清楚可以打开程序目录里的 PRIVACY.md。",
                parent=root)
            root.destroy()
            return
        accept_privacy()

    # 2. 目录设置
    cfg = load_config()
    if config_problems(cfg):
        # 第一次用（或者目录被挪走了）：先让用户把目录指定清楚，再进主界面
        dialog = SetupDialog(root, cfg, first_run=True)
        root.wait_window(dialog)
        saved = dialog.saved
        if not saved:
            messagebox.showinfo("还没设置完",
                                "没有指定素材目录和仓库目录，程序先退出。\n"
                                "下次启动会再问你一次。", parent=root)
        root.destroy()
        if not saved:
            return
        cfg = load_config()
    else:
        root.destroy()

    apply_config(cfg)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
