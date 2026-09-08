#!/usr/bin/env python3
"""终端 Markdown 幻灯片播放器 —— jyy 风格，带目录导航。

用法 (每讲一个文件, 支持多文件):
    python3 slides.py 01-KVM概述.slides 02-安装KVM.slides
    python3 slides.py *.slides

启动即进入目录页: 每个文件的 # 是章节(讲)、## 是小节, 两级缩进,
光标挑行回车进入, 阅读时按 Esc / t 随时返回目录。

格式 (Markdown + 少量扩展, 纯文本, 可直接 git 管理):
    # 标题          章节 (目录一级, 黄色粗体, 与正文同页)
    ## 标题         小节 (目录二级, 粗体, 与正文同页)
    ### 标题        三级标题 (左对齐加粗, 不进目录)
    ---             单独一行: 分页
    > 引用          引用块 (灰色 + 左侧竖线)
    - / 1.          无序 / 有序列表
    **加粗**  *斜体*  `行内代码`  ~~删除线~~
    [文字](url)     链接
    {r}..{/}        显式上色: r红 g绿 y黄 b蓝 m品红 c青 k灰 w白
    ``` 围栏        代码块 (可带语言: ```c)

目录模式按键:
    j / k          上下选择目录项
    Enter / Tab    进入选中项 (l / → 也可)
    q              退出

阅读模式按键:
    h / l          光标左右移动
    j / k          光标上下移动
    Tab / Enter    打开光标所在的链接
    ←/→ (方向键)   翻页 (↑/↓ 也可)     空格  下一页
    g / G          首页 / 末页
    Esc / t        返回目录
    q              退出
"""

import os
import re
import sys
import select
import signal
import shutil
import subprocess
import termios
import tty
import unicodedata
from collections import namedtuple

# ---- ANSI ----
RESET = "\033[0m"
CLEAR = "\033[2J\033[H"
SAVE = "\0337"          # DEC 保存光标
RESTORE = "\0338"       # DEC 恢复光标

PALETTE = {
    "r": "31", "g": "32", "y": "33", "b": "34",
    "m": "35", "c": "36", "w": "37", "k": "90",
}

ST_BOLD = "\033[1m"
ST_ITALIC = "\033[3m"
ST_STRIKE = "\033[9m"
ST_DIM = "\033[2m"
ST_CODE = "\033[36m"
ST_LINK = "\033[4m\033[34m"

Cell = namedtuple("Cell", "ch style link")


def dim(s):
    return f"\033[2m{s}{RESET}"


