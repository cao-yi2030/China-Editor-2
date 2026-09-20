# -*- coding: utf-8 -*-
import curses
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import zlib

# 专有 .cn 文档格式：zlib 压缩 + 密钥流混淆。
# 保存为 .cn 的文件是二进制乱码，其他编辑器无法打开，只有本编辑器能读。
CN_MAGIC = b"CNDOC\x00\x1a"
CN_VERSION = 1
CN_EXT = ".cn"
CN_DEFAULT_KEY = b"china-editor-cn-doc-v1"


def _cn_rand_salt():
    try:
        return random.randbytes(16)
    except AttributeError:
        return bytes(random.getrandbits(8) for _ in range(16))


def _cn_keystream(key, salt, length):
    out = bytearray()
    n = 0
    while len(out) < length:
        out += hashlib.sha256(salt + key + n.to_bytes(4, "little")).digest()
        n += 1
    return bytes(out[:length])


def cn_encode(lines):
    meta = {
        "app": "ChinaEditor",
        "format": "cn",
        "version": CN_VERSION,
        "created": time.time(),
        "modified": time.time(),
        "lines": lines,
    }
    data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    body = zlib.compress(data, 9)
    salt = _cn_rand_salt()
    key = hashlib.sha256(CN_DEFAULT_KEY).digest()
    ks = _cn_keystream(key, salt, len(body))
    payload = bytes(a ^ b for a, b in zip(body, ks))
    return CN_MAGIC + bytes([CN_VERSION, 0]) + salt + payload


def cn_decode(raw):
    if not raw.startswith(CN_MAGIC) or len(raw) < len(CN_MAGIC) + 2 + 16:
        raise ValueError("不是有效的 .cn 文件")
    salt = raw[len(CN_MAGIC) + 2:len(CN_MAGIC) + 2 + 16]
    payload = raw[len(CN_MAGIC) + 2 + 16:]
    key = hashlib.sha256(CN_DEFAULT_KEY).digest()
    ks = _cn_keystream(key, salt, len(payload))
    body = bytes(a ^ b for a, b in zip(payload, ks))
    try:
        data = zlib.decompress(body)
        meta = json.loads(data.decode("utf-8"))
    except Exception:
        raise ValueError("文件损坏或无法解码")
    lines = meta.get("lines")
    if not isinstance(lines, list):
        raise ValueError("文档数据无效")
    return [str(x) for x in lines]


def is_word_char(ch):
    return ch.isalnum() or ch == '_'


def is_blank(ch):
    return ch in ' \t\n'


def cell_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


class VimEditor:
    """一个尽可能接近 Vim 的终端编辑器"""

    def __init__(self):
        self.screen = curses.initscr()
        self.screen.clear()
        self.rows, self.cols = self.screen.getmaxyx()

        self.text = [""]
        self.filename = None
        self.saved_text = [""]
        self.cursor_y = 0
        self.cursor_x = 0

        self.mode = "normal"          # normal / insert / command / search
        self.visual_active = False
        self.visual_linewise = False
        self.visual_start = (0, 0)
        self.replace_mode = False

        self.counter = ""             # 数字前缀
        self.prefix = ""              # g 前缀
        self.operator = None          # 待执行的 d/y/c/</> 操作
        self.find_pending = False
        self.find_key = None
        self.find_count = 1
        self.last_find = None         # (char, key, count)

        self.show_line_numbers = True
        self.auto_indent = True
        self.shiftwidth = 4

        self.search_pattern = None
        self.search_results = []
        self.search_index = -1
        self.search_direction = 1
        self.search_forward = True
        self.search_query = ""

        self.clipboard = []
        self.clipboard_linewise = False

        self.history = []
        self.redo_history = []

        self.dot_repeat = None

        self.command = ""
        self.command_history = []
        self.command_history_index = -1

        self.message = ""
        self.top_line = 0
        self.view_x = 0
        self.line_num_w = 0
        self.running = True

        self.dialog_active = False
        self.dialog_mode = "open"       # open / save
        self.dialog_dir = os.getcwd()
        self.dialog_files = []
        self.dialog_sel = 0
        self.dialog_input = ""

        curses.noecho()
        curses.cbreak()
        self.screen.keypad(True)
        try:
            curses.curs_set(1)
        except Exception:
            pass

    # ---------------------------------------------------------------- 运行
    def run(self, filename=None):
        if filename:
            if os.path.exists(filename):
                self.open_file(filename)
            else:
                self.filename = filename
                self.message = "新文件: " + filename
        try:
            while self.running:
                try:
                    self.draw()
                except curses.error:
                    pass
                key = self.getkey()
                self.handle_input(key)
        finally:
            self.cleanup()

    def cleanup(self):
        try:
            curses.nocbreak()
            self.screen.keypad(False)
            curses.echo()
            curses.endwin()
        except Exception:
            pass

    def getkey(self):
        try:
            key = self.screen.get_wch()
        except curses.error:
            return -1
        if isinstance(key, str) and len(key) == 1 and ord(key) < 32:
            return ord(key)
        return key

    # ---------------------------------------------------------------- 输入分发
    def handle_input(self, key):
        if key == -1:
            return
        if key == 19 and not self.dialog_active:      # Ctrl+S 保存
            self.handle_ctrl_s()
            return
        if key == 15 and not self.dialog_active:      # Ctrl+O 打开
            self.handle_ctrl_o()
            return
        if self.dialog_active:
            self.handle_dialog_input(key)
            return
        if self.find_pending:
            self.handle_find_char(key)
        elif self.mode == "command":
            self.handle_command_mode(key)
        elif self.mode == "search":
            self.handle_search_mode(key)
        elif self.mode == "insert":
            self.handle_insert_mode(key)
        elif self.visual_active:
            self.handle_visual_mode(key)
        elif self.operator:
            self.handle_operator_mode(key)
        else:
            self.handle_normal_mode(key)

    # ---------------------------------------------------------------- 文件对话框
    def handle_ctrl_s(self):
        if self.filename:
            self.save_file()
        else:
            self.open_dialog("save")

    def handle_ctrl_o(self):
        self.open_dialog("open")

    def open_dialog(self, mode):
        self.dialog_mode = mode
        self.dialog_active = True
        self.dialog_input = ""
        self.dialog_sel = 0
        if mode == "open" and self.filename:
            self.dialog_dir = os.path.dirname(os.path.abspath(self.filename))
        else:
            self.dialog_dir = os.getcwd()
        self.update_dialog_list()

    def update_dialog_list(self):
        try:
            items = sorted(os.listdir(self.dialog_dir))
        except OSError:
            items = []
        dirs = [n + "/" for n in items if os.path.isdir(os.path.join(self.dialog_dir, n))]
        files = [n for n in items if not os.path.isdir(os.path.join(self.dialog_dir, n))]
        at_root = (os.path.dirname(self.dialog_dir) == self.dialog_dir)
        self.dialog_files = []
        if not at_root:
            self.dialog_files.append("../")
        self.dialog_files += dirs + files
        if at_root:
            self.dialog_files += self.available_drives()
        if self.dialog_sel >= len(self.dialog_files):
            self.dialog_sel = max(0, len(self.dialog_files) - 1)

    def available_drives(self):
        drives = []
        if os.name == "nt":
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if os.path.exists(letter + ":\\"):
                    drives.append(letter + ":/")
        return drives

    def handle_dialog_input(self, key):
        if key == 27:
            self.dialog_active = False
            return
        if key == curses.KEY_UP:
            if self.dialog_sel > 0:
                self.dialog_sel -= 1
            return
        if key == curses.KEY_DOWN:
            if self.dialog_sel < len(self.dialog_files) - 1:
                self.dialog_sel += 1
            return
        if key in (10, 13, curses.KEY_ENTER):
            if self.dialog_input.strip():
                path = os.path.join(self.dialog_dir, self.dialog_input.strip())
                if os.path.isdir(path):
                    self.dialog_dir = path
                    self.dialog_sel = 0
                    self.dialog_input = ""
                    self.update_dialog_list()
                    return
                self.dialog_active = False
                if self.dialog_mode == "save":
                    self.save_file(path)
                else:
                    self.open_file(path)
                return
            if not self.dialog_files:
                return
            entry = self.dialog_files[self.dialog_sel]
            if entry == "../":
                self.dialog_dir = os.path.dirname(self.dialog_dir)
                self.dialog_sel = 0
                self.update_dialog_list()
            elif entry.endswith("/"):
                self.dialog_dir = os.path.join(self.dialog_dir, entry)
                self.dialog_sel = 0
                self.update_dialog_list()
            else:
                path = os.path.join(self.dialog_dir, entry)
                self.dialog_active = False
                if self.dialog_mode == "save":
                    self.save_file(path)
                else:
                    self.open_file(path)
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.dialog_input = self.dialog_input[:-1]
            return
        if isinstance(key, str):
            self.dialog_input += key

    def draw_dialog(self):
        rows, cols = self.rows, self.cols
        dh = min(rows - 6, 20)
        dw = min(cols - 6, 70)
        if dh < 8 or dw < 30:
            return
        sy = (rows - dh) // 2
        sx = (cols - dw) // 2
        self.screen.clear()
        title = "文件选择器 - " + ("保存" if self.dialog_mode == "save" else "打开")
        try:
            for x in range(dw):
                self.screen.addstr(sy, sx + x, "-")
                self.screen.addstr(sy + dh - 1, sx + x, "-")
            for y in range(1, dh - 1):
                self.screen.addstr(sy + y, sx, "|")
                self.screen.addstr(sy + y, sx + dw - 1, "|")
            self.screen.addstr(sy, sx, "+")
            self.screen.addstr(sy, sx + dw - 1, "+")
            self.screen.addstr(sy + dh - 1, sx, "+")
            self.screen.addstr(sy + dh - 1, sx + dw - 1, "+")
            self.screen.addstr(sy + 1, sx + 2, title, curses.A_BOLD)
            dirdisp = self.truncate_cells("目录: " + self.dialog_dir, dw - 4)
            self.screen.addstr(sy + 2, sx + 2, dirdisp)
            list_h = dh - 7
            list_top = sy + 3
            display_start = max(0, self.dialog_sel - list_h + 1)
            for i in range(display_start, min(len(self.dialog_files), display_start + list_h)):
                y = list_top + (i - display_start)
                entry = self.truncate_cells(self.dialog_files[i], dw - 4)
                if i == self.dialog_sel:
                    self.screen.addstr(y, sx + 2, entry, curses.A_REVERSE)
                else:
                    self.screen.addstr(y, sx + 2, entry)
            if self.dialog_mode == "save":
                inp = self.truncate_cells("文件名: " + self.dialog_input, dw - 4)
                self.screen.addstr(sy + dh - 3, sx + 2, inp)
            hint = self.truncate_cells("Enter:打开  Esc:取消  ↑/↓:移动  直接输入路径(如 D:\\ )按Enter", dw - 4)
            self.screen.addstr(sy + dh - 2, sx + 2, hint)
            self.screen.refresh()
        except curses.error:
            pass

    # ---------------------------------------------------------------- 文本工具
    def flat_text(self):
        return "\n".join(self.text)

    def lines_from_flat(self, s):
        if s == "":
            self.text = [""]
        else:
            self.text = s.split("\n")

    def yx_to_flat(self, y, x):
        idx = 0
        for i in range(min(y, len(self.text))):
            idx += len(self.text[i]) + 1
        idx += x
        return min(idx, len(self.flat_text()))

    def flat_to_yx(self, idx):
        s = self.flat_text()
        n = len(s)
        idx = min(max(idx, 0), n)
        if n == 0:
            return 0, 0
        y = s.count("\n", 0, idx)
        last_nl = s.rfind("\n", 0, idx)
        x = idx - (last_nl + 1)
        if y >= len(self.text):
            return len(self.text) - 1, len(self.text[-1])
        return y, x

    def char_cells(self, s):
        return sum(cell_width(ch) for ch in s)

    def truncate_cells(self, s, maxc):
        w = 0
        for i, ch in enumerate(s):
            cw = cell_width(ch)
            if w + cw > maxc:
                return s[:i]
            w += cw
        return s

    def clamp_cursor(self):
        if not self.text:
            self.text = [""]
        self.cursor_y = min(max(self.cursor_y, 0), len(self.text) - 1)
        self.cursor_x = min(max(self.cursor_x, 0), len(self.text[self.cursor_y]))

    def leading_ws(self, line):
        ws = ""
        for ch in line:
            if ch in ' \t':
                ws += ch
            else:
                break
        return ws

    # ---------------------------------------------------------------- 单词移动
    def flat_skip_token(self, s, i):
        n = len(s)
        if i >= n:
            return n
        c = s[i]
        if is_word_char(c):
            while i < n and is_word_char(s[i]):
                i += 1
        elif is_blank(c):
            while i < n and is_blank(s[i]):
                i += 1
        else:
            while i < n and not is_word_char(s[i]) and not is_blank(s[i]):
                i += 1
        return i

    def flat_w(self, idx, count):
        s = self.flat_text()
        n = len(s)
        i = idx
        for _ in range(count):
            if i >= n:
                return None
            i = self.flat_skip_token(s, i)
            while i < n and is_blank(s[i]):
                i += 1
            if i >= n:
                return None
        return i

    def flat_W(self, idx, count):
        s = self.flat_text()
        n = len(s)
        i = idx
        for _ in range(count):
            if i >= n:
                return None
            while i < n and not is_blank(s[i]):
                i += 1
            while i < n and is_blank(s[i]):
                i += 1
            if i >= n:
                return None
        return i

    def flat_e(self, idx, count):
        s = self.flat_text()
        n = len(s)
        i = idx
        for _ in range(count):
            while i < n and is_blank(s[i]):
                i += 1
            if i >= n:
                return None
            i = self.flat_skip_token(s, i) - 1
            if i < 0:
                return None
        return i

    def flat_E(self, idx, count):
        s = self.flat_text()
        n = len(s)
        i = idx
        for _ in range(count):
            while i < n and is_blank(s[i]):
                i += 1
            if i >= n:
                return None
            while i < n and not is_blank(s[i]):
                i += 1
            i -= 1
            if i < 0:
                return None
        return i

    def flat_b(self, idx, count):
        s = self.flat_text()
        i = idx
        for _ in range(count):
            if i <= 0:
                return None
            while i > 0 and is_blank(s[i - 1]):
                i -= 1
            if i <= 0:
                return None
            c = s[i - 1]
            if is_word_char(c):
                while i > 0 and is_word_char(s[i - 1]):
                    i -= 1
            else:
                while i > 0 and not is_word_char(s[i - 1]) and not is_blank(s[i - 1]):
                    i -= 1
        return i

    def flat_B(self, idx, count):
        s = self.flat_text()
        i = idx
        for _ in range(count):
            if i <= 0:
                return None
            while i > 0 and is_blank(s[i - 1]):
                i -= 1
            if i <= 0:
                return None
            while i > 0 and not is_blank(s[i - 1]):
                i -= 1
        return i

    def word_start_flat(self, idx):
        s = self.flat_text()
        n = len(s)
        if idx >= n:
            idx = max(0, n - 1)
        if is_blank(s[idx]):
            while idx < n and is_blank(s[idx]):
                idx += 1
            if idx >= n:
                return None
            return idx
        c = s[idx]
        if is_word_char(c):
            while idx > 0 and is_word_char(s[idx - 1]):
                idx -= 1
        else:
            while idx > 0 and not is_word_char(s[idx - 1]) and not is_blank(s[idx - 1]):
                idx -= 1
        return idx

    def word_end_flat(self, idx):
        s = self.flat_text()
        n = len(s)
        if idx >= n:
            return None
        if is_blank(s[idx]):
            while idx < n and is_blank(s[idx]):
                idx += 1
            if idx >= n:
                return None
        return self.flat_skip_token(s, idx)

    # ---------------------------------------------------------------- 光标移动
    def move_char(self, dx, count=1):
        for _ in range(count):
            if dx < 0:
                if self.cursor_x > 0:
                    self.cursor_x -= 1
            else:
                if self.cursor_x < len(self.text[self.cursor_y]):
                    self.cursor_x += 1

    def move_line(self, dy):
        y = self.cursor_y + dy
        y = min(max(y, 0), len(self.text) - 1)
        self.cursor_y = y
        self.cursor_x = min(self.cursor_x, len(self.text[y]))

    def goto_line(self, n):
        n = min(max(int(n), 1), len(self.text))
        self.cursor_y = n - 1
        self.cursor_x = min(self.cursor_x, len(self.text[self.cursor_y]))

    def move_page(self, direction):
        vh = max(1, self.rows - 1)
        self.move_line(direction * vh)

    def move_half_page(self, direction):
        vh = max(1, (self.rows - 1) // 2)
        self.move_line(direction * vh)

    def do_motion_char(self, c, count=1):
        if c == 'h':
            self.move_char(-1, count)
        elif c == 'l' or c == ' ':
            self.move_char(1, count)
        elif c == 'j':
            self.move_line(count)
        elif c == 'k':
            self.move_line(-count)
        elif c == 'w':
            t = self.flat_w(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == 'W':
            t = self.flat_W(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == 'b':
            t = self.flat_b(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == 'B':
            t = self.flat_B(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == 'e':
            t = self.flat_e(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == 'E':
            t = self.flat_E(self.yx_to_flat(self.cursor_y, self.cursor_x), count)
            if t is not None:
                self.cursor_y, self.cursor_x = self.flat_to_yx(t)
        elif c == '0':
            self.cursor_x = 0
        elif c == '^':
            line = self.text[self.cursor_y]
            i = 0
            while i < len(line) and is_blank(line[i]):
                i += 1
            self.cursor_x = i
        elif c == '$':
            self.cursor_x = max(0, len(self.text[self.cursor_y]) - 1)
        elif c == 'G':
            self.goto_line(count)
        elif c == 'H':
            self.cursor_y = self.top_line
            self.clamp_cursor()
        elif c == 'M':
            self.cursor_y = min(self.top_line + (self.rows - 1) // 2, len(self.text) - 1)
            self.clamp_cursor()
        elif c == 'L':
            self.cursor_y = min(self.top_line + self.rows - 2, len(self.text) - 1)
            self.clamp_cursor()

    def navigate_find(self, ch, count, kind):
        line = self.text[self.cursor_y]
        if kind in ('f', 't'):
            i = line.find(ch, self.cursor_x + 1)
            k = 1
            while i != -1 and k < count:
                i = line.find(ch, i + 1)
                k += 1
            if i == -1:
                return False
            self.cursor_x = i - 1 if kind == 't' else i
            return True
        else:
            i = line.rfind(ch, 0, self.cursor_x)
            k = 1
            while i != -1 and k < count:
                i = line.rfind(ch, 0, i)
                k += 1
            if i == -1:
                return False
            self.cursor_x = i + 1 if kind == 'T' else i
            return True

    def repeat_find(self, opposite):
        if not self.last_find:
            self.message = "没有字符查找"
            return
        ch, fk, cnt = self.last_find
        if opposite:
            fk = {'f': 'F', 'F': 'f', 't': 'T', 'T': 't'}.get(fk, fk)
        if not self.navigate_find(ch, cnt, fk):
            self.message = "未找到字符"
        self.last_find = (ch, fk, cnt)

    def match_bracket(self):
        pairs = {'(': ')', '[': ']', '{': '}', ')': '(', ']': '[', '}': '{'}
        line = self.text[self.cursor_y]
        if self.cursor_x >= len(line):
            return
        ch = line[self.cursor_x]
        if ch not in pairs:
            return
        opp = pairs[ch]
        if ch in '([{':
            depth = 1
            y, x = self.cursor_y, self.cursor_x + 1
            while y < len(self.text):
                l = self.text[y]
                while x < len(l):
                    cc = l[x]
                    if cc == ch:
                        depth += 1
                    elif cc == opp:
                        depth -= 1
                        if depth == 0:
                            self.cursor_y, self.cursor_x = y, x
                            return
                    x += 1
                y += 1
                x = 0
        else:
            depth = 1
            y, x = self.cursor_y, self.cursor_x - 1
            while y >= 0:
                l = self.text[y]
                while x >= 0:
                    cc = l[x]
                    if cc == ch:
                        depth += 1
                    elif cc == opp:
                        depth -= 1
                        if depth == 0:
                            self.cursor_y, self.cursor_x = y, x
                            return
                    x -= 1
                y -= 1
                x = len(self.text[y]) - 1 if y >= 0 else -1

    # ---------------------------------------------------------------- 历史 / 撤销
    def save_state(self):
        if self.history and self.history[-1] == self.text:
            return
        self.history.append([l for l in self.text])
        if len(self.history) > 300:
            self.history.pop(0)

    def undo(self):
        if self.history:
            self.redo_history.append([l for l in self.text])
            self.text = [l for l in self.history.pop()]
            self.clamp_cursor()
            self.message = "已撤销"
        else:
            self.message = "没有可撤销的内容"

    def redo(self):
        if self.redo_history:
            self.history.append([l for l in self.text])
            self.text = [l for l in self.redo_history.pop()]
            self.clamp_cursor()
            self.message = "已重做"
        else:
            self.message = "没有可重做的内容"

    # ---------------------------------------------------------------- 模式切换
    def begin_insert(self, y, x):
        self.mode = "insert"
        self.cursor_y, self.cursor_x = y, x
        self.entry_yx = (y, x)
        try:
            curses.curs_set(2)
        except Exception:
            pass

    def enter_insert(self):
        self.save_state()
        self.begin_insert(self.cursor_y, self.cursor_x)

    def exit_insert(self):
        ftxt = self.flat_text()
        e_idx = self.yx_to_flat(self.entry_yx[0], self.entry_yx[1])
        x_idx = self.yx_to_flat(self.cursor_y, self.cursor_x)
        if x_idx > e_idx:
            self.dot_repeat = ('insert', ftxt[e_idx:x_idx])
        else:
            self.dot_repeat = None
        self.replace_mode = False
        self.mode = "normal"
        if self.cursor_x > 0:
            self.cursor_x -= 1
        self.clamp_cursor()
        try:
            curses.curs_set(1)
        except Exception:
            pass

    def enter_visual(self, linewise):
        self.visual_active = True
        self.visual_linewise = linewise
        self.visual_start = (self.cursor_y, self.cursor_x)
        self.mode = "visual"

    def exit_visual(self):
        self.visual_active = False
        self.visual_linewise = False
        self.mode = "normal"

    # ---------------------------------------------------------------- 普通模式
    def handle_normal_mode(self, key):
        self.message = ""
        if key == 27:
            self.counter = ""
            self.prefix = ""
            return
        if key == curses.KEY_LEFT:
            self.move_char(-1, 1)
            return
        if key == curses.KEY_RIGHT:
            self.move_char(1, 1)
            return
        if key == curses.KEY_UP:
            self.move_line(-1)
            return
        if key == curses.KEY_DOWN:
            self.move_line(1)
            return
        if key == curses.KEY_PPAGE:
            self.move_page(-1)
            return
        if key == curses.KEY_NPAGE:
            self.move_page(1)
            return
        if key == curses.KEY_HOME:
            self.cursor_x = 0
            return
        if key == curses.KEY_END:
            self.cursor_x = len(self.text[self.cursor_y])
            return
        if key == 2:       # Ctrl+B
            self.move_page(-1)
            return
        if key == 6:       # Ctrl+F
            self.move_page(1)
            return
        if key == 4:       # Ctrl+D
            self.move_half_page(1)
            return
        if key == 21:      # Ctrl+U
            self.move_half_page(-1)
            return
        if key == 18:      # Ctrl+R 重做
            self.redo()
            return
        if not isinstance(key, str) or not key.isprintable():
            return
        c = key
        if c.isdigit():
            if c == '0' and not self.counter:
                self.cursor_x = 0
                return
            self.counter += c
            return
        count = int(self.counter) if self.counter else 1
        self.counter = ""

        if self.prefix == 'g':
            self.prefix = ""
            if c == 'g':
                self.goto_line(count)
            elif c == 'G':
                self.goto_line(len(self.text))
            return
        if c == 'g':
            self.prefix = 'g'
            return

        if c in 'hjklwWeEbB0^$GHML':
            self.do_motion_char(c, count)
            return
        if c == ' ':
            self.move_char(1, count)
            return
        if c in 'fFtT':
            self.find_key = c
            self.find_count = count
            self.find_pending = True
            return
        if c == ';':
            self.repeat_find(False)
            return
        if c == ',':
            self.repeat_find(True)
            return
        if c == '%':
            self.match_bracket()
            return

        if c == 'i':
            self.enter_insert()
        elif c == 'I':
            line = self.text[self.cursor_y]
            i = 0
            while i < len(line) and is_blank(line[i]):
                i += 1
            self.cursor_x = i
            self.enter_insert()
        elif c == 'a':
            if self.cursor_x < len(self.text[self.cursor_y]):
                self.cursor_x += 1
            self.enter_insert()
        elif c == 'A':
            self.cursor_x = len(self.text[self.cursor_y])
            self.enter_insert()
        elif c == 'o':
            self.save_state()
            indent = self.leading_ws(self.text[self.cursor_y])
            self.text.insert(self.cursor_y + 1, indent)
            self.cursor_y += 1
            self.cursor_x = len(indent)
            self.begin_insert(self.cursor_y, self.cursor_x)
        elif c == 'O':
            self.save_state()
            indent = self.leading_ws(self.text[self.cursor_y])
            self.text.insert(self.cursor_y, indent)
            self.cursor_x = len(indent)
            self.begin_insert(self.cursor_y, self.cursor_x)
        elif c == 'x':
            self.save_state()
            self.do_delete_chars(count)
        elif c == 'X':
            self.save_state()
            removed = ""
            for _ in range(count):
                if self.cursor_x <= 0:
                    break
                line = self.text[self.cursor_y]
                removed = line[self.cursor_x - 1] + removed
                self.text[self.cursor_y] = line[:self.cursor_x - 1] + line[self.cursor_x:]
                self.cursor_x -= 1
            if removed:
                self.clipboard = [removed]
                self.clipboard_linewise = False
        elif c == 's':
            self.save_state()
            line = self.text[self.cursor_y]
            for _ in range(count):
                if self.cursor_x >= len(line):
                    break
                line = line[:self.cursor_x] + line[self.cursor_x + 1:]
            self.text[self.cursor_y] = line
            self.begin_insert(self.cursor_y, self.cursor_x)
        elif c == 'S':
            self.save_state()
            y2 = min(len(self.text), self.cursor_y + count)
            del self.text[self.cursor_y:y2]
            if not self.text:
                self.text = [""]
            self.begin_insert(self.cursor_y, 0)
        elif c == 'r':
            self.do_replace(count)
        elif c == 'R':
            self.replace_mode = True
            self.save_state()
            self.begin_insert(self.cursor_y, self.cursor_x)
        elif c in 'dcy':
            self.operator = c
        elif c in '><':
            self.operator = c
        elif c == 'D':
            self.save_state()
            line = self.text[self.cursor_y]
            self.clipboard = [line[self.cursor_x:]]
            self.clipboard_linewise = False
            self.text[self.cursor_y] = line[:self.cursor_x]
        elif c == 'C':
            self.save_state()
            line = self.text[self.cursor_y]
            self.text[self.cursor_y] = line[:self.cursor_x]
            self.begin_insert(self.cursor_y, self.cursor_x)
        elif c == 'Y':
            self.yank_lines(count)
        elif c == 'p':
            self.do_paste(False, count)
        elif c == 'P':
            self.do_paste(True, count)
        elif c == 'u':
            self.undo()
        elif c == '.':
            self.repeat_dot(count)
        elif c == 'J':
            self.save_state()
            for _ in range(count):
                if self.cursor_y >= len(self.text) - 1:
                    break
                cur = self.text[self.cursor_y]
                nxt = self.text[self.cursor_y + 1]
                self.text[self.cursor_y] = (cur.rstrip() + " " + nxt.lstrip()) if (cur and nxt) else (cur + nxt)
                self.text.pop(self.cursor_y + 1)
        elif c == '~':
            self.save_state()
            for _ in range(count):
                line = self.text[self.cursor_y]
                if self.cursor_x >= len(line):
                    break
                ch = line[self.cursor_x]
                nch = ch.upper() if ch.islower() else ch.lower()
                self.text[self.cursor_y] = line[:self.cursor_x] + nch + line[self.cursor_x + 1:]
                if self.cursor_x < len(self.text[self.cursor_y]) - 1:
                    self.cursor_x += 1
        elif c == '/':
            self.start_search(True)
        elif c == '?':
            self.start_search(False)
        elif c == 'n':
            self.search_next(count, False)
        elif c == 'N':
            self.search_next(count, True)
        elif c == '*':
            self.search_word(True)
        elif c == '#':
            self.search_word(False)
        elif c == ':':
            self.start_command()
        elif c == 'v':
            self.enter_visual(False)
        elif c == 'V':
            self.enter_visual(True)
        elif c == 'q':
            self.message = "宏录制未实现，请使用 :q 退出"
        else:
            self.message = "未知按键: " + c

    def do_replace(self, count):
        key = self.getkey()
        if key == 27:
            return
        if isinstance(key, str):
            ch = key
            self.save_state()
            for _ in range(count):
                line = self.text[self.cursor_y]
                if self.cursor_x >= len(line):
                    break
                self.text[self.cursor_y] = line[:self.cursor_x] + ch + line[self.cursor_x + 1:]
                if self.cursor_x < len(self.text[self.cursor_y]) - 1:
                    self.cursor_x += 1
            self.dot_repeat = ('r', ch)

    def do_delete_chars(self, count):
        line = self.text[self.cursor_y]
        removed = ""
        i = 0
        while i < count and self.cursor_x < len(line):
            removed += line[self.cursor_x]
            line = line[:self.cursor_x] + line[self.cursor_x + 1:]
            i += 1
        if removed:
            self.text[self.cursor_y] = line
            self.clipboard = [removed]
            self.clipboard_linewise = False
            self.dot_repeat = ('x', count)

    def delete_lines(self, count):
        y2 = min(len(self.text), self.cursor_y + count)
        n = y2 - self.cursor_y
        if n <= 0:
            return
        self.clipboard = self.text[self.cursor_y:y2]
        self.clipboard_linewise = True
        del self.text[self.cursor_y:y2]
        if not self.text:
            self.text = [""]
        self.cursor_y = min(self.cursor_y, len(self.text) - 1)
        self.cursor_x = 0
        self.dot_repeat = ('dd', count)
        self.message = "已删除 %d 行" % n

    def yank_lines(self, count):
        y2 = min(len(self.text), self.cursor_y + count)
        self.clipboard = self.text[self.cursor_y:y2]
        self.clipboard_linewise = True
        self.message = "已复制 %d 行" % (y2 - self.cursor_y)

    def change_lines(self, count):
        y2 = min(len(self.text), self.cursor_y + count)
        del self.text[self.cursor_y:y2]
        if not self.text:
            self.text = [""]
        self.begin_insert(self.cursor_y, 0)

    # ---------------------------------------------------------------- 操作符模式
    def handle_operator_mode(self, key):
        if key == 27:
            self.operator = None
            return
        if not isinstance(key, str) or not key.isprintable():
            self.operator = None
            return
        c = key
        if c.isdigit():
            if c == '0' and not self.counter:
                op = self.operator
                self.operator = None
                rng = self.op_range('0', 1)
                if rng:
                    self.apply_operator_result(op, rng, '0', 1)
                return
            self.counter += c
            return
        count = int(self.counter) if self.counter else 1
        self.counter = ""
        op = self.operator
        self.operator = None

        if c == op and op in 'dcy':
            if op == 'd':
                self.save_state()
                self.delete_lines(count)
            elif op == 'y':
                self.yank_lines(count)
            else:
                self.save_state()
                self.change_lines(count)
            return
        if c == op and op in '><':
            self.save_state()
            self.shift_range(self.cursor_y, min(len(self.text), self.cursor_y + count), op == '>')
            return
        if c in 'fFtT':
            self.find_key = c
            self.find_count = count
            self.find_pending = True
            self.operator = op
            return
        if op in '><':
            if c in ('j', 'k', 'g', 'G'):
                rng = self.op_range(c, count)
                if rng and rng.get('linewise'):
                    y1, y2 = rng['lines']
                else:
                    y1, y2 = self.cursor_y, min(len(self.text), self.cursor_y + count)
            elif c == '$':
                y1, y2 = self.cursor_y, self.cursor_y + count
            else:
                y1, y2 = self.cursor_y, min(len(self.text), self.cursor_y + count)
            self.save_state()
            self.shift_range(y1, y2, op == '>')
            return
        rng = self.op_range(c, count)
        if rng is None:
            self.message = "无法执行操作"
            return
        self.apply_operator_result(op, rng, c, count)

    def op_range(self, motion, count):
        cy, cx = self.cursor_y, self.cursor_x
        cur = self.yx_to_flat(cy, cx)
        if motion in ('j', 'k', 'gg', 'G'):
            if motion == 'j':
                return {'linewise': True, 'lines': (cy, min(len(self.text), cy + count))}
            if motion == 'k':
                return {'linewise': True, 'lines': (max(0, cy - count), cy + 1)}
            if motion == 'gg':
                return {'linewise': True, 'lines': (0, cy + 1)}
            if motion == 'G':
                return {'linewise': True, 'lines': (cy, len(self.text))}
        if motion == 'h':
            if cx <= 0:
                return None
            return {'linewise': False, 'chars': (cur - 1, cur)}
        if motion == 'l':
            if cx >= len(self.text[cy]):
                return None
            return {'linewise': False, 'chars': (cur, cur + 1)}
        if motion == 'w':
            t = self.flat_w(cur, count)
            if t is None:
                return None
            return {'linewise': False, 'chars': (cur, t)}
        if motion == 'W':
            t = self.flat_W(cur, count)
            if t is None:
                return None
            return {'linewise': False, 'chars': (cur, t)}
        if motion == 'e':
            t = self.flat_e(cur, count)
            if t is None:
                return None
            return {'linewise': False, 'chars': (cur, t + 1)}
        if motion == 'E':
            t = self.flat_E(cur, count)
            if t is None:
                return None
            return {'linewise': False, 'chars': (cur, t + 1)}
        if motion == 'b':
            t = self.flat_b(cur, count)
            if t is None or t >= cur:
                return None
            return {'linewise': False, 'chars': (t, cur)}
        if motion == 'B':
            t = self.flat_B(cur, count)
            if t is None or t >= cur:
                return None
            return {'linewise': False, 'chars': (t, cur)}
        if motion == '0':
            return {'linewise': False, 'chars': (self.yx_to_flat(cy, 0), cur)}
        if motion == '^':
            line = self.text[cy]
            i = 0
            while i < len(line) and is_blank(line[i]):
                i += 1
            return {'linewise': False, 'chars': (self.yx_to_flat(cy, i), cur)}
        if motion == '$':
            return {'linewise': False, 'chars': (cur, self.yx_to_flat(cy, len(self.text[cy])))}
        if motion in ('iw', 'iW', 'aw', 'aW'):
            s = self.word_start_flat(cur)
            e = self.word_end_flat(cur)
            if s is None or e is None:
                return None
            if motion in ('aw', 'aW'):
                ftxt = self.flat_text()
                t = e
                while t < len(ftxt) and ftxt[t] in ' \t':
                    t += 1
                if t > e:
                    e = t
            return {'linewise': False, 'chars': (s, e)}
        return None

    def op_find_range(self, ch, count, kind):
        cy, cx = self.cursor_y, self.cursor_x
        cur = self.yx_to_flat(cy, cx)
        line = self.text[cy]
        if kind in ('f', 't'):
            i = line.find(ch, cx + 1)
            k = 1
            while i != -1 and k < count:
                i = line.find(ch, i + 1)
                k += 1
            if i == -1:
                return None
            if kind == 'f':
                return {'linewise': False, 'chars': (cur, self.yx_to_flat(cy, i + 1))}
            return {'linewise': False, 'chars': (cur, self.yx_to_flat(cy, i))}
        else:
            i = line.rfind(ch, 0, cx)
            k = 1
            while i != -1 and k < count:
                i = line.rfind(ch, 0, i)
                k += 1
            if i == -1:
                return None
            if kind == 'F':
                return {'linewise': False, 'chars': (self.yx_to_flat(cy, i), cur)}
            return {'linewise': False, 'chars': (self.yx_to_flat(cy, i + 1), cur)}

    def apply_operator_result(self, op, rng, motion, count):
        if rng.get('linewise'):
            y1, y2 = rng['lines']
            if y1 >= y2:
                return
            if op == 'y':
                self.clipboard = self.text[y1:y2]
                self.clipboard_linewise = True
                self.message = "已复制 %d 行" % (y2 - y1)
                return
            self.save_state()
            self.clipboard = self.text[y1:y2]
            self.clipboard_linewise = True
            del self.text[y1:y2]
            if not self.text:
                self.text = [""]
            self.cursor_y = min(y1, len(self.text) - 1)
            self.cursor_x = 0
            if op == 'd':
                self.dot_repeat = ('dd', count)
                self.message = "已删除 %d 行" % (y2 - y1)
            elif op == 'c':
                self.begin_insert(self.cursor_y, 0)
            return
        s, e = rng['chars']
        if e <= s:
            return
        if op == 'y':
            self.clipboard = [self.flat_text()[s:e]]
            self.clipboard_linewise = False
            self.message = "已复制 %d 个字符" % (e - s)
            return
        self.save_state()
        removed = self.flat_cut(s, e)
        self.clipboard = [removed]
        self.clipboard_linewise = False
        y, x = self.flat_to_yx(s)
        self.cursor_y, self.cursor_x = y, x
        if op == 'd':
            if motion in ('iw', 'iW', 'aw', 'aW'):
                self.dot_repeat = ('d_motion', motion)
            self.message = "已删除 %d 个字符" % (e - s)
        elif op == 'c':
            self.begin_insert(self.cursor_y, self.cursor_x)

    def flat_cut(self, s, e):
        ftxt = self.flat_text()
        removed = ftxt[s:e]
        self.lines_from_flat(ftxt[:s] + ftxt[e:])
        return removed

    # ---------------------------------------------------------------- 查找字符
    def handle_find_char(self, key):
        self.find_pending = False
        fk = self.find_key
        cnt = self.find_count if self.find_count else 1
        self.find_key = None
        self.find_count = 1
        self.counter = ""
        if not isinstance(key, str) or not key.isprintable():
            self.message = "缺少字符"
            return
        ch = key
        if self.operator:
            op = self.operator
            self.operator = None
            rng = self.op_find_range(ch, cnt, fk)
            if rng:
                self.apply_operator_result(op, rng, 'f', cnt)
            else:
                self.message = "未找到字符"
        else:
            if not self.navigate_find(ch, cnt, fk):
                self.message = "未找到字符"
            else:
                self.last_find = (ch, fk, cnt)

    # ---------------------------------------------------------------- 插入模式
    def handle_insert_mode(self, key):
        if key == 27:
            self.exit_insert()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.insert_backspace()
        elif key == curses.KEY_DC:
            line = self.text[self.cursor_y]
            if self.cursor_x < len(line):
                self.text[self.cursor_y] = line[:self.cursor_x] + line[self.cursor_x + 1:]
        elif key in (10, 13, curses.KEY_ENTER):
            self.insert_newline()
        elif key == 9:
            self.insert_char("\t")
        elif key == curses.KEY_LEFT:
            if self.cursor_x > 0:
                self.cursor_x -= 1
        elif key == curses.KEY_RIGHT:
            if self.cursor_x < len(self.text[self.cursor_y]):
                self.cursor_x += 1
        elif key == curses.KEY_UP:
            if self.cursor_y > 0:
                self.cursor_y -= 1
                self.cursor_x = min(self.cursor_x, len(self.text[self.cursor_y]))
        elif key == curses.KEY_DOWN:
            if self.cursor_y < len(self.text) - 1:
                self.cursor_y += 1
                self.cursor_x = min(self.cursor_x, len(self.text[self.cursor_y]))
        elif key == curses.KEY_HOME:
            self.cursor_x = 0
        elif key == curses.KEY_END:
            self.cursor_x = len(self.text[self.cursor_y])
        elif key == 21:  # Ctrl+U 删除到行首
            line = self.text[self.cursor_y]
            self.text[self.cursor_y] = line[self.cursor_x:]
            self.cursor_x = 0
        elif isinstance(key, str):
            self.insert_char(key)

    def insert_char(self, ch):
        line = self.text[self.cursor_y]
        if self.replace_mode:
            if self.cursor_x < len(line):
                self.text[self.cursor_y] = line[:self.cursor_x] + ch + line[self.cursor_x + 1:]
            else:
                self.text[self.cursor_y] = line + ch
        else:
            self.text[self.cursor_y] = line[:self.cursor_x] + ch + line[self.cursor_x:]
        self.cursor_x += 1

    def insert_backspace(self):
        if self.cursor_x > 0:
            line = self.text[self.cursor_y]
            self.text[self.cursor_y] = line[:self.cursor_x - 1] + line[self.cursor_x:]
            self.cursor_x -= 1
        elif self.cursor_y > 0:
            prev = self.text[self.cursor_y - 1]
            cur = self.text[self.cursor_y]
            self.text[self.cursor_y - 1] = prev + cur
            self.text.pop(self.cursor_y)
            self.cursor_y -= 1
            self.cursor_x = len(prev)

    def insert_newline(self):
        line = self.text[self.cursor_y]
        indent = self.leading_ws(line) if self.auto_indent else ""
        self.text[self.cursor_y] = line[:self.cursor_x]
        self.text.insert(self.cursor_y + 1, indent + line[self.cursor_x:])
        self.cursor_y += 1
        self.cursor_x = len(indent)

    def insert_str(self, s):
        parts = s.split("\n")
        if len(parts) == 1:
            line = self.text[self.cursor_y]
            self.text[self.cursor_y] = line[:self.cursor_x] + parts[0] + line[self.cursor_x:]
            self.cursor_x += len(parts[0])
        else:
            line = self.text[self.cursor_y]
            self.text[self.cursor_y] = line[:self.cursor_x] + parts[0]
            tail = parts[-1] + line[self.cursor_x:]
            new_lines = parts[1:-1] + [tail]
            for i, ln in enumerate(new_lines):
                self.text.insert(self.cursor_y + 1 + i, ln)
            self.cursor_y += len(new_lines)
            self.cursor_x = len(parts[-1])

    # ---------------------------------------------------------------- 粘贴 / 重复
    def do_paste(self, before, count):
        if not self.clipboard:
            self.message = "剪贴板为空"
            return
        self.save_state()
        for _ in range(count):
            if self.clipboard_linewise:
                y = self.cursor_y + 1 if not before else self.cursor_y
                y = min(y, len(self.text))
                for j, ln in enumerate(self.clipboard):
                    self.text.insert(y + j, ln)
                self.cursor_y = y
                self.cursor_x = 0
            else:
                text = self.clipboard[0]
                if not before and self.cursor_x < len(self.text[self.cursor_y]):
                    self.cursor_x += 1
                self.insert_str(text)
        self.dot_repeat = ('p', before)

    def repeat_dot(self, count):
        if self.dot_repeat is None:
            self.message = "没有可重复的命令"
            return
        kind, data = self.dot_repeat
        if kind in ('insert', 'insert_multi'):
            for _ in range(count):
                self.save_state()
                self.insert_str(data)
        elif kind == 'x':
            for _ in range(count):
                self.save_state()
                self.do_delete_chars(data)
        elif kind == 'dd':
            for _ in range(count):
                self.save_state()
                self.delete_lines(data)
        elif kind == 'p':
            self.do_paste(data, 1)
        elif kind == 'r':
            for _ in range(count):
                self.save_state()
                self.replace_single(data)
        elif kind == 'd_motion':
            for _ in range(count):
                self.save_state()
                rng = self.op_range(data, 1)
                if rng and not rng.get('linewise'):
                    s, e = rng['chars']
                    if e > s:
                        removed = self.flat_cut(s, e)
                        self.clipboard = [removed]
                        self.clipboard_linewise = False

    def replace_single(self, ch):
        line = self.text[self.cursor_y]
        if self.cursor_x >= len(line):
            return
        self.text[self.cursor_y] = line[:self.cursor_x] + ch + line[self.cursor_x + 1:]
        if self.cursor_x < len(self.text[self.cursor_y]) - 1:
            self.cursor_x += 1

    # ---------------------------------------------------------------- 缩进
    def shift_range(self, y1, y2, right):
        sw = self.shiftwidth
        for i in range(y1, y2):
            line = self.text[i]
            if right:
                self.text[i] = " " * sw + line
                if i == y1:
                    self.cursor_x += sw
            else:
                j = 0
                while j < len(line) and j < sw and line[j] in ' \t':
                    j += 1
                self.text[i] = line[j:]
                if i == y1:
                    self.cursor_x = max(0, self.cursor_x - j)

    # ---------------------------------------------------------------- 搜索
    def start_search(self, forward):
        self.mode = "search"
        self.search_forward = forward
        self.search_query = ""
        self.message = ""

    def update_search_results(self):
        self.search_results = []
        if not self.search_pattern:
            return
        try:
            rx = re.compile(self.search_pattern)
        except re.error:
            return
        for i, line in enumerate(self.text):
            for m in rx.finditer(line):
                self.search_results.append((i, m.start()))

    def finish_search(self):
        if self.search_query:
            self.search_pattern = self.search_query
            self.search_direction = 1 if self.search_forward else -1
            self.update_search_results()
            if self.search_results:
                self.search_index = -1 if self.search_direction > 0 else len(self.search_results)
                self.search_next(1, False)
            else:
                self.message = "未找到: " + self.search_query
        self.mode = "normal"

    def search_next(self, count, reverse):
        if not self.search_results:
            self.message = "没有搜索结果"
            return
        n = len(self.search_results)
        direction = self.search_direction
        if reverse:
            direction = -direction
        self.search_index = (self.search_index + direction * count) % n
        y, x = self.search_results[self.search_index]
        self.cursor_y, self.cursor_x = y, x
        self.message = "匹配 %d/%d" % (self.search_index + 1, n)

    def word_under_cursor(self):
        line = self.text[self.cursor_y]
        if self.cursor_x >= len(line):
            return None
        cur = self.yx_to_flat(self.cursor_y, self.cursor_x)
        s = self.word_start_flat(cur)
        e = self.word_end_flat(cur)
        if s is None or e is None:
            return None
        return self.flat_text()[s:e]

    def search_word(self, forward):
        word = self.word_under_cursor()
        if not word:
            self.message = "光标处没有单词"
            return
        self.search_pattern = re.escape(word)
        self.search_forward = forward
        self.search_direction = 1 if forward else -1
        self.update_search_results()
        if self.search_results:
            self.search_index = -1 if self.search_direction > 0 else len(self.search_results)
            self.search_next(1, False)
        else:
            self.message = "未找到: " + word

    def handle_search_mode(self, key):
        if key == 27:
            self.mode = "normal"
            self.search_query = ""
            return
        if key in (10, 13, curses.KEY_ENTER):
            self.finish_search()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            self.search_pattern = self.search_query
            self.update_search_results()
        elif isinstance(key, str):
            self.search_query += key
            self.search_pattern = self.search_query
            self.update_search_results()

    # ---------------------------------------------------------------- 命令模式
    def start_command(self):
        self.mode = "command"
        self.command = ""
        self.command_history_index = -1

    def handle_command_mode(self, key):
        if key == 27:
            self.mode = "normal"
            self.command = ""
            return
        if key in (10, 13, curses.KEY_ENTER):
            cmd = self.command
            self.mode = "normal"
            self.command = ""
            self.execute_command(cmd)
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.command = self.command[:-1]
        elif key == curses.KEY_UP:
            if self.command_history:
                if self.command_history_index == -1:
                    self.command_history_index = len(self.command_history) - 1
                else:
                    self.command_history_index = max(0, self.command_history_index - 1)
                self.command = self.command_history[self.command_history_index]
        elif key == curses.KEY_DOWN:
            if self.command_history_index != -1:
                self.command_history_index += 1
                if self.command_history_index >= len(self.command_history):
                    self.command_history_index = -1
                    self.command = ""
                else:
                    self.command = self.command_history[self.command_history_index]
        elif isinstance(key, str):
            self.command += key

    def execute_command(self, cmd):
        if cmd.strip():
            self.command_history.append(cmd.strip())
        self.command_history_index = -1
        cmd = cmd.strip()
        if cmd == "":
            return
        if cmd == "q":
            self.running = False
        elif cmd == "q!":
            self.running = False
        elif cmd == "w":
            self.save_file()
        elif cmd in ("wq", "x"):
            if self.save_file():
                self.running = False
        elif cmd == "w!":
            self.save_file()
        elif cmd.startswith("w "):
            self.save_file(cmd[2:].strip())
        elif cmd.startswith("e "):
            self.open_file(cmd[2:].strip())
        elif cmd == "e!":
            if self.filename:
                self.open_file(self.filename)
        elif cmd == "enew":
            self.text = [""]
            self.filename = None
            self.saved_text = [""]
            self.history = []
            self.message = "已新建文件"
        elif cmd.startswith("set "):
            self.do_set(cmd[4:].strip())
        elif cmd.startswith("%s") or cmd.startswith("s"):
            self.do_substitute(cmd)
        elif cmd in ("noh", "nohlsearch"):
            self.search_pattern = None
            self.search_results = []
            self.message = "已清除搜索高亮"
        elif cmd in ("h", "help"):
            self.message = "i 插入  Ctrl+S 保存  Ctrl+O 打开  :q 退出  .cn 为本程序专有格式，其他编辑器打不开"
        else:
            self.message = "未识别的命令: " + cmd

    def do_set(self, arg):
        if arg in ("nu", "number"):
            self.show_line_numbers = True
            self.message = "已显示行号"
        elif arg in ("nonu", "nonumber"):
            self.show_line_numbers = False
            self.message = "已隐藏行号"
        elif arg in ("ai", "autoindent"):
            self.auto_indent = True
            self.message = "已启用自动缩进"
        elif arg in ("noai", "noautoindent"):
            self.auto_indent = False
            self.message = "已禁用自动缩进"
        elif arg == "sw=2":
            self.shiftwidth = 2
            self.message = "缩进宽度 = 2"
        elif arg == "sw=4":
            self.shiftwidth = 4
            self.message = "缩进宽度 = 4"
        else:
            self.message = "未知选项: " + arg

    def do_substitute(self, cmd):
        whole = False
        s = cmd
        if s.startswith('%'):
            whole = True
            s = s[1:]
        if not s.startswith('s') or len(s) < 3:
            self.message = "用法: s/旧/新/g  或  %s/旧/新/g"
            return
        body = s[1:]
        sep = body[0]
        if sep not in '/\\|@!':
            self.message = "格式错误"
            return
        parts = body[1:].split(sep)
        if len(parts) < 2:
            self.message = "格式错误"
            return
        pattern, repl = parts[0], parts[1]
        flags = parts[2] if len(parts) > 2 else ""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            self.message = "正则错误: " + str(e)
            return
        maxcount = 0 if 'g' in flags else 1
        start = 0 if whole else self.cursor_y
        end = len(self.text) if whole else min(len(self.text), self.cursor_y + 1)
        new_text = [l for l in self.text]
        total = 0
        for i in range(start, end):
            nl, n = rx.subn(repl, self.text[i], count=maxcount)
            if n:
                new_text[i] = nl
                total += n
        if total:
            self.save_state()
            self.text = new_text
            self.message = "替换了 %d 处" % total
        else:
            self.message = "未找到匹配: " + pattern

    # ---------------------------------------------------------------- 可视模式
    def visual_char_range(self):
        a = self.yx_to_flat(self.visual_start[0], self.visual_start[1])
        b = self.yx_to_flat(self.cursor_y, self.cursor_x)
        if a <= b:
            return a, b + 1
        return b, a + 1

    def handle_visual_mode(self, key):
        if key == 27:
            self.exit_visual()
            return
        if key == 'v' or key == 'V':
            self.visual_linewise = not self.visual_linewise
            return
        if key == 'o':
            (self.visual_start, (self.cursor_y, self.cursor_x)) = ((self.cursor_y, self.cursor_x), self.visual_start)
            return
        if key == 'd' or key == 'x':
            self.visual_delete()
            return
        if key == 'y':
            self.visual_yank()
            return
        if key == 'c':
            self.visual_change()
            return
        if key == 'p' or key == 'P':
            self.visual_paste()
            return
        if key == '~':
            self.visual_transform(lambda ch: ch.upper() if ch.islower() else ch.lower())
            return
        if key == 'u':
            self.visual_transform(lambda ch: ch.lower())
            return
        if key == 'U':
            self.visual_transform(lambda ch: ch.upper())
            return
        if key == curses.KEY_LEFT:
            self.move_char(-1, 1)
            return
        if key == curses.KEY_RIGHT:
            self.move_char(1, 1)
            return
        if key == curses.KEY_UP:
            self.move_line(-1)
            return
        if key == curses.KEY_DOWN:
            self.move_line(1)
            return
        if key == curses.KEY_PPAGE:
            self.move_page(-1)
            return
        if key == curses.KEY_NPAGE:
            self.move_page(1)
            return
        if not isinstance(key, str) or not key.isprintable():
            return
        c = key
        if c.isdigit():
            if c == '0' and not self.counter:
                self.cursor_x = 0
                return
            self.counter += c
            return
        count = int(self.counter) if self.counter else 1
        self.counter = ""
        if c == 'i' or c == 'a':
            nx = self.getkey()
            if nx in ('w', 'W'):
                self.visual_select_word(c)
            return
        if c in 'hjklwWeEbB0^$G':
            self.do_motion_char(c, count)
            return
        if c == ' ':
            self.move_char(1, count)
            return
        if c in 'fFtT':
            self.find_key = c
            self.find_count = count
            self.find_pending = True
            return
        if c == ';':
            self.repeat_find(False)
            return
        if c == ',':
            self.repeat_find(True)
            return
        if c == '%':
            self.match_bracket()
            return
        if c == 'g':
            nx = self.getkey()
            if nx == 'g':
                self.goto_line(1)
            return
        if c == 'n':
            self.search_next(count, False)
            return
        if c == 'N':
            self.search_next(count, True)
            return
        if c == '/':
            self.start_search(True)
            return
        if c == '?':
            self.start_search(False)
            return

    def visual_select_word(self, prefix):
        cur = self.yx_to_flat(self.cursor_y, self.cursor_x)
        s = self.word_start_flat(cur)
        e = self.word_end_flat(cur)
        if s is None or e is None:
            return
        if prefix == 'a':
            ftxt = self.flat_text()
            t = e
            while t < len(ftxt) and ftxt[t] in ' \t':
                t += 1
            if t > e:
                e = t
        y1, x1 = self.flat_to_yx(s)
        y2, x2 = self.flat_to_yx(max(s, e - 1))
        self.visual_start = (y1, x1)
        self.cursor_y, self.cursor_x = y2, x2

    def visual_delete(self):
        if self.visual_linewise:
            y1 = min(self.visual_start[0], self.cursor_y)
            y2 = max(self.visual_start[0], self.cursor_y)
            self.save_state()
            self.clipboard = self.text[y1:y2 + 1]
            self.clipboard_linewise = True
            del self.text[y1:y2 + 1]
            if not self.text:
                self.text = [""]
            self.cursor_y = min(y1, len(self.text) - 1)
            self.cursor_x = 0
            self.exit_visual()
        else:
            s, e = self.visual_char_range()
            if e > s:
                self.save_state()
                removed = self.flat_cut(s, e)
                self.clipboard = [removed]
                self.clipboard_linewise = False
                self.cursor_y, self.cursor_x = self.flat_to_yx(s)
            self.exit_visual()

    def visual_yank(self):
        if self.visual_linewise:
            y1 = min(self.visual_start[0], self.cursor_y)
            y2 = max(self.visual_start[0], self.cursor_y)
            self.clipboard = self.text[y1:y2 + 1]
            self.clipboard_linewise = True
        else:
            s, e = self.visual_char_range()
            if e > s:
                self.clipboard = [self.flat_text()[s:e]]
                self.clipboard_linewise = False
        self.exit_visual()

    def visual_change(self):
        pos = (self.cursor_y, self.cursor_x)
        if self.visual_linewise:
            y1 = min(self.visual_start[0], self.cursor_y)
            y2 = max(self.visual_start[0], self.cursor_y)
            self.save_state()
            del self.text[y1:y2 + 1]
            if not self.text:
                self.text = [""]
            pos = (min(y1, len(self.text) - 1), 0)
        else:
            s, e = self.visual_char_range()
            if e > s:
                self.save_state()
                self.flat_cut(s, e)
                pos = self.flat_to_yx(s)
        self.exit_visual()
        self.begin_insert(pos[0], pos[1])

    def visual_paste(self):
        saved = (self.clipboard, self.clipboard_linewise)
        if self.visual_linewise:
            y1 = min(self.visual_start[0], self.cursor_y)
            y2 = max(self.visual_start[0], self.cursor_y)
            self.save_state()
            self.clipboard = self.text[y1:y2 + 1]
            self.clipboard_linewise = True
            del self.text[y1:y2 + 1]
            if not self.text:
                self.text = [""]
            self.cursor_y = min(y1, len(self.text) - 1)
            self.cursor_x = 0
        else:
            s, e = self.visual_char_range()
            if e > s:
                self.save_state()
                self.flat_cut(s, e)
                self.cursor_y, self.cursor_x = self.flat_to_yx(s)
        self.clipboard, self.clipboard_linewise = saved
        self.exit_visual()
        if self.clipboard:
            self.do_paste(False, 1)

    def visual_transform(self, func):
        if self.visual_linewise:
            y1 = min(self.visual_start[0], self.cursor_y)
            y2 = max(self.visual_start[0], self.cursor_y)
            self.save_state()
            for i in range(y1, y2 + 1):
                self.text[i] = "".join(func(ch) for ch in self.text[i])
        else:
            s, e = self.visual_char_range()
            if e > s:
                self.save_state()
                ftxt = self.flat_text()
                sub = ftxt[s:e]
                self.lines_from_flat(ftxt[:s] + "".join(func(ch) for ch in sub) + ftxt[e:])
        self.exit_visual()

    # ---------------------------------------------------------------- 文件操作
    def save_file(self, filename=None):
        try:
            if filename:
                self.filename = filename
            if not self.filename:
                self.filename = "未命名" + CN_EXT
            base = os.path.basename(self.filename)
            if "." not in base:
                self.filename += CN_EXT
            if self.filename.lower().endswith(CN_EXT):
                with open(self.filename, "wb") as f:
                    f.write(cn_encode(self.text))
                self.message = "已保存: " + self.filename
            else:
                with open(self.filename, "w", encoding="utf-8") as f:
                    for line in self.text:
                        f.write(line + "\n")
                self.message = "已保存(纯文本): " + self.filename
            self.saved_text = [l for l in self.text]
            return True
        except Exception as e:
            self.message = "保存失败: " + str(e)
            return False

    def open_file(self, filename):
        if not os.path.exists(filename):
            self.filename = filename
            self.text = [""]
            self.saved_text = [""]
            self.history = []
            self.cursor_y = self.cursor_x = 0
            self.message = "新文件: " + filename
            return
        try:
            with open(filename, "rb") as f:
                raw = f.read()
            if raw.startswith(CN_MAGIC):
                self.text = cn_decode(raw)
            else:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("gbk")
                content = content.replace("\r\n", "\n").replace("\r", "\n")
                self.text = content.split("\n") if content else [""]
            self.filename = filename
            self.saved_text = [l for l in self.text]
            self.history = []
            self.cursor_y = self.cursor_x = 0
            self.top_line = 0
            self.view_x = 0
            self.message = "已打开: " + filename
        except Exception as e:
            self.message = "打开失败: " + str(e)

    # ---------------------------------------------------------------- 绘制
    def line_attr_array(self, y, line):
        n = len(line)
        attrs = [0] * n
        if self.search_pattern:
            try:
                rx = re.compile(self.search_pattern)
                for m in rx.finditer(line):
                    for k in range(m.start(), min(m.end(), n)):
                        attrs[k] = 1
            except re.error:
                pass
        if self.visual_active:
            vy, vx = self.visual_start
            cy, cx = self.cursor_y, self.cursor_x
            if self.visual_linewise:
                y1, y2 = min(vy, cy), max(vy, cy)
                if y1 <= y <= y2:
                    attrs = [2] * n
            else:
                sf = self.yx_to_flat(vy, vx)
                ef = self.yx_to_flat(cy, cx)
                yf = self.yx_to_flat(y, 0)
                lo, hi = yf, yf + max(n, 0)
                s, e = (sf, ef) if sf <= ef else (ef, sf)
                if e >= lo and s < hi and n > 0:
                    start_x = max(0, s - yf)
                    end_x = min(n - 1, e - yf)
                    for k in range(start_x, min(end_x + 1, n)):
                        attrs[k] = 2
        return attrs

    def draw(self):
        if self.dialog_active:
            self.draw_dialog()
            return
        self.screen.clear()
        rows, cols = self.rows, self.cols
        if self.show_line_numbers:
            self.line_num_w = max(len(str(len(self.text))) + 1, 4)
        else:
            self.line_num_w = 0
        text_w = max(1, cols - self.line_num_w)
        view_h = max(1, rows - 1)

        if self.cursor_y < self.top_line:
            self.top_line = self.cursor_y
        if self.cursor_y >= self.top_line + view_h:
            self.top_line = self.cursor_y - view_h + 1
        if self.cursor_x < self.view_x:
            self.view_x = self.cursor_x
        if self.cursor_x - self.view_x >= text_w:
            self.view_x = self.cursor_x - text_w + 1

        for i in range(view_h):
            y = self.top_line + i
            if y >= len(self.text):
                try:
                    self.screen.addstr(i, self.line_num_w, "~", curses.A_DIM)
                except curses.error:
                    pass
                continue
            line = self.text[y]
            if self.show_line_numbers:
                try:
                    self.screen.addstr(i, 0, str(y + 1).rjust(self.line_num_w - 1), curses.A_DIM)
                except curses.error:
                    pass
            self.draw_line(i, self.line_num_w, y, line, text_w)

        self.draw_status()
        self.position_cursor()
        self.screen.refresh()

    def draw_line(self, y_screen, x_screen, y, full_line, text_w):
        attrs = self.line_attr_array(y, full_line)
        base = self.view_x
        visible, _ = self.visible_part(full_line, base, text_w)
        if not visible:
            if self.visual_active and self.visual_linewise:
                vy, cy = self.visual_start[0], self.cursor_y
                if min(vy, cy) <= y <= max(vy, cy):
                    try:
                        self.screen.addstr(y_screen, x_screen, " ", curses.A_REVERSE)
                    except curses.error:
                        pass
            return
        runs = []
        i = 0
        while i < len(visible):
            attr = attrs[base + i]
            j = i
            while j < len(visible) and attrs[base + j] == attr:
                j += 1
            runs.append((visible[i:j], attr))
            i = j
        curx = x_screen
        for text, attr in runs:
            if not text:
                continue
            try:
                if attr in (1, 2):
                    self.screen.addstr(y_screen, curx, text, curses.A_REVERSE)
                else:
                    self.screen.addstr(y_screen, curx, text)
            except curses.error:
                pass
            curx += self.char_cells(text)

    def visible_part(self, line, start, maxcells):
        w = 0
        for i, ch in enumerate(line[start:], start=start):
            cw = cell_width(ch)
            if w + cw > maxcells:
                return line[start:i], w
            w += cw
        return line[start:], w

    def draw_status(self):
        line = self.rows - 1
        mod = "+" if self.text != self.saved_text else " "
        fname = self.filename if self.filename else "[未命名]"
        left = '"%s"%s  %d行, 第%d行' % (fname, mod, len(self.text), self.cursor_y + 1)
        if self.mode == "insert":
            right = "-- 插入 --" if not self.replace_mode else "-- 替换 --"
        elif self.mode == "visual":
            right = "-- 可视行 --" if self.visual_linewise else "-- 可视 --"
        elif self.mode == "command":
            right = ":" + self.command
        elif self.mode == "search":
            right = ("/" if self.search_forward else "?") + self.search_query
        else:
            pct = int((self.cursor_y + 1) / max(1, len(self.text)) * 100)
            right = "%3d%%" % pct
        if self.message:
            right = self.message + "  " + right
        avail = self.cols - 1
        left_cells = self.char_cells(left)
        right_cells = self.char_cells(right)
        while right_cells > avail - left_cells - 1 and right:
            right = right[:-1]
            right_cells = self.char_cells(right)
        pad = max(0, avail - left_cells - right_cells)
        status = self.truncate_cells(left + " " * pad + right, avail)
        try:
            self.screen.addstr(line, 0, status, curses.A_REVERSE)
        except curses.error:
            pass

    def position_cursor(self):
        y = self.cursor_y - self.top_line
        line = self.text[self.cursor_y]
        before = line[self.view_x:self.cursor_x]
        col = self.line_num_w + self.char_cells(before)
        if 0 <= y < self.rows - 1:
            try:
                self.screen.move(y, col)
            except curses.error:
                pass
        if self.mode == "insert":
            try:
                curses.curs_set(2)
            except Exception:
                pass
        else:
            try:
                curses.curs_set(1)
            except Exception:
                pass


# 兼容旧类名
ChinaEditor = VimEditor


if __name__ == "__main__":
    editor = VimEditor()
    try:
        editor.run(sys.argv[1] if len(sys.argv) > 1 else None)
    except KeyboardInterrupt:
        editor.cleanup()
    except Exception:
        editor.cleanup()
        raise