def wc(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def vwidth(s):
    return sum(wc(c) for c in s)


# ---- 行内 Markdown 解析 (返回 (文字, 样式前缀, 链接id) 段列表) ----
TOKEN_RE = re.compile(
    r"\[(?P<linktext>[^\]]+)\]\((?P<linkurl>[^)\s]+)\)"
    r"|`(?P<code>[^`]+)`"
    r"|~~(?P<strike>.+?)~~"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\*(?P<ital>[^*\s][^*]*?)\*"
    r"|(?<![\w*])_(?P<unders>[^_]+)_(?![\w*])"
    r"|\{(?P<color>\w)\}(?P<colortext>.+?)\{/\}"
)


def parse_inline(s, urls, style=""):
    segs = []
    i, n = 0, len(s)
    while i < n:
        m = TOKEN_RE.match(s, i)
        if not m:
            nxt = TOKEN_RE.search(s, i)
            end = nxt.start() if nxt else n
            if end > i:
                segs.append((s[i:end], style, None))
            i = end
            continue
        if m.group("linktext") is not None:
            idx = len(urls)
            urls.append(m.group("linkurl"))
            inner = parse_inline(m.group("linktext"), urls, style + ST_LINK)
            for t, st, _ in inner:
                segs.append((t, st, idx))
        elif m.group("code") is not None:
            segs.append((m.group("code"), style + ST_CODE, None))
        elif m.group("strike") is not None:
            segs.extend(parse_inline(m.group("strike"), urls, style + ST_STRIKE))
        elif m.group("bold") is not None:
            segs.extend(parse_inline(m.group("bold"), urls, style + ST_BOLD))
        elif m.group("ital") is not None:
            segs.extend(parse_inline(m.group("ital"), urls, style + ST_ITALIC))
        elif m.group("unders") is not None:
            segs.extend(parse_inline(m.group("unders"), urls, style + ST_ITALIC))
        elif m.group("color") is not None:
            code = PALETTE.get(m.group("color"), "0")
            segs.extend(parse_inline(m.group("colortext"), urls, style + f"\033[{code}m"))
        i = m.end()

    merged = []
    for seg in segs:
        if merged and merged[-1][1] == seg[1] and merged[-1][2] == seg[2]:
            t, st, l = merged[-1]
            merged[-1] = (t + seg[0], st, l)
        else:
            merged.append(seg)
    return merged


def cells_from_segments(segs):
    cells = []
    for text, style, link in segs:
        for ch in text:
            cells.append(Cell(ch, style, link))
    return cells


def pad_row(cells, width):
    w = sum(wc(c.ch) for c in cells)
    if w < width:
        cells = cells + [Cell(" ", "", None)] * (width - w)
    return cells


def build_grid(lines, width, urls):
    rows = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            cells = [Cell(ch, ST_DIM, None) for ch in "│ "]
            cells += [Cell(ch, "\033[32m", None) for ch in line]
            rows.append(pad_row(cells, width))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            segs = parse_inline(m.group(2), urls)
            if level == 1:
                segs = [(t, st + ST_BOLD + "\033[33m", l) for t, st, l in segs]
            else:
                segs = [(t, st + ST_BOLD, l) for t, st, l in segs]
            body = cells_from_segments(segs)
            if level <= 2:
                total = sum(wc(c.ch) for c in body)
                left = (width - total) // 2
                cells = [Cell(" ", "", None)] * max(left, 0) + body
            else:
                cells = body
            rows.append(pad_row(cells, width))
            continue
        if line.startswith("> "):
            segs = parse_inline(line[2:], urls)
            cells = [Cell("│", ST_DIM, None), Cell(" ", ST_DIM, None)]
            cells += cells_from_segments(segs)
            rows.append(pad_row(cells, width))
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            indent, marker, text = m.group(1), m.group(2), m.group(3)
            bullet = "• " if marker in ("-", "*", "+") else marker + " "
            cells = [Cell(ch, "", None) for ch in (indent + bullet)]
            cells += cells_from_segments(parse_inline(text, urls))
            rows.append(pad_row(cells, width))
            continue
        if re.match(r"^\s*(\*{3,}|_{3,})\s*$", line):
            rows.append([Cell("─", ST_DIM, None)] * width)
            continue
        cells = cells_from_segments(parse_inline(line, urls))
        rows.append(pad_row(cells, width))
    return rows


def render_toc(toc, width, cursor_row, top=0, height=None):
    if height is None:
        height = len(toc) - top
    lines = []
    for i in range(top, min(top + height, len(toc))):
        level, title, _ = toc[i]
        indent = "  " * level
        base = ST_BOLD + "\033[33m" if level == 0 else ST_BOLD
        segs = parse_inline(title, [], base)
        cells = [Cell(ch, "", None) for ch in indent] + cells_from_segments(segs)
        cells = pad_row(cells, width)
        buf = ["\033[7m"] if i == cursor_row else []
        prev = None
        for cell in cells:
            if cell.style != prev:
                buf.append(cell.style)
                prev = cell.style
            buf.append(cell.ch)
        buf.append(RESET)
        lines.append("".join(buf))
    return "\n".join(lines)


def emit_rows(rows, cursor):
    lines = []
    for r, row in enumerate(rows):
        buf = []
        prev = None
        for c, cell in enumerate(row):
            if cursor is not None and r == cursor[0] and c == cursor[1]:
                buf.append(cell.style + "\033[7m" + cell.ch + "\033[0m")
                prev = None
            else:
                if cell.style != prev:
                    buf.append(cell.style)
                    prev = cell.style
                buf.append(cell.ch)
        lines.append("".join(buf) + RESET)
    return "\n".join(lines)


def link_at(rows, cur):
    r, c = cur
    if 0 <= r < len(rows) and 0 <= c < len(rows[r]):
        return rows[r][c].link
    return None


def parse(text):
    pages, toc = [], []
    cur = []
    in_code = False

    def flush():
        nonlocal cur
        while cur and not cur[0].strip():
            cur.pop(0)
        while cur and not cur[-1].strip():
            cur.pop()
        if cur:
            pages.append(cur)
            cur = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            cur.append(line)
            continue
        if in_code:
            cur.append(line)
            continue
        if line.strip() == "---":
            flush()
            continue
        m = re.match(r"^(#{1,2})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1)) - 1  # 0 章节 / 1 小节
            cur.append(line)              # 标题作为当前页第一行, 与正文同页
            toc.append((level, m.group(2), len(pages)))
            continue
        cur.append(line)
    flush()
    return pages, toc


def load(files):
    all_pages, all_toc = [], []
    for path in files:
        with open(path, encoding="utf-8") as f:
            pages, toc = parse(f.read())
        offset = len(all_pages)
        all_pages.extend(pages)
        for level, title, idx in toc:
            all_toc.append((level, title, idx + offset))
    return all_pages, all_toc


def open_url(url):
    if "://" not in url and not url.startswith("mailto:"):
        url = "https://" + url
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


# ---- 终端交互 ----
def draw_status(pi, n_pages, cur, cur_link, urls):
    t = shutil.get_terminal_size()
    H, W = t.lines, t.columns
    hints = "   ".join(["h/l 左右", "j/k 上下", "Tab 链接", "←/→ 翻页", "Esc 目录", "q 退出"])
    if cur_link is not None and 0 <= cur_link < len(urls):
        u = urls[cur_link]
        u = u if len(u) <= 40 else u[:40] + "…"
        left = f"  [链接 {cur_link + 1}/{len(urls)}] {u}   {hints}"
    else:
        left = f"  [{cur[0] + 1},{cur[1] + 1}]   " + hints
    right = f"{pi + 1}/{n_pages}"
    pad = W - vwidth(left) - vwidth(right)
    if pad < 1:
        pad = 1
    sys.stdout.write(f"{SAVE}\033[{H};1H\033[2K" + dim(left + " " * pad + right) + RESTORE)
    sys.stdout.flush()


def draw_toc_status(n_toc, toc_cur):
    t = shutil.get_terminal_size()
    H, W = t.lines, t.columns
    left = "  目录 " + f"[{toc_cur + 1}/{n_toc}]   " + "   ".join(["j/k 选择", "回车 进入", "q 退出"])
    right = "目录"
    pad = W - vwidth(left) - vwidth(right)
    if pad < 1:
        pad = 1
    sys.stdout.write(f"{SAVE}\033[{H};1H\033[2K" + dim(left + " " * pad + right) + RESTORE)
    sys.stdout.flush()


def read_key():
    try:
        b = os.read(0, 1)
    except OSError:
        return None
    if not b:
        return None
    if b != b"\x1b":
        return b
    seq = b"\x1b"
    while True:
        r, _, _ = select.select([0], [], [], 0.03)
        if not r:
            break
        c = os.read(0, 1)
        if not c:
            break
        seq += c
        if len(seq) >= 8:
            break
    return seq


PAGE_NEXT = {b" ", b"\x1b[C", b"\x1b[B", b"\x1b[6~"}
PAGE_PREV = {b"\x1b[D", b"\x1b[A", b"\x1b[5~"}
LINK_OPEN = {b"\t", b"\r", b"\n"}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("错误: 需要在真实终端中运行 (不能重定向 stdin/stdout)")
        sys.exit(1)

    pages, toc = load(sys.argv[1:])
    if not pages:
        print("错误: 没有解析到任何页面")
        sys.exit(1)

    n_pages = len(pages)
    n_toc = len(toc)
    mode = "toc"            # "toc" 目录 / "view" 阅读
    pi = 0                  # 全局页索引
    toc_cur = 0             # 目录光标行
    toc_top = 0             # 目录可视窗口起始行
    cur = [0, 0]            # view 模式自由光标 [行, 单元格]
    rows = []
    urls = []
    cur_link = None

    def clamp():
        if not rows:
            cur[:] = [0, 0]
            return
        cur[0] = max(0, min(cur[0], len(rows) - 1))
        cur[1] = max(0, min(cur[1], len(rows[cur[0]]) - 1))

    def draw_view():
        nonlocal rows, urls, cur_link
        width = shutil.get_terminal_size().columns
        urls = []
        rows = build_grid(pages[pi], width, urls)
        clamp()
        cur_link = link_at(rows, tuple(cur))
        sys.stdout.write(CLEAR)
        sys.stdout.write(emit_rows(rows, tuple(cur)))
        draw_status(pi, n_pages, tuple(cur), cur_link, urls)

    def draw_toc():
        nonlocal toc_top
        width = shutil.get_terminal_size().columns
        height = max(1, shutil.get_terminal_size().lines - 1)
        if toc_cur < toc_top:
            toc_top = toc_cur
        elif toc_cur >= toc_top + height:
            toc_top = toc_cur - height + 1
        toc_top = max(0, min(toc_top, max(0, n_toc - height)))
        sys.stdout.write(CLEAR)
        sys.stdout.write(render_toc(toc, width, toc_cur, toc_top, height))
        draw_toc_status(n_toc, toc_cur)

    def draw():
        draw_toc() if mode == "toc" else draw_view()

    resized = [False]
    signal.signal(signal.SIGWINCH, lambda *a: resized.__setitem__(0, True))

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        draw()
        while True:
            r, _, _ = select.select([0], [], [], 0.5)
            if resized[0]:
                resized[0] = False
                draw()
                continue
            if not r:
                continue
            key = read_key()
            if key is None:
                continue

            if key in (b"q", b"\x03"):
                break

            if mode == "toc":
                if key == b"j":
                    toc_cur = min(toc_cur + 1, n_toc - 1)
                    draw()
                elif key == b"k":
                    toc_cur = max(toc_cur - 1, 0)
                    draw()
                elif key in (b"\r", b"\n", b"\t", b"l", b"\x1b[C"):
                    pi = toc[toc_cur][2]
                    cur[:] = [0, 0]
                    mode = "view"
                    draw()
            else:
                if key in (b"\x1b", b"t"):
                    mode = "toc"
                    draw()
                elif key == b"h":
                    cur[1] -= 1
                    clamp()
                    draw()
                elif key == b"l":
                    cur[1] += 1
                    clamp()
                    draw()
                elif key == b"k":
                    cur[0] -= 1
                    clamp()
                    draw()
                elif key == b"j":
                    cur[0] += 1
                    clamp()
                    draw()
                elif key in LINK_OPEN:
                    if cur_link is not None and 0 <= cur_link < len(urls):
                        open_url(urls[cur_link])
                elif key == b"g":
                    pi = 0
                    cur[:] = [0, 0]
                    draw()
                elif key == b"G":
                    pi = n_pages - 1
                    cur[:] = [0, 0]
                    draw()
                elif key in PAGE_NEXT:
                    if pi + 1 < n_pages:
                        pi += 1
                        cur[:] = [0, 0]
                        draw()
                elif key in PAGE_PREV:
                    if pi > 0:
                        pi -= 1
                        cur[:] = [0, 0]
                        draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(RESET + "\033[0m\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
