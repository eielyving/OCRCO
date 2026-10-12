import sys
import os
import subprocess
import threading
import requests
import json
import re
import gc 
import winreg 
import ctypes
import socket
import urllib.parse
from ctypes import wintypes
import math
from datetime import datetime
import traceback
import numpy as np
import cv2
from PyQt6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, 
                             QTextEdit, QPushButton, QMessageBox, QSystemTrayIcon, QMenu, 
                             QStyle, QSizePolicy, QFrame, QLineEdit, QFileDialog, QButtonGroup)
from PyQt6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, pyqtSignal, QObject, QSize, QTimer, QAbstractNativeEventFilter
from PyQt6.QtGui import (QPainter, QColor, QPen, QGuiApplication, QIcon, QAction, QFont, 
                        QPixmap, QFontMetrics, QImage, QPolygonF, QBrush, QCursor)
from wechat_ocr.ocr_manager import OcrManager

# --- 全局异常日志记录，确保任何崩溃均输出至 crash.log ---
def global_exception_logger(exctype, value, tb):
    err = "".join(traceback.format_exception(exctype, value, tb))
    print(f"[FATAL EXCEPTION] {err}", file=sys.stderr)
    try:
        log_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(log_dir, "crash.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CRASH:\n{err}\n")
    except Exception:
        pass
    sys.__excepthook__(exctype, value, tb)

sys.excepthook = global_exception_logger

# --- Windows API 设置 ---
user32 = ctypes.windll.user32
# 定义参数类型，防止64位系统下的指针截断问题
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_bool

MOD_ALT = 0x0001
VK_A = 0x41
WM_HOTKEY = 0x0312
HOTKEY_ID = 1

# --- 用户配置区 ---
OPENAI_API_KEY = "" 
OPENAI_MODEL = "gpt-3.5-turbo"
APP_NAME = "OCRCO"

# --- 工作目录与环境设置 ---
if getattr(sys, 'frozen', False):
    APP_ROOT = os.path.dirname(sys.executable)
else:
    APP_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_ROOT)

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# --- 智能路径查找函数 ---
def get_ocr_engine_path():
    if not getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_engine")
    
    base_dir = os.path.dirname(sys.executable)
    path_a = os.path.join(base_dir, "ocr_engine")
    if os.path.exists(path_a): return path_a
    path_b = os.path.join(base_dir, "_internal", "ocr_engine")
    if os.path.exists(path_b): return path_b
    return path_a

APP_PATH = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
ICON_PATH = resource_path("icon-48.png")
MY_OCR_FOLDER = get_ocr_engine_path() 
WECHAT_OCR_EXE = os.path.join(MY_OCR_FOLDER, "WeChatOCR.exe")
WECHAT_LIB_DIR = MY_OCR_FOLDER

class OCRSignaler(QObject):
    result_ready = pyqtSignal(str)
    # 专门用于热键触发的信号
    hotkey_pressed = pyqtSignal()

# --- 【核心修复】全局热键过滤器 ---
# 这是一个独立于窗口的监听器，挂在 App 上，极其稳定
class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, signaler):
        super().__init__()
        self.signaler = signaler

    def nativeEventFilter(self, eventType, message):
        # PyQt6 中 eventType 是 bytes 类型
        if eventType == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    print("[DEBUG] 收到 Alt+A 热键消息，发射信号！")
                    self.signaler.hotkey_pressed.emit()
                    return True, 0
            except Exception as e:
                print(f"[ERROR] 消息过滤出错: {e}")
        return False, 0

class AutoScalableLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("border: 2px dashed #666; background: #2b2b2b; border-radius: 10px;")
        self.setMouseTracking(True)
        self._pixmap = None
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._is_panning = False
        self._last_mouse_pos = QPointF(0, 0)
        self._show_badge = False
        self._badge_timer = QTimer(self)
        self._badge_timer.setSingleShot(True)
        self._badge_timer.timeout.connect(self._hide_badge)

    def _hide_badge(self):
        self._show_badge = False
        self.update()

    def set_custom_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.reset_zoom()
        self.update()

    def clear_canvas(self):
        self._pixmap = None
        self.reset_zoom()
        self.update()

    def reset_zoom(self):
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._is_panning = False
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event):
        if not self._pixmap or self._pixmap.isNull():
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta == 0:
            return

        old_zoom = self._zoom
        factor = 1.15 if delta > 0 else (1.0 / 1.15)
        new_zoom = max(0.5, min(8.0, old_zoom * factor))
        if abs(new_zoom - 1.0) < 0.04:
            new_zoom = 1.0

        if new_zoom != old_zoom:
            # 以当前鼠标位置为缩放锚点，使光标下的图像点保持稳定
            cursor_pos = event.position()
            cx, cy = self.width() / 2.0, self.height() / 2.0
            
            dx = cursor_pos.x() - cx - self._pan_x
            dy = cursor_pos.y() - cy - self._pan_y
            
            scale_change = new_zoom / old_zoom
            self._pan_x -= dx * (scale_change - 1.0)
            self._pan_y -= dy * (scale_change - 1.0)
            self._zoom = new_zoom

            if self._zoom <= 1.01:
                self._pan_x = 0.0
                self._pan_y = 0.0
                self.setCursor(Qt.CursorShape.ArrowCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)

            self._show_badge = True
            self._badge_timer.start(1500)
            self.update()

        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._zoom > 1.01:
            self._is_panning = True
            self._last_mouse_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_panning and self._zoom > 1.01:
            cur_pos = event.position()
            diff = cur_pos - self._last_mouse_pos
            self._pan_x += diff.x()
            self._pan_y += diff.y()
            self._last_mouse_pos = cur_pos
            self.update()
            event.accept()
        else:
            if self._zoom > 1.01 and not self._is_panning:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._zoom > 1.01 else Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 双击快速重置缩放至 100% 适应视口
            self.reset_zoom()
            self._show_badge = True
            self._badge_timer.start(1200)
            self.update()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

            if self._pixmap and not self._pixmap.isNull():
                base_scaled = self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                bw = base_scaled.width() * self._zoom
                bh = base_scaled.height() * self._zoom
                
                bx = (self.width() - bw) / 2.0 + self._pan_x
                by = (self.height() - bh) / 2.0 + self._pan_y

                # 视口裁剪保护
                painter.save()
                painter.setClipRect(self.rect().adjusted(2, 2, -2, -2))
                target_rect = QRect(int(round(bx)), int(round(by)), int(round(bw)), int(round(bh)))
                painter.drawPixmap(target_rect, self._pixmap)
                painter.restore()

                # 缩放倍率悬浮胶囊提示
                if self._show_badge or self._zoom > 1.01:
                    zoom_pct = int(round(self._zoom * 100))
                    hint_txt = f"🔍 {zoom_pct}% (滚轮缩放 / 拖拽平移 / 双击复原)"
                    font = QFont("Microsoft YaHei", 9, QFont.Weight.Medium)
                    painter.setFont(font)
                    fm = QFontMetrics(font)
                    tw = fm.horizontalAdvance(hint_txt) + 18
                    th = 26
                    rx = self.width() - tw - 14
                    ry = 14
                    badge_rect = QRect(int(rx), int(ry), int(tw), int(th))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(20, 20, 24, 200))
                    painter.drawRoundedRect(badge_rect, 6, 6)
                    painter.setPen(QColor(240, 240, 240))
                    painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, hint_txt)
            else:
                painter.setPen(QColor("#888888"))
                font = painter.font()
                font.setPointSize(14)
                painter.setFont(font)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "截图预览\n(Light Mode)")
        except Exception as e:
            print(f"[ERROR] AutoScalableLabel.paintEvent: {e}")

def is_cjk(ch):
    return '\u4e00' <= ch <= '\u9fff'

def merge_text_lines(line1, line2):
    if not line1: return line2
    if not line2: return line1
    last = line1[-1]
    first = line2[0]
    
    if last == '-' and first.isalpha():
        return line1[:-1] + line2
        
    if is_cjk(last) or is_cjk(first) or last in '，。！？、；：“”‘’（）《》【】':
        return line1 + line2
        
    return line1 + ' ' + line2

def should_merge_lines(prev_it, cur_it, avg_h):
    gap = cur_it['top'] - prev_it['bottom']
    if gap > avg_h * 0.95:
        return False
    prev_txt = prev_it['text'].strip()
    cur_txt = cur_it['text'].strip()
    if not prev_txt or not cur_txt:
        return False
    # 首行缩进过大或属于独立栏目则不合并
    if abs(cur_it['left'] - prev_it['left']) > avg_h * 1.5:
        return False
    # 序号、列表符开头视为新项目
    if re.match(r'^((\d+[\.\、\)])|([一二三四五六七八九十]+[\.\、\)])|([•\-\*\+]))', cur_txt):
        return False
    # 冒号、大括号结尾通常为标题或代码结构
    if prev_txt[-1] in (':', '：', '{', '}'):
        return False
    # 标题/卡片标题特征保护：上一行为短小标题（如 "AIHOT", "Automate", "Canvas"），下一行为描述正文，不合并
    prev_words = prev_txt.split()
    if len(prev_txt) <= 25 and (len(prev_words) <= 4 or len(prev_txt) <= 10) and prev_txt[-1] not in (',', '，', '-', '—'):
        if len(cur_txt) >= 15 or len(cur_txt.split()) >= 4:
            return False
    if prev_it.get('height', avg_h) > cur_it.get('height', avg_h) * 1.25:
        return False
    # 句号/问号结尾且后续行明显缩进或垂直间距较大
    if prev_txt[-1] in ('。', '！', '？', '.', '!', '?') and (cur_it['left'] - prev_it['left']) > avg_h * 0.8:
        return False
    if prev_txt[-1] in ('。', '！', '？') and gap > avg_h * 0.65:
        return False
    return True

def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

class LayoutFormatter:
    @classmethod
    def format_blocks(cls, raw_blocks, mode='smart'):
        '''
        mode:
          'smart': 智能排版 (多栏/表格对齐，段落自动合并)
          'paragraph': 自然段落 (合并换行硬回车)
          'raw': 逐行原始 (按OCR原始分行)
          'markdown': 表格转为 Markdown 格式
        '''
        if not raw_blocks:
            return '', ''
        try:
            return cls._format_blocks_impl(raw_blocks, mode)
        except Exception as e:
            print(f"[WARN] LayoutFormatter.format_blocks error: {e}")
            raw_lines = [str(b.get('text', '')).strip() for b in raw_blocks if isinstance(b, dict) and b.get('text')]
            fallback_text = '\n'.join([l for l in raw_lines if l])
            return fallback_text, cls.to_html_plain(fallback_text)

    @classmethod
    def detect_column_splits(cls, items, avg_h):
        """
        稳健多栏/卡片分栏中轴线检测：
        即使页面中有通栏横幅或按钮，也能通过统计学排除噪点，准确定位多栏分界线
        """
        if len(items) < 4:
            return []
        min_x = max(0.0, min(it['left'] for it in items))
        max_x = max(min_x + 10.0, max(it['right'] for it in items))
        if (max_x - min_x) < avg_h * 5:
            return []

        step = max(5, int(avg_h * 0.4))
        cand_splits = []
        for sx in range(int(min_x + avg_h * 2), int(max_x - avg_h * 2), step):
            cross = [it for it in items if it['left'] < sx < it['right']]
            left = [it for it in items if it['right'] <= sx]
            right = [it for it in items if it['left'] >= sx]
            if len(left) >= 2 and len(right) >= 2 and len(cross) <= max(1, len(items) * 0.15):
                cand_splits.append((sx, len(cross), len(left), len(right)))

        if not cand_splits:
            return []

        cand_splits.sort(key=lambda s: s[0])
        merged_splits = []
        g_start = cand_splits[0][0]
        g_end = cand_splits[0][0]
        for s in cand_splits[1:]:
            if s[0] - g_end <= step * 2.5:
                g_end = s[0]
            else:
                merged_splits.append((g_start + g_end) / 2.0)
                g_start = s[0]
                g_end = s[0]
        merged_splits.append((g_start + g_end) / 2.0)
        return merged_splits

    @classmethod
    def extract_paragraphs(cls, raw_blocks):
        """
        按阅读顺序和排版结构聚合成语义完整的自然段落。
        解决断行截断导致的逐词硬翻译、语序混乱问题。
        返回包含完整文本、外接矩形、行数据及对齐方式的段落对象列表。
        """
        items = []
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            loc = b.get('location')
            if not isinstance(loc, dict):
                loc = {}
            t = safe_float(loc.get('top'), 0.0)
            bt = safe_float(loc.get('bottom'), 0.0)
            l = safe_float(loc.get('left'), 0.0)
            r = safe_float(loc.get('right'), 0.0)
            txt = str(b.get('text', '')).strip()
            if txt and (r - l) > 0 and (bt - t) > 0:
                items.append({
                    'left': l, 'right': r, 'top': t, 'bottom': bt,
                    'height': max(1.0, bt - t), 'width': max(1.0, r - l),
                    'cy': (t + bt) / 2, 'cx': (l + r) / 2,
                    'text': txt
                })

        if not items:
            return []

        avg_h = sum(it['height'] for it in items) / len(items)
        if avg_h <= 0:
            avg_h = 16.0

        split_x_list = cls.detect_column_splits(items, avg_h)
        is_multi_column = bool(split_x_list)

        def build_para_obj(para_lines):
            merged = para_lines[0]['text']
            for it in para_lines[1:]:
                merged = merge_text_lines(merged, it['text'])
            min_l = min(x['left'] for x in para_lines)
            min_t = min(x['top'] for x in para_lines)
            max_r = max(x['right'] for x in para_lines)
            max_b = max(x['bottom'] for x in para_lines)
            lefts = [x['left'] for x in para_lines]
            cxs = [x['cx'] for x in para_lines]
            is_centered = len(para_lines) > 1 and (max(cxs) - min(cxs) < 15) and (max(lefts) - min(lefts) > 20)
            return {
                'text': merged,
                'lines': para_lines,
                'location': {'left': min_l, 'top': min_t, 'right': max_r, 'bottom': max_b},
                'avg_height': sum(x['height'] for x in para_lines) / len(para_lines),
                'align': 'center' if is_centered else 'left'
            }

        paragraphs = []
        if is_multi_column:
            def get_col(it):
                for idx, sx in enumerate(split_x_list):
                    if it['cx'] < sx:
                        return idx
                return len(split_x_list)

            for it in items:
                it['col'] = get_col(it)

            num_cols = len(split_x_list) + 1
            for c in range(num_cols):
                c_items = [it for it in items if it['col'] == c]
                c_items.sort(key=lambda x: (round(x['top'] / (avg_h * 0.8)), x['left']))
                cur_para = []
                for it in c_items:
                    if not cur_para:
                        cur_para.append(it)
                    else:
                        prev = cur_para[-1]
                        if should_merge_lines(prev, it, avg_h):
                            cur_para.append(it)
                        else:
                            paragraphs.append(build_para_obj(cur_para))
                            cur_para = [it]
                if cur_para:
                    paragraphs.append(build_para_obj(cur_para))
            paragraphs.sort(key=lambda p: (round(p['location']['top'] / (avg_h * 1.2)), p['location']['left']))
        else:
            items.sort(key=lambda x: (round(x['top'] / (avg_h * 0.8)), x['left']))
            cur_para = []
            for it in items:
                if not cur_para:
                    cur_para.append(it)
                else:
                    prev = cur_para[-1]
                    if should_merge_lines(prev, it, avg_h):
                        cur_para.append(it)
                    else:
                        paragraphs.append(build_para_obj(cur_para))
                        cur_para = [it]
            if cur_para:
                paragraphs.append(build_para_obj(cur_para))

        return paragraphs

    @classmethod
    def _format_blocks_impl(cls, raw_blocks, mode='smart'):
        items = []
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            loc = b.get('location')
            if not isinstance(loc, dict):
                loc = {}
            t = safe_float(loc.get('top'), 0.0)
            bt = safe_float(loc.get('bottom'), 0.0)
            l = safe_float(loc.get('left'), 0.0)
            r = safe_float(loc.get('right'), 0.0)
            txt = str(b.get('text', '')).strip()
            if txt:
                items.append({
                    'left': l, 'right': r, 'top': t, 'bottom': bt,
                    'height': max(1.0, bt - t), 'width': max(1.0, r - l),
                    'cy': (t + bt) / 2, 'cx': (l + r) / 2,
                    'text': txt
                })
                
        if not items:
            return '', ''

        if mode == 'raw':
            items.sort(key=lambda x: (round(x['top'] / 15), x['left']))
            raw_text = '\n'.join(it['text'] for it in items)
            return raw_text, cls.to_html_plain(raw_text)

        avg_h = sum(it['height'] for it in items) / len(items)
        if avg_h <= 0:
            avg_h = 16.0

        split_x_list = cls.detect_column_splits(items, avg_h)
        is_multi_column = bool(split_x_list)

        if is_multi_column and mode in ('smart', 'markdown'):
            num_cols = len(split_x_list) + 1
            
            def get_col(it):
                for idx, sx in enumerate(split_x_list):
                    if it['cx'] < sx:
                        return idx
                return len(split_x_list)

            for it in items:
                it['col'] = get_col(it)

            col_cells = []
            for c in range(num_cols):
                c_items = [it for it in items if it['col'] == c]
                c_items.sort(key=lambda x: x['top'])
                
                cells = []
                for it in c_items:
                    if not cells:
                        cells.append([it])
                    else:
                        prev = cells[-1][-1]
                        if should_merge_lines(prev, it, avg_h):
                            cells[-1].append(it)
                        else:
                            cells.append([it])
                            
                cell_objs = []
                for cell in cells:
                    merged = cell[0]['text']
                    for it in cell[1:]:
                        merged = merge_text_lines(merged, it['text'])
                    top_y = min(x['top'] for x in cell)
                    bot_y = max(x['bottom'] for x in cell)
                    cell_objs.append({
                        'top': top_y, 'bottom': bot_y, 'cy': (top_y + bot_y) / 2,
                        'text': merged, 'col': c
                    })
                col_cells.append(cell_objs)

            all_cells = []
            for c in range(num_cols):
                all_cells.extend(col_cells[c])
            all_cells.sort(key=lambda x: x['top'])

            rows = []
            for cell in all_cells:
                placed = False
                for row in rows:
                    r_top = min(x['top'] for x in row)
                    r_bot = max(x['bottom'] for x in row)
                    overlap = min(cell['bottom'], r_bot) - max(cell['top'], r_top)
                    if overlap > 0.3 * min(cell['bottom'] - cell['top'], r_bot - r_top) or abs(cell['cy'] - (r_top + r_bot) / 2) < avg_h * 1.2:
                        if not any(x['col'] == cell['col'] for x in row):
                            row.append(cell)
                            placed = True
                            break
                if not placed:
                    rows.append([cell])

            rows.sort(key=lambda r: min(x['top'] for x in r))
            
            if mode == 'markdown':
                md_lines = []
                header = '| ' + ' | '.join(f'列 {i+1}' for i in range(num_cols)) + ' |'
                sep = '| ' + ' | '.join(['---'] * num_cols) + ' |'
                md_lines.append(header)
                md_lines.append(sep)
                for r in rows:
                    cols_txt = [''] * num_cols
                    for cell in r:
                        cols_txt[cell['col']] = cell['text'].replace('|', '\\|')
                    md_lines.append('| ' + ' | '.join(cols_txt) + ' |')
                plain_res = '\n'.join(md_lines)
                html_res = cls.to_html_table(rows, num_cols)
                return plain_res, html_res
            else:
                plain_lines = []
                for r in rows:
                    cols_txt = [''] * num_cols
                    for cell in r:
                        cols_txt[cell['col']] = cell['text']
                    plain_lines.append('\t'.join(cols_txt))
                plain_res = '\n'.join(plain_lines)
                html_res = cls.to_html_table(rows, num_cols)
                return plain_res, html_res

        else:
            paragraphs = cls.extract_paragraphs(raw_blocks)
            para_texts = [p['text'] for p in paragraphs]
            plain_res = '\n\n'.join(para_texts)
            html_res = cls.to_html_paragraphs(para_texts)
            return plain_res, html_res

    @staticmethod
    def to_html_plain(text):
        escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return f'<div style="font-family: \'Microsoft YaHei\', sans-serif; font-size: 16px; line-height: 1.6; color: #222;">{escaped.replace(chr(10), "<br>")}</div>'

    @staticmethod
    def to_html_paragraphs(paras):
        html_paras = ''.join(f'<p style="margin-bottom: 12px; line-height: 1.6;">{p}</p>' for p in paras)
        return f'<div style="font-family: \'Microsoft YaHei\', sans-serif; font-size: 16px; color: #222;">{html_paras}</div>'

    @staticmethod
    def to_html_table(rows, num_cols):
        rows_html = []
        for r in rows:
            cols_txt = [''] * num_cols
            for cell in r:
                cols_txt[cell['col']] = cell['text']
            cells_html = ''.join(
                f'<td style="border: 1px solid #d9d9d9; padding: 8px 12px; background: {"#f5f7fa" if i==0 else "#ffffff"}; font-weight: {"bold" if i==0 else "normal"}; font-size: 15px; vertical-align: top;">{txt}</td>'
                for i, txt in enumerate(cols_txt)
            )
            rows_html.append(f'<tr>{cells_html}</tr>')
        table_body = ''.join(rows_html)
        return f'''
        <div style="font-family: \'Microsoft YaHei\', sans-serif; font-size: 15px; color: #222;">
            <table style="border-collapse: collapse; width: 100%; border: 1px solid #d9d9d9;">
                {table_body}
            </table>
        </div>
        '''

class TranslationService:
    """
    多引擎高可用翻译服务：
    1. 在线首选：Google Chrome 扩展 API (超高限流上限，极少 429)
    2. 在线备用 1：Google GTX Web API (浏览器级伪装 Headers)
    3. 在线备用 2：MyMemory 开放翻译 API
    4. 离线兜底 / 局域网断网保底：本地 Ollama 大模型 (如 qwen2.5:7b / qwen2.5:latest)，100% 本地运行无需外网
    """
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
    }

    @staticmethod
    def is_online(timeout=0.8):
        for host in [('223.5.5.5', 53), ('119.29.29.29', 53), ('1.1.1.1', 53)]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(host)
                s.close()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def get_ollama_model():
        try:
            r = requests.get('http://127.0.0.1:11434/api/tags', timeout=0.8)
            if r.status_code == 200:
                models = [m.get('name', '') for m in r.json().get('models', [])]
                for preferred in ['qwen2.5:7b', 'qwen2.5:latest', 'qwen2.5', 'llama3', 'gemma2']:
                    for m in models:
                        if preferred in m:
                            return m
                if models:
                    return models[0]
        except Exception:
            pass
        return None

    @classmethod
    def translate_google_chrome_batch(cls, items, to_lang='zh-CN'):
        if not items:
            return []
        url = 'https://clients5.google.com/translate_a/t'
        params = {'client': 'dict-chrome-ex', 'sl': 'auto', 'tl': to_lang}
        data = [('q', it) for it in items]
        r = requests.post(url, params=params, data=data, headers=cls.HEADERS, timeout=6)
        if r.status_code == 200:
            data_res = r.json()
            if isinstance(data_res, list):
                results = []
                for idx, entry in enumerate(data_res):
                    if isinstance(entry, list) and len(entry) > 0:
                        results.append(str(entry[0]))
                    elif isinstance(entry, str):
                        results.append(entry)
                    else:
                        results.append(items[idx])
                if len(results) == len(items):
                    return results
        raise Exception(f'Google Chrome API code {r.status_code}')

    @classmethod
    def translate_google_chrome(cls, text, to_lang='zh-CN'):
        res = cls.translate_google_chrome_batch([text], to_lang)
        if res:
            return res[0]
        raise Exception('Google Chrome API failed')

    @classmethod
    def translate_google_gtx(cls, text, to_lang='zh-CN'):
        url = 'https://translate.googleapis.com/translate_a/single'
        params = {'client': 'gtx', 'sl': 'auto', 'tl': to_lang, 'dt': 't'}
        r = requests.post(url, params=params, data={'q': text}, headers=cls.HEADERS, timeout=4)
        if r.status_code == 200:
            data = r.json()
            return ''.join([x[0] for x in data[0] if x and len(x) > 0 and x[0]])
        raise Exception(f'Google GTX API code {r.status_code}')

    @classmethod
    def translate_mymemory(cls, text, to_lang='zh-CN'):
        url = 'https://api.mymemory.translated.net/get'
        params = {'q': text[:500], 'langpair': f'auto|{to_lang}'}
        r = requests.get(url, params=params, headers=cls.HEADERS, timeout=4)
        if r.status_code == 200:
            res = r.json().get('responseData', {}).get('translatedText')
            if res:
                return res
        raise Exception('MyMemory API failed')

    @classmethod
    def translate_ollama(cls, text, to_lang='zh-CN', model=None):
        if not model:
            model = cls.get_ollama_model()
        if not model:
            raise Exception('未检测到本地 Ollama 模型')
        
        target_name = 'Simplified Chinese (简体中文)' if to_lang == 'zh-CN' else 'English'
        prompt = (
            f"You are a professional translator. Translate the following text into {target_name}.\n"
            "If any portion of the text is already in the target language, keep it natural and preserve it.\n"
            "Output ONLY the direct translation content, with no explanations, notes, markdown formatting or quotes:\n\n"
            f"{text}"
        )
        r = requests.post('http://127.0.0.1:11434/api/generate', json={
            'model': model,
            'prompt': prompt,
            'stream': False,
            'options': {'temperature': 0.1}
        }, timeout=15)
        if r.status_code == 200:
            return r.json().get('response', '').strip()
        raise Exception(f'Ollama API code {r.status_code}')

    @classmethod
    def _translate_chunk(cls, chunk, to_lang):
        online = cls.is_online(timeout=0.8)

        # 1. 在线首选：Google Chrome 扩展原生多参数批处理（每项独立语种识别，零断隔符损耗）
        if online:
            try:
                results = cls.translate_google_chrome_batch(chunk, to_lang)
                if results and len(results) == len(chunk):
                    return results, 'Google Chrome'
            except Exception:
                pass

        # 2. 备用通道：并发独立请求翻译（彻底杜绝 ||| 丢失或断句污染）
        if online:
            from concurrent.futures import ThreadPoolExecutor
            for engine_name, engine_func in [
                ('Google GTX', cls.translate_google_gtx),
                ('MyMemory', cls.translate_mymemory)
            ]:
                try:
                    with ThreadPoolExecutor(max_workers=min(6, len(chunk))) as executor:
                        futures = [executor.submit(engine_func, item, to_lang) for item in chunk]
                        results = [f.result(timeout=5) for f in futures]
                    if results and len(results) == len(chunk):
                        return results, engine_name
                except Exception:
                    continue

        # 3. 离线/在线失败时，调度本地 Ollama
        try:
            ollama_model = cls.get_ollama_model()
            if ollama_model:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(2, len(chunk))) as executor:
                    futures = [executor.submit(cls.translate_ollama, item, to_lang, ollama_model) for item in chunk]
                    results = [f.result(timeout=15) for f in futures]
                if results and len(results) == len(chunk):
                    return results, f'本地离线大模型 ({ollama_model})'
        except Exception:
            pass

        if not online:
            raise Exception('网络已断开（离线模式），且未检测到运行中的本地 Ollama 模型。\n已为您保留原始识别文字。')
        else:
            raise Exception('在线翻译接口均受限/超时(429)，且本地未启动 Ollama 模型。\n已为您保留原始识别文字。')

    @classmethod
    def translate_texts(cls, texts, to_lang='zh-CN'):
        if not texts:
            return [], '空文本'
        
        if not to_lang:
            to_lang = 'zh-CN'

        batch_size = 40
        all_results = []
        last_engine = ''

        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            chunk_results, engine_used = cls._translate_chunk(chunk, to_lang)
            all_results.extend(chunk_results)
            last_engine = engine_used

        return all_results, last_engine

class ResultWindow(QWidget):
    def __init__(self, ocr_manager, signaler):
        print("[DEBUG] 1. ResultWindow 开始初始化...") 
        super().__init__()
        self.ocr_manager = ocr_manager
        self.signaler = signaler
        self.selector = None 
        
        self.setWindowTitle("OCRCO - 智能识别")
        self.resize(1200, 700) 
        
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        
        # 强制创建原生窗口句柄 (这是解决 winId 无效的关键，代替 showMinimized)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        
        # 托盘设置
        self.tray_icon = QSystemTrayIcon(self)
        if os.path.exists(ICON_PATH):
            self.tray_icon.setIcon(QIcon(ICON_PATH))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
            
        self.tray_menu = QMenu()
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self.show_window)
        
        self.autostart_action = QAction("开机自启", self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(self.check_autostart_status())
        self.autostart_action.triggered.connect(self.toggle_autostart)

        quit_action = QAction("彻底退出", self)
        quit_action.triggered.connect(self.quit_app)
        
        self.tray_menu.addAction(show_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.autostart_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.on_tray_click)
        self.tray_icon.show()

        self.current_format_mode = 'smart'
        self.current_plain_text = ""
        self.has_translation = False
        self.translated_blocks_cache = []
        self.current_text_lang = 'source'
        self.translated_pixmap = None
        self.target_lang = 'zh-CN'
        self.setup_ui()
        self.current_pixmap = None
        self.current_ocr_data = [] 
        
        self.signaler.result_ready.connect(self.update_ui_with_result)
        
        if hasattr(self.ocr_manager, 'SetOcrResultCallback'):
            self.ocr_manager.SetOcrResultCallback(self.global_ocr_callback)

        # 延时注册热键
        print("[DEBUG] 4. 计划在 1 秒后注册系统热键...")
        QTimer.singleShot(1000, self.register_global_hotkey)

    def register_global_hotkey(self):
        print("[DEBUG] 5. 定时器触发，开始注册热键...")
        try:
            # 此时因为设置了 WA_NativeWindow，winId 一定存在且有效
            hwnd = int(self.winId())
            print(f"[DEBUG] 成功获取窗口句柄: {hwnd}")
            
            # 注册热键 (无需 showMinimized)
            result = user32.RegisterHotKey(ctypes.c_void_p(hwnd), HOTKEY_ID, MOD_ALT, VK_A)
            
            if not result:
                print("[WARNING] API返回失败: 热键可能被占用或句柄无效")
            else:
                print("[SUCCESS] 系统级热键 Alt+A 注册成功! (过滤器模式)")
                
        except Exception as e:
            print(f"[ERROR] RegisterHotKey 调用崩溃: {e}")

    def set_selector_reference(self, selector):
        self.selector = selector

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget { font-family: 'Microsoft YaHei', 'SimHei'; }
            ResultWindow { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2b1055, stop:1 #7597de); }
            QTextEdit {
                background-color: rgba(255, 255, 255, 0.95);
                border-radius: 10px; border: none; padding: 16px;
                color: #333; selection-background-color: #7597de;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 0.2);
                border: 1px solid rgba(255, 255, 255, 0.3);
                border-radius: 8px; color: white; font-size: 14px; padding: 8px; font-weight: bold;
            }
            QPushButton:hover { background-color: rgba(255, 255, 255, 0.4); }
            QPushButton:pressed { background-color: rgba(255, 255, 255, 0.1); }
            QPushButton.modeBtn {
                background-color: rgba(255, 255, 255, 0.18);
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 6px;
                color: #e0e0e0;
                font-size: 13px;
                padding: 6px 12px;
                font-weight: normal;
            }
            QPushButton.modeBtn:hover {
                background-color: rgba(255, 255, 255, 0.35);
                color: #ffffff;
            }
            QPushButton.modeBtn:checked {
                background-color: #1890ff;
                border: 1px solid #1890ff;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton.langBtn {
                background-color: rgba(255, 255, 255, 0.18);
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 6px;
                color: #e0e0e0;
                font-size: 13px;
                padding: 6px 10px;
                font-weight: normal;
            }
            QPushButton.langBtn:hover {
                background-color: rgba(255, 255, 255, 0.35);
                color: #ffffff;
            }
            QPushButton.langBtn:checked {
                background-color: #722ed1;
                border: 1px solid #9254de;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton.langBtn:disabled {
                background-color: rgba(255, 255, 255, 0.05);
                color: rgba(255, 255, 255, 0.25);
                border-color: rgba(255, 255, 255, 0.1);
            }
            QPushButton.langToggleBtn {
                background-color: rgba(255, 255, 255, 0.18);
                border: 1px solid rgba(255, 255, 255, 0.28);
                border-radius: 6px;
                color: #ffffff;
                font-size: 13px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QPushButton.langToggleBtn:hover {
                background-color: rgba(255, 255, 255, 0.35);
                border-color: rgba(255, 255, 255, 0.5);
            }
            QPushButton#copyTextBtn {
                background-color: #07c160;
                border: none;
                border-radius: 6px;
                color: #ffffff;
                font-weight: bold;
                font-size: 13px;
                padding: 6px 14px;
            }
            QPushButton#copyTextBtn:hover {
                background-color: #09d66a;
            }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        self.image_label = AutoScalableLabel()
        
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        # 排版模式与语言控制栏 (精简瘦身版)
        mode_container = QWidget()
        mode_layout = QHBoxLayout(mode_container)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(6)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        self.btn_mode_smart = QPushButton("📑 智能排版")
        self.btn_mode_smart.setProperty("class", "modeBtn")
        self.btn_mode_smart.setCheckable(True)
        self.btn_mode_smart.setChecked(True)
        self.btn_mode_smart.setToolTip("自动识别段落与多栏，智能合并跨行文字 (默认)")
        self.btn_mode_smart.clicked.connect(lambda: self.switch_format_mode('smart'))

        self.btn_mode_raw = QPushButton("📄 逐行原始")
        self.btn_mode_raw.setProperty("class", "modeBtn")
        self.btn_mode_raw.setCheckable(True)
        self.btn_mode_raw.setToolTip("保留 OCR 检测的原始单行分行 (适合复制单行代码/诗句)")
        self.btn_mode_raw.clicked.connect(lambda: self.switch_format_mode('raw'))

        self.mode_group.addButton(self.btn_mode_smart)
        self.mode_group.addButton(self.btn_mode_raw)

        self.view_lang_group = QButtonGroup(self)
        self.view_lang_group.setExclusive(True)

        self.btn_lang_src = QPushButton("原文")
        self.btn_lang_src.setProperty("class", "langBtn")
        self.btn_lang_src.setCheckable(True)
        self.btn_lang_src.setChecked(True)
        self.btn_lang_src.setToolTip("查看识别原文")
        self.btn_lang_src.clicked.connect(lambda: self.switch_text_lang('source'))

        self.btn_lang_trans = QPushButton("译文")
        self.btn_lang_trans.setProperty("class", "langBtn")
        self.btn_lang_trans.setCheckable(True)
        self.btn_lang_trans.setEnabled(False)
        self.btn_lang_trans.setToolTip("查看翻译译文")
        self.btn_lang_trans.clicked.connect(lambda: self.switch_text_lang('trans'))

        self.view_lang_group.addButton(self.btn_lang_src)
        self.view_lang_group.addButton(self.btn_lang_trans)

        self.btn_target_lang = QPushButton("🇨🇳 译为中文")
        self.btn_target_lang.setProperty("class", "langToggleBtn")
        self.btn_target_lang.setToolTip("点击切换翻译目标语言 (当前: 译为中文)")
        self.btn_target_lang.clicked.connect(self.toggle_target_lang)

        mode_layout.addWidget(self.btn_mode_smart)
        mode_layout.addWidget(self.btn_mode_raw)
        mode_layout.addSpacing(10)
        mode_layout.addWidget(self.btn_lang_src)
        mode_layout.addWidget(self.btn_lang_trans)
        mode_layout.addSpacing(10)
        mode_layout.addWidget(self.btn_target_lang)
        mode_layout.addStretch()

        self.copy_text_btn = QPushButton("📋 复制文字")
        self.copy_text_btn.setToolTip("复制当前排版格式的文本至剪贴板")
        self.copy_text_btn.setObjectName("copyTextBtn")
        self.copy_text_btn.clicked.connect(self.copy_text_action)
        mode_layout.addWidget(self.copy_text_btn)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("等待识别...")
        self.text_edit.setTabStopDistance(QFontMetrics(self.text_edit.font()).horizontalAdvance(' ') * 8)
        
        self.trans_image_label = AutoScalableLabel()
        self.trans_image_label.hide()

        btn_layout = QHBoxLayout()
        self.copy_img_btn = QPushButton("📷 复制图片并隐藏")
        self.copy_img_btn.setFixedHeight(40)
        self.copy_img_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.trans_btn = QPushButton("🌐 图片翻译") 
        self.trans_btn.setFixedHeight(40)
        self.trans_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.save_btn = QPushButton("💾")
        self.save_btn.setFixedSize(40, 40)
        self.save_btn.setToolTip("保存当前显示的图片")
        
        self.copy_img_btn.clicked.connect(self.copy_image_action)
        self.save_btn.clicked.connect(self.save_image_action)
        self.trans_btn.clicked.connect(self.translate_overlay_action) 
        
        btn_layout.addWidget(self.copy_img_btn)
        btn_layout.addWidget(self.trans_btn)
        btn_layout.addWidget(self.save_btn)
        
        right_layout.addWidget(mode_container)
        right_layout.addWidget(self.text_edit)
        right_layout.addWidget(self.trans_image_label)
        right_layout.addLayout(btn_layout)
        
        main_layout.addWidget(self.image_label, 1)
        main_layout.addWidget(right_container, 1)

    def enter_light_mode(self):
        """深度清理内存"""
        self.text_edit.clear()
        self.image_label.clear_canvas()
        self.trans_image_label.clear_canvas() 
        self.trans_image_label.hide()         
        self.text_edit.show()                 
        self.current_pixmap = None
        self.current_ocr_data = []
        self.current_plain_text = ""
        self.current_format_mode = 'smart'
        self.btn_mode_smart.setChecked(True)
        self.has_translation = False
        self.translated_blocks_cache = []
        self.translated_pixmap = None
        self.current_text_lang = 'source'
        self.target_lang = 'zh-CN'
        if hasattr(self, 'btn_target_lang'):
            self.btn_target_lang.setText("🇨🇳 译为中文")
            self.btn_target_lang.setToolTip("点击切换翻译目标语言 (当前: 译为中文)")
        if hasattr(self, 'btn_lang_src'):
            self.btn_lang_src.setChecked(True)
            self.btn_lang_trans.setEnabled(False)
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setText("🌐 图片翻译")
            self.trans_btn.setEnabled(True)
        
        if self.selector:
            self.selector.clear_memory()
            
        self.hide()
        gc.collect() 
        self.tray_icon.showMessage("OCRCO", "进入静默模式", QSystemTrayIcon.MessageIcon.Information, 500)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape: self.enter_light_mode()
        else: super().keyPressEvent(event)
    def closeEvent(self, event):
        self.enter_light_mode(); event.ignore()
    @staticmethod
    def get_startup_shortcut_path():
        appdata = os.environ.get('APPDATA', '')
        if not appdata:
            return ""
        return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup", f"{APP_NAME}.lnk")

    def get_run_key(self, write=False):
        access = winreg.KEY_ALL_ACCESS if write else winreg.KEY_READ
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, access)

    def get_app_paths(self):
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
            work_dir = os.path.dirname(exe_path)
            cmd = f'"{exe_path}"'
        else:
            main_script = os.path.abspath(__file__)
            work_dir = os.path.dirname(main_script)
            py_exe = sys.executable
            pyw_exe = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            exe_to_use = pyw_exe if os.path.exists(pyw_exe) else py_exe
            cmd = f'"{exe_to_use}" "{main_script}"'
            exe_path = exe_to_use
        return exe_path, work_dir, cmd

    def check_autostart_status(self):
        # 1. 优先检查注册表 Run 项（主流标准自启位置）
        try:
            key = self.get_run_key(write=False)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            pass
        # 2. 备选检查启动文件夹快捷方式
        lnk_path = self.get_startup_shortcut_path()
        if lnk_path and os.path.exists(lnk_path):
            return True
        return False

    def toggle_autostart(self):
        is_checked = self.autostart_action.isChecked()
        lnk_path = self.get_startup_shortcut_path()
        exe_path, work_dir, cmd = self.get_app_paths()

        if is_checked:
            # 开启开机自启：采用单通道注册规范（优先注册表 Run 项，若失败则降级到快捷方式）
            reg_success = False
            try:
                key = self.get_run_key(write=True)
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                winreg.CloseKey(key)
                reg_success = True
                # 注册表写入成功后，确保清理多余快捷方式，避免 Windows 设置中重复出现两个自启项
                if lnk_path and os.path.exists(lnk_path):
                    try:
                        os.remove(lnk_path)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[WARN] 写入注册表自启失败，尝试回退快捷方式模式: {e}")

            if not reg_success and lnk_path:
                try:
                    ps_cmd = (
                        f"$w = New-Object -ComObject WScript.Shell;"
                        f"$s = $w.CreateShortcut('{lnk_path}');"
                        f"$s.TargetPath = '{exe_path}';"
                        f"$s.WorkingDirectory = '{work_dir}';"
                        f"$s.Description = '{APP_NAME} - 智能截图识别';"
                    )
                    if not getattr(sys, 'frozen', False):
                        main_script = os.path.abspath(__file__)
                        ps_cmd += f"$s.Arguments = '\"{main_script}\"';"
                    ps_cmd += "$s.Save()"
                    subprocess.run(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                        capture_output=True,
                        creationflags=0x08000000 if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                    )
                except Exception as e:
                    print(f"[WARN] 创建自启快捷方式失败: {e}")

            self.tray_icon.showMessage("OCRCO", "已开启开机自启动！\n（已加入 Windows 启动项）", QSystemTrayIcon.MessageIcon.Information, 1500)
        else:
            # 关闭自启动：双重清理，确保彻底注销
            if lnk_path and os.path.exists(lnk_path):
                try:
                    os.remove(lnk_path)
                except Exception:
                    pass

            try:
                key = self.get_run_key(write=True)
                winreg.DeleteValue(key, APP_NAME)
                winreg.CloseKey(key)
            except Exception:
                pass

            self.tray_icon.showMessage("OCRCO", "已关闭开机自启动", QSystemTrayIcon.MessageIcon.Information, 1000)
    def show_window(self):
        self.showNormal(); self.activateWindow(); self.setFocus()
    def quit_app(self): QApplication.quit()
    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger: self.show_window()

    def process_ocr(self, pixmap, clean_pixmap=None):
        self.current_pixmap = pixmap
        self.image_label.set_custom_pixmap(pixmap)
        
        self.trans_image_label.clear_canvas()
        self.trans_image_label.hide()
        self.text_edit.show()
        self.text_edit.setHtml("")
        self.text_edit.setPlaceholderText("正在识别中...")
        self.has_translation = False
        self.translated_blocks_cache = []
        self.translated_pixmap = None
        self.current_text_lang = 'source'
        if hasattr(self, 'btn_lang_src'):
            self.btn_lang_src.setChecked(True)
            self.btn_lang_trans.setEnabled(False)
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setText("🌐 图片翻译")
            self.trans_btn.setEnabled(True)
        self.show_window()
        
        target_ocr_pixmap = clean_pixmap if (clean_pixmap and not clean_pixmap.isNull()) else pixmap
        temp_path = os.path.join(APP_PATH, "temp_ocr.png")
        target_ocr_pixmap.save(temp_path)

        try:
            self.ocr_manager.DoOCRTask(temp_path)
        except Exception as e:
            self.text_edit.setPlainText(f"引擎调用异常: {e}")

    def global_ocr_callback(self, img_path, results):
        try:
            self.current_ocr_data = []
            if isinstance(results, dict):
                if 'ocrResult' in results:
                    self.current_ocr_data = results['ocrResult']
                elif 'ocr_response' in results:
                    self.current_ocr_data = results['ocr_response']
            
            if self.current_ocr_data:
                all_text = ''.join(str(b.get('text', '')) for b in self.current_ocr_data)
                clean_text = all_text.strip()
                if len(clean_text) < 2 and not re.search(r'[\u4e00-\u9fa5a-zA-Z0-9]', clean_text):
                    self.signaler.result_ready.emit("") 
                else:
                    self.signaler.result_ready.emit("APPLY_FORMAT")
            else:
                self.signaler.result_ready.emit("未检测到有效文字") 
                
        except Exception as e:
            self.signaler.result_ready.emit(f"解析错误: {e}")

    def update_ui_with_result(self, text):
        try:
            if text == "APPLY_FORMAT":
                self.apply_current_format()
                return
            elif text.startswith("PAINT_OVERLAY"):
                engine_info = text.split(":", 1)[1] if ":" in text else ""
                self.paint_translation_overlay(engine_info)
                return
            elif text.startswith("TRANSLATE_FAILED:"):
                error_msg = text.split(":", 1)[1]
                self.handle_translation_failed(error_msg)
                return
            self.update_text(text)
        except Exception as e:
            print(f"[ERROR] update_ui_with_result error: {e}")
            traceback.print_exc()

    def switch_format_mode(self, mode):
        self.current_format_mode = mode
        if self.trans_image_label.isVisible():
            self.trans_image_label.hide()
            self.text_edit.show()
            if self.has_translation:
                self.trans_btn.setText("🌐 查看翻译图片")
        self.apply_current_format()

    def switch_text_lang(self, lang):
        self.current_text_lang = lang
        if self.trans_image_label.isVisible():
            self.trans_image_label.hide()
            self.text_edit.show()
            if self.has_translation:
                self.trans_btn.setText("🌐 查看翻译图片")
        self.apply_current_format()

    def toggle_target_lang(self):
        if self.target_lang == 'zh-CN':
            self.target_lang = 'en'
            self.btn_target_lang.setText("🇬🇧 译为英文")
            self.btn_target_lang.setToolTip("点击切换翻译目标语言 (当前: 译为英文)")
            self.tray_icon.showMessage("OCRCO", "翻译目标已切换为：英文", QSystemTrayIcon.MessageIcon.Information, 800)
        else:
            self.target_lang = 'zh-CN'
            self.btn_target_lang.setText("🇨🇳 译为中文")
            self.btn_target_lang.setToolTip("点击切换翻译目标语言 (当前: 译为中文)")
            self.tray_icon.showMessage("OCRCO", "翻译目标已切换为：中文", QSystemTrayIcon.MessageIcon.Information, 800)
        
        # 若已有翻译结果，自动以新目标语言重新翻译
        if self.has_translation and self.current_ocr_data:
            self.has_translation = False
            self.translated_blocks_cache = []
            self.translated_pixmap = None
            self.trans_image_label.clear_canvas()
            self.trans_image_label.hide()
            self.text_edit.show()
            self.btn_lang_src.setChecked(True)
            self.btn_lang_trans.setEnabled(False)
            self.translate_overlay_action()

    def apply_current_format(self):
        if self.current_text_lang == 'trans' and getattr(self, 'translated_paragraphs', None):
            paras = self.translated_paragraphs
            if self.current_format_mode in ('smart', 'paragraph'):
                plain_text = '\n\n'.join(p['text'] for p in paras)
                html_content = LayoutFormatter.to_html_paragraphs([p['text'] for p in paras])
            elif self.current_format_mode == 'raw':
                plain_text = '\n'.join(p['text'] for p in paras)
                html_content = LayoutFormatter.to_html_plain(plain_text)
            elif self.current_format_mode == 'markdown':
                plain_text, html_content = LayoutFormatter.format_blocks(
                    self.translated_blocks_cache,
                    mode='markdown'
                )
            else:
                plain_text = '\n\n'.join(p['text'] for p in paras)
                html_content = LayoutFormatter.to_html_paragraphs([p['text'] for p in paras])

            self.current_plain_text = plain_text
            self.text_edit.setHtml(html_content)
            if plain_text.strip():
                QApplication.clipboard().setText(plain_text)
            return

        if not self.current_ocr_data:
            self.text_edit.setHtml("")
            self.text_edit.setPlaceholderText("未识别到文字 (已过滤杂讯)")
            return

        plain_text, html_content = LayoutFormatter.format_blocks(
            self.current_ocr_data,
            mode=self.current_format_mode
        )
        self.current_plain_text = plain_text
        self.text_edit.setHtml(html_content)

        if plain_text.strip():
            QApplication.clipboard().setText(plain_text)

    def copy_text_action(self):
        if self.current_plain_text:
            QApplication.clipboard().setText(self.current_plain_text)
            lang_label = "译文" if (self.current_text_lang == 'trans' and self.has_translation) else "原文"
            self.tray_icon.showMessage("OCRCO", f"{lang_label}排版已复制到剪贴板！", QSystemTrayIcon.MessageIcon.Information, 800)

    def update_text(self, text):
        if not text:
            self.text_edit.setHtml("")
            self.text_edit.setPlaceholderText("未识别到文字 (已过滤杂讯)")
            return

        html_content = f"""
        <html><head><style>
            body {{ font-family: 'Microsoft YaHei', 'SimHei'; font-size: 20px; line-height: 1.4; color: #333; }}
        </style></head><body>
        {text.replace(chr(10), '<br>')}
        </body></html>
        """
        self.text_edit.setHtml(html_content)
        if "未检测到" not in text and "正在识别" not in text:
            QApplication.clipboard().setText(text)

    def copy_image_action(self):
        target_pixmap = self.current_pixmap
        if self.trans_image_label.isVisible() and self.trans_image_label._pixmap:
            target_pixmap = self.trans_image_label._pixmap
            
        if target_pixmap:
            QApplication.clipboard().setPixmap(target_pixmap)
            self.enter_light_mode()

    def save_image_action(self):
        target_pixmap = self.current_pixmap
        if self.trans_image_label.isVisible() and self.trans_image_label._pixmap:
            target_pixmap = self.trans_image_label._pixmap
            
        if target_pixmap:
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getSaveFileName(self, "保存图片", "OCR.png", "PNG (*.png)")
            if path: target_pixmap.save(path)

    def translate_overlay_action(self):
        # 如果已经生成过翻译，点击按钮直接在【翻译图片】和【文字排版】之间切换
        if self.has_translation and self.translated_pixmap:
            if self.trans_image_label.isVisible():
                self.trans_image_label.hide()
                self.text_edit.show()
                self.trans_btn.setText("🌐 查看翻译图片")
            else:
                self.text_edit.hide()
                self.trans_image_label.show()
                self.trans_btn.setText("📝 查看排版文字")
            return

        if not self.current_ocr_data:
            self.tray_icon.showMessage("OCRCO", "没有识别到有效文字，无法进行翻译", QSystemTrayIcon.MessageIcon.Warning, 1500)
            return

        self.trans_btn.setText("⏳ 正在翻译中...")
        self.trans_btn.setEnabled(False)
        threading.Thread(target=self.do_translate_overlay_thread, daemon=True).start()

    def do_translate_overlay_thread(self):
        try:
            paragraphs = LayoutFormatter.extract_paragraphs(self.current_ocr_data)
            if not paragraphs:
                self.signaler.result_ready.emit("TRANSLATE_FAILED:未检测到有效坐标文本")
                return

            texts_to_trans = [p['text'] for p in paragraphs]
            translated_results, engine_used = TranslationService.translate_texts(texts_to_trans, to_lang=self.target_lang)

            self.translated_paragraphs = []
            for i, p in enumerate(paragraphs):
                trans_text = translated_results[i].strip() if i < len(translated_results) else p['text']
                self.translated_paragraphs.append({
                    'text': trans_text,
                    'location': p['location'],
                    'lines': p['lines'],
                    'avg_height': p.get('avg_height', 20),
                    'align': p.get('align', 'left')
                })
            self.translated_blocks_cache = self.translated_paragraphs

            self.signaler.result_ready.emit(f"PAINT_OVERLAY:{engine_used}")

        except Exception as e:
            self.signaler.result_ready.emit(f"TRANSLATE_FAILED:{e}")

    def handle_translation_failed(self, error_msg):
        self.trans_btn.setText("🌐 图片翻译")
        self.trans_btn.setEnabled(True)
        self.tray_icon.showMessage("翻译未完成", error_msg, QSystemTrayIcon.MessageIcon.Warning, 4500)

    def paint_translation_overlay(self, engine_info=""):
        if not self.current_pixmap:
            self.trans_btn.setText("🌐 图片翻译")
            self.trans_btn.setEnabled(True)
            return
        
        new_pixmap = self.current_pixmap.copy()
        painter = QPainter(new_pixmap)
        
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        
        src_image = self.current_pixmap.toImage()
        img_w, img_h = src_image.width(), src_image.height()
        
        target_paras = getattr(self, 'translated_paragraphs', self.translated_blocks_cache)
        
        for item in target_paras:
            loc = item.get('location') or item.get('rect') or {}
            text = item.get('text', '').strip()
            if not text:
                continue
            
            x = int(safe_float(loc.get('left'), 0))
            y = int(safe_float(loc.get('top'), 0))
            r = int(safe_float(loc.get('right'), x))
            b = int(safe_float(loc.get('bottom'), y))
            w = max(1, r - x)
            h = max(1, b - y)
            
            # 多点稳定背景采样
            sample_pts = [
                (max(0, x - 5), max(0, min(img_h - 1, y + 4))),
                (max(0, x - 5), max(0, min(img_h - 1, y + h // 2))),
                (min(img_w - 1, r + 5), max(0, min(img_h - 1, y + 4))),
                (max(0, min(img_w - 1, x + 4)), max(0, y - 5))
            ]
            sample_colors = [src_image.pixelColor(int(px), int(py)) for px, py in sample_pts]
            bg_color = sample_colors[0]

            pad = 1
            lines = item.get('lines', [])
            # 擦除原文字区域
            if lines and len(lines) > 1:
                for l_info in lines:
                    lx = max(0, int(safe_float(l_info.get('left'), x)) - pad)
                    ly = max(0, int(safe_float(l_info.get('top'), y)) - pad)
                    lw = min(img_w - lx, int(safe_float(l_info.get('width'), w)) + pad * 2)
                    lh = min(img_h - ly, int(safe_float(l_info.get('height'), h)) + pad * 2)
                    painter.fillRect(QRect(lx, ly, lw, lh), bg_color)
                # 联合区域整块覆平
                bg_x = max(0, x - pad)
                bg_y = max(0, y - pad)
                bg_w = min(img_w - bg_x, w + pad * 2)
                bg_h = min(img_h - bg_y, h + pad * 2)
                painter.fillRect(QRect(bg_x, bg_y, bg_w, bg_h), bg_color)
            else:
                bg_x = max(0, x - pad)
                bg_y = max(0, y - pad)
                bg_w = min(img_w - bg_x, w + pad * 2)
                bg_h = min(img_h - bg_y, h + pad * 2)
                painter.fillRect(QRect(bg_x, bg_y, bg_w, bg_h), bg_color)

            # 文字颜色根据背景亮度判断
            if bg_color.lightness() < 128:
                text_color = QColor(255, 255, 255)
            else:
                text_color = QColor(25, 25, 25)
            painter.setPen(text_color)

            # 动态字号计算 (基于原图行高自适应缩放)
            orig_line_h = item.get('avg_height', h / max(1, len(lines)))
            target_pixel_size = max(11, min(36, int(orig_line_h * 0.82)))
            base_font = QFont("Microsoft YaHei", target_pixel_size, QFont.Weight.Medium)
            base_font.setPixelSize(target_pixel_size)

            draw_rect = QRect(x, y, w, h)
            fm = QFontMetrics(base_font)
            flags = int(Qt.TextFlag.TextWordWrap)
            br = fm.boundingRect(draw_rect, flags, text)

            # 递减字号直到完整容纳，最低保持 11px 清晰度
            max_allowed_h = int(h + max(8.0, orig_line_h * 0.5))
            while (br.height() > max_allowed_h or br.width() > w + 6) and target_pixel_size > 11:
                target_pixel_size -= 1
                base_font.setPixelSize(target_pixel_size)
                fm = QFontMetrics(base_font)
                br = fm.boundingRect(draw_rect, flags, text)

            painter.setFont(base_font)

            if item.get('align') == 'center':
                align_flag = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
            else:
                align_flag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop

            painter.drawText(draw_rect, int(align_flag | Qt.TextFlag.TextWordWrap), text)
            
        painter.end()
        self.translated_pixmap = new_pixmap
        self.trans_image_label.set_custom_pixmap(new_pixmap)
        self.text_edit.hide()
        self.trans_image_label.show()

        self.has_translation = True
        self.btn_lang_trans.setEnabled(True)
        self.trans_btn.setText("📝 查看排版文字")
        self.trans_btn.setEnabled(True)

        info_str = f"({engine_info})" if engine_info else ""
        target_str = "中文" if self.target_lang == 'zh-CN' else "英文"
        self.tray_icon.showMessage("OCRCO", f"翻译完成 {info_str}！\n已译为{target_str}并按自然段落聚合", QSystemTrayIcon.MessageIcon.Information, 2500)

class ColorButton(QPushButton):
    def __init__(self, color_hex, is_selected=False, parent=None):
        super().__init__(parent)
        self.color = QColor(color_hex)
        self.is_selected = is_selected
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(80, 80, 80), 1))
        painter.setBrush(self.color)
        painter.drawEllipse(1, 1, 16, 16)
        if self.is_selected:
            ring_color = QColor(255, 255, 255) if self.color.lightness() < 160 else QColor(0, 0, 0)
            painter.setPen(QPen(ring_color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(3, 3, 12, 12)

class WidthButton(QPushButton):
    def __init__(self, width_val, dot_radius, is_selected=False, parent=None):
        super().__init__(parent)
        self.width_val = width_val
        self.dot_radius = dot_radius
        self.is_selected = is_selected
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.is_selected:
            painter.setPen(QPen(QColor(24, 144, 255), 1.5))
            painter.setBrush(QColor(255, 255, 255, 30))
            painter.drawRoundedRect(1, 1, 20, 20, 4, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(230, 230, 230))
        cx, cy = self.width() / 2, self.height() / 2
        painter.drawEllipse(QPointF(cx, cy), self.dot_radius, self.dot_radius)

class ScreenshotToolbar(QWidget):
    tool_changed = pyqtSignal(str)
    color_changed = pyqtSignal(QColor)
    width_changed = pyqtSignal(int)
    undo_clicked = pyqtSignal()
    ocr_clicked = pyqtSignal()
    copy_clicked = pyqtSignal()
    save_clicked = pyqtSignal()
    close_clicked = pyqtSignal()
    
    COLORS = ['#F5222D', '#FAAD14', '#52C41A', '#1890FF', '#FFFFFF', '#000000']
    WIDTHS = [(2, 2.5), (4, 4.5), (7, 6.5)]
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.active_tool = None
        self.current_color = QColor(self.COLORS[0])
        self.current_width = self.WIDTHS[1][0]
        self.color_buttons = []
        self.width_buttons = []
        
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('''
            ScreenshotToolbar {
                background-color: rgba(30, 31, 35, 0.95);
                border: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 8px;
            }
            QPushButton.toolBtn, QPushButton.actionBtn {
                background: transparent;
                color: #EDEDED;
                border: none;
                border-radius: 4px;
                padding: 4px 7px;
                font-family: 'Microsoft YaHei', 'Segoe UI';
                font-size: 13px;
            }
            QPushButton.toolBtn:hover, QPushButton.actionBtn:hover {
                background: rgba(255, 255, 255, 0.16);
            }
            QPushButton.toolBtn:checked {
                background: #1890FF;
                color: #FFFFFF;
                font-weight: bold;
            }
            QPushButton#ocrBtn {
                background: #07C160;
                color: #FFFFFF;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 4px;
                font-size: 13px;
                border: 1px solid #06AD56;
            }
            QPushButton#ocrBtn:hover {
                background: #09D66A;
            }
            QPushButton#copyBtn {
                background: #1890FF;
                color: #FFFFFF;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 4px;
                font-size: 13px;
                border: 1px solid #096DD9;
            }
            QPushButton#copyBtn:hover {
                background: #40A9FF;
            }
            QFrame.sep {
                background-color: rgba(255, 255, 255, 0.2);
            }
        ''')
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)
        
        row1 = QWidget()
        r1_layout = QHBoxLayout(row1)
        r1_layout.setContentsMargins(0, 0, 0, 0)
        r1_layout.setSpacing(4)
        
        self.tool_buttons = {}
        tool_defs = [
            ('arrow', '↗ 箭头', '箭头标注 (拖拽绘制)')
        ]
        
        for key, name, tip in tool_defs:
            btn = QPushButton(name)
            btn.setProperty('class', 'toolBtn')
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked, k=key: self.on_tool_clicked(k, checked))
            r1_layout.addWidget(btn)
            self.tool_buttons[key] = btn
            
        self.undo_btn = QPushButton('↩️ 撤销')
        self.undo_btn.setProperty('class', 'actionBtn')
        self.undo_btn.setToolTip('撤销上一步操作 (Ctrl+Z)')
        self.undo_btn.clicked.connect(self.undo_clicked.emit)
        r1_layout.addWidget(self.undo_btn)
        
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setProperty('class', 'sep')
        sep.setFixedWidth(1)
        r1_layout.addWidget(sep)
        
        self.ocr_btn = QPushButton('🔍 识别文字 (Ctrl)')
        self.ocr_btn.setObjectName('ocrBtn')
        self.ocr_btn.setToolTip('提取图片文字并打开排版/翻译面板 (快捷键: 单按Ctrl / 回车 / 双击选区)')
        self.ocr_btn.clicked.connect(self.ocr_clicked.emit)
        r1_layout.addWidget(self.ocr_btn)
        
        self.copy_btn = QPushButton('📋 复制图片 (空格)')
        self.copy_btn.setObjectName('copyBtn')
        self.copy_btn.setToolTip('直接复制截图图片到剪贴板并退出 (快捷键: 空格键 / Ctrl+C)')
        self.copy_btn.clicked.connect(self.copy_clicked.emit)
        r1_layout.addWidget(self.copy_btn)
        
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setProperty('class', 'sep')
        sep2.setFixedWidth(1)
        r1_layout.addWidget(sep2)
        
        self.save_btn = QPushButton('💾 保存')
        self.save_btn.setProperty('class', 'actionBtn')
        self.save_btn.setToolTip('保存截图到文件')
        self.save_btn.clicked.connect(self.save_clicked.emit)
        r1_layout.addWidget(self.save_btn)
        
        self.close_btn = QPushButton('✕')
        self.close_btn.setProperty('class', 'actionBtn')
        self.close_btn.setToolTip('退出截图 (Esc)')
        self.close_btn.clicked.connect(self.close_clicked.emit)
        r1_layout.addWidget(self.close_btn)
        
        main_layout.addWidget(row1)
        
        self.sub_bar = QWidget()
        sb_layout = QHBoxLayout(self.sub_bar)
        sb_layout.setContentsMargins(4, 2, 4, 2)
        sb_layout.setSpacing(6)
        
        for w_val, dot_r in self.WIDTHS:
            w_btn = WidthButton(w_val, dot_r, is_selected=(w_val == self.current_width))
            w_btn.clicked.connect(lambda _, w=w_val: self.set_width(w))
            sb_layout.addWidget(w_btn)
            self.width_buttons.append(w_btn)
            
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setProperty('class', 'sep')
        sep2.setFixedWidth(1)
        sb_layout.addWidget(sep2)
        
        for c_hex in self.COLORS:
            c_btn = ColorButton(c_hex, is_selected=(c_hex == self.COLORS[0]))
            c_btn.clicked.connect(lambda _, c=c_hex: self.set_color(c))
            sb_layout.addWidget(c_btn)
            self.color_buttons.append(c_btn)
            
        sb_layout.addStretch()
        main_layout.addWidget(self.sub_bar)
        self.sub_bar.hide()

    def on_tool_clicked(self, key, checked):
        if checked:
            for k, btn in self.tool_buttons.items():
                if k != key:
                    btn.setChecked(False)
            self.active_tool = key
            self.sub_bar.show()
            self.adjustSize()
        else:
            self.active_tool = None
            self.sub_bar.hide()
            self.adjustSize()
        self.tool_changed.emit(self.active_tool)

    def set_color(self, hex_val):
        self.current_color = QColor(hex_val)
        for btn in self.color_buttons:
            btn.is_selected = (btn.color == self.current_color)
            btn.update()
        self.color_changed.emit(self.current_color)

    def set_width(self, w_val):
        self.current_width = w_val
        for btn in self.width_buttons:
            btn.is_selected = (btn.width_val == w_val)
            btn.update()
        self.width_changed.emit(w_val)

    def reset_tools(self):
        for btn in self.tool_buttons.values():
            btn.setChecked(False)
        self.active_tool = None
        self.sub_bar.hide()
        self.adjustSize()

class BoxDetector:
    IGNORE_CLASSES = {
        'ThumbnailDeviceHelperWnd', 'tooltips_class32', 'DummyDWMListenerWindow',
        'EdgeUiInputTopWndClass', 'Windows.UI.Core.CoreWindow', 'ForegroundStaging',
        'XamlExplorerHostIslandWindow', 'TopLevelWindowForOverflowXamlIsland',
        'Cua.AgentCursorOverlay.default'
    }
    DESKTOP_CLASSES = {'Progman', 'WorkerW'}

    @classmethod
    def get_system_hierarchy(cls, self_hwnd=0):
        windows = []
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        WS_EX_TRANSPARENT = 0x00000020

        def get_rect(hwnd):
            rect = wintypes.RECT()
            res = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
            if res != 0 or rect.right - rect.left <= 0:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

        def get_class_name(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            return buf.value

        hdesk = None
        try:
            hdesk = user32.OpenDesktopW('Default', 0, False, 0x0100)
            raw_windows = []

            def enum_proc(hwnd, _):
                if hwnd == self_hwnd:
                    return True
                if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                    ex_style = user32.GetWindowLongW(hwnd, -20)
                    if not (ex_style & WS_EX_TRANSPARENT):
                        cname = get_class_name(hwnd)
                        if cname not in cls.IGNORE_CLASSES:
                            x, y, w, h = get_rect(hwnd)
                            if w > 40 and h > 40:
                                raw_windows.append((hwnd, cname, (x, y, w, h)))
                return True

            EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc)
            try:
                user32.EnumWindows(EnumProc, 0)
            except Exception:
                pass

            if not raw_windows and hdesk:
                user32.EnumDesktopWindows(hdesk, EnumProc, 0)

            for hwnd, cname, (x, y, w, h) in raw_windows:
                is_desktop = cname in cls.DESKTOP_CLASSES
                children = []
                if not is_desktop:
                    def child_proc(chwnd, _):
                        if user32.IsWindowVisible(chwnd):
                            cx, cy, cw, ch = get_rect(chwnd)
                            if cw >= 60 and ch >= 25 and (cw < w or ch < h):
                                children.append((cx, cy, cw, ch))
                        return True
                    c_cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(child_proc)
                    try:
                        user32.EnumChildWindows(hwnd, c_cb, 0)
                    except Exception:
                        pass
                    children.sort(key=lambda c: c[2] * c[3])

                windows.append({
                    'hwnd': hwnd,
                    'class': cname,
                    'rect': (x, y, w, h),
                    'children': children,
                    'is_desktop': is_desktop
                })
        except Exception as e:
            print(f"[WARN] get_system_hierarchy error: {e}")
        finally:
            if hdesk:
                try:
                    user32.CloseDesktop(hdesk)
                except Exception:
                    pass

        return windows

    @staticmethod
    def get_visual_cards(pixmap, screen_w, screen_h):
        cards = []
        try:
            if not pixmap:
                return []
            qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
            w = qimg.width()
            h = qimg.height()
            if w <= 0 or h <= 0:
                return []
            ptr = qimg.bits()
            ptr.setsize(h * qimg.bytesPerLine())
            arr = np.frombuffer(ptr, np.uint8).reshape((h, qimg.bytesPerLine()))[:, :w]

            blurred = cv2.GaussianBlur(arr, (5, 5), 0)
            edges = cv2.Canny(blurred, 40, 120)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
            contours, _ = cv2.findContours(closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

            for c in contours:
                bx, by, bw, bh = cv2.boundingRect(c)
                # 排除过小（文字、按钮、图标）或过大（整个屏幕）
                if bw < 100 or bh < 60:
                    continue
                if bw >= screen_w * 0.98 and bh >= screen_h * 0.98:
                    continue
                bbox_area = bw * bh
                if bbox_area < 10000:
                    continue
                aspect = bw / float(bh)
                if aspect < 0.08 or aspect > 12.0:
                    continue
                # 形状必须近似矩形，禁止非矩形噪点
                peri = cv2.arcLength(c, True)
                approx = cv2.approxPolyDP(c, 0.03 * peri, True)
                if len(approx) not in (4, 5, 6):
                    continue
                # 实心度检查：面积占比必须 >= 0.75，彻底排除稀疏文字轮廓
                area = cv2.contourArea(c)
                if (area / bbox_area) < 0.75:
                    continue
                cards.append((bx, by, bw, bh, bbox_area))

            cards.sort(key=lambda item: item[4])
        except Exception as e:
            print(f"[WARN] get_visual_cards error: {e}")
        return cards

    @classmethod
    def find_target(cls, windows, visual_cards, x, y, default_rect):
        # 1. 优先按 Win32 Z-order（从最顶层至底层窗口）匹配
        for win in windows:
            if win['is_desktop']:
                continue
            wx, wy, ww, wh = win['rect']
            if wx <= x < wx + ww and wy <= y < wy + wh:
                # 命中了当前最顶层激活窗口！
                # 优先匹配此窗口内部的大块图片/卡片视觉容器
                for vx, vy, vw, vh, _ in visual_cards:
                    if vx <= x < vx + vw and vy <= y < vy + vh:
                        if vx >= wx - 5 and vy >= wy - 5 and (vx + vw) <= (wx + ww + 5) and (vy + vh) <= (wy + wh + 5):
                            return QRect(vx, vy, vw, vh)

                # 其次匹配该窗口内的子控件/子容器（如编辑区、内容面板、工具栏）
                for cx, cy, cw, ch in win['children']:
                    if cx <= x < cx + cw and cy <= y < cy + ch:
                        return QRect(cx, cy, cw, ch)

                # 最终返回该窗口整体
                return QRect(wx, wy, ww, wh)

        # 2. 如果未命中任何有效应用窗口，检查是否有独立的规整视觉卡片
        for vx, vy, vw, vh, _ in visual_cards:
            if vx <= x < vx + vw and vy <= y < vy + vh:
                return QRect(vx, vy, vw, vh)

        # 3. 兜底返回当前鼠标所在屏幕/主屏幕矩形
        return default_rect

class ScreenshotSelector(QWidget):
    HANDLE_SIZE = 8

    def __init__(self, result_window):
        super().__init__()
        self.result_window = result_window
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        
        self.full_screen_pixmap = None
        self.system_windows = []
        self.visual_cards = []
        
        # State: 'HOVER', 'DRAGGING', 'SELECTED'
        self.state = 'HOVER'
        self.hover_rect = None
        self.selected_rect = None
        
        # Dragging state
        self.is_pressing = False
        self.press_pos = None
        self.current_drag_rect = None
        
        # Handle / Move state
        self.drag_mode = None  # None, 'move', 'resize'
        self.active_handle = None
        self.move_offset = None
        
        # Annotation state
        self.annotations = []
        self.active_tool = None # 'rect', 'ellipse', 'arrow', 'pen', 'text', None
        self.current_shape = None
        self.draw_color = QColor(245, 34, 45) # Red default
        self.line_width = 4                    # Medium default
        self.font_size = 15
        
        # Child widgets: Toolbar & Text Editor
        self.toolbar = ScreenshotToolbar(self)
        self.toolbar.hide()
        self.toolbar.tool_changed.connect(self.on_tool_changed)
        self.toolbar.color_changed.connect(self.on_color_changed)
        self.toolbar.width_changed.connect(self.on_width_changed)
        self.toolbar.undo_clicked.connect(self.undo_annotation)
        self.toolbar.ocr_clicked.connect(self.commit_ocr)
        self.toolbar.copy_clicked.connect(self.copy_to_clipboard)
        self.toolbar.save_clicked.connect(self.save_to_file)
        self.toolbar.close_clicked.connect(self.close_and_clear)
        
        self.text_editor = QLineEdit(self)
        self.text_editor.hide()
        self.text_editor.returnPressed.connect(self.commit_text_editor)
        self.text_editor_pos = None

    def clear_memory(self):
        self.full_screen_pixmap = None
        self.system_windows = []
        self.visual_cards = []
        self.annotations = []
        self.selected_rect = None
        self.hover_rect = None
        self.active_tool = None
        self.current_shape = None
        if hasattr(self, 'toolbar') and self.toolbar:
            self.toolbar.reset_tools()
            self.toolbar.hide()
        if hasattr(self, 'text_editor') and self.text_editor:
            self.text_editor.clear()
            self.text_editor.hide()

    def get_current_screen_rect(self, pos):
        for s in QGuiApplication.screens():
            if s.geometry().contains(pos):
                return s.geometry()
        return self.rect()

    def start_capture(self):
        self.clear_memory()
        
        # 1. 优先获取物理屏幕和系统真实窗口层次（在截屏全屏覆盖前获取真实Z序）
        try:
            hwnd_val = int(self.winId()) if self.winId() else 0
        except Exception:
            hwnd_val = 0
            
        self.system_windows = BoxDetector.get_system_hierarchy(self_hwnd=hwnd_val)
        
        v_rect = QRect()
        for s in QGuiApplication.screens():
            v_rect = v_rect.united(s.geometry())
            
        screen = QGuiApplication.primaryScreen()
        self.full_screen_pixmap = screen.grabWindow(0, v_rect.x(), v_rect.y(), v_rect.width(), v_rect.height())
        self.setGeometry(v_rect)
        
        # 2. 提取有效的大块规整卡片/容器（完全过滤孤立文字和杂乱无序的噪点轮廓）
        self.visual_cards = BoxDetector.get_visual_cards(self.full_screen_pixmap, v_rect.width(), v_rect.height())
        
        self.state = 'HOVER'
        cursor_pos = QCursor.pos()
        target_screen_rect = self.get_current_screen_rect(cursor_pos)
        self.hover_rect = BoxDetector.find_target(
            self.system_windows,
            self.visual_cards,
            cursor_pos.x(), cursor_pos.y(),
            target_screen_rect
        )
        self.selected_rect = None
        self.annotations = []
        self.active_tool = None
        self.current_shape = None
        
        if self.toolbar:
            self.toolbar.reset_tools()
            self.toolbar.hide()
            
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def get_handles(self):
        if not self.selected_rect or self.selected_rect.isNull():
            return {}
        r = self.selected_rect
        hs = self.HANDLE_SIZE
        half = hs // 2
        
        cx = r.center().x()
        cy = r.center().y()
        
        return {
            'TL': QRect(r.left() - half, r.top() - half, hs, hs),
            'T':  QRect(cx - half, r.top() - half, hs, hs),
            'TR': QRect(r.right() - half, r.top() - half, hs, hs),
            'R':  QRect(r.right() - half, cy - half, hs, hs),
            'BR': QRect(r.right() - half, r.bottom() - half, hs, hs),
            'B':  QRect(cx - half, r.bottom() - half, hs, hs),
            'BL': QRect(r.left() - half, r.bottom() - half, hs, hs),
            'L':  QRect(r.left() - half, cy - half, hs, hs),
        }
        
    def get_handle_at(self, pos):
        for name, rect in self.get_handles().items():
            if rect.adjusted(-3, -3, 3, 3).contains(pos):
                return name
        return None

    def get_cursor_for_handle(self, handle):
        if handle in ('TL', 'BR'):
            return Qt.CursorShape.SizeFDiagCursor
        elif handle in ('TR', 'BL'):
            return Qt.CursorShape.SizeBDiagCursor
        elif handle in ('T', 'B'):
            return Qt.CursorShape.SizeVerCursor
        elif handle in ('L', 'R'):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.ArrowCursor

    def resize_selected_rect(self, handle, pos):
        r = QRect(self.selected_rect)
        if 'L' in handle:
            r.setLeft(min(pos.x(), r.right() - 20))
        if 'R' in handle:
            r.setRight(max(pos.x(), r.left() + 20))
        if 'T' in handle:
            r.setTop(min(pos.y(), r.bottom() - 20))
        if 'B' in handle:
            r.setBottom(max(pos.y(), r.top() + 20))
        self.selected_rect = r.normalized()
        self.update_toolbar_position()

    def move_selected_rect(self, pos):
        new_top_left = pos - self.move_offset
        r = QRect(new_top_left, self.selected_rect.size())
        
        if r.left() < 0:
            r.moveLeft(0)
        if r.top() < 0:
            r.moveTop(0)
        if r.right() > self.width():
            r.moveRight(self.width())
        if r.bottom() > self.height():
            r.moveBottom(self.height())
            
        self.selected_rect = r
        self.update_toolbar_position()

    def update_toolbar_position(self):
        if not self.selected_rect or self.selected_rect.isNull():
            self.toolbar.hide()
            return

        tb_w = self.toolbar.sizeHint().width()
        tb_h = self.toolbar.sizeHint().height()
        
        x = self.selected_rect.right() - tb_w + 1
        y = self.selected_rect.bottom() + 10
        
        if x + tb_w > self.width() - 8:
            x = self.width() - tb_w - 8
        if x < 8:
            x = 8
            
        if y + tb_h > self.height() - 8:
            y = self.selected_rect.top() - tb_h - 10
            if y < 8:
                y = self.selected_rect.bottom() - tb_h - 8

        self.toolbar.move(x, y)
        self.toolbar.show()
        self.toolbar.raise_()

    def enter_selected_state(self):
        self.state = 'SELECTED'
        self.is_pressing = False
        self.drag_mode = None
        self.active_handle = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update_toolbar_position()
        self.update()

    def draw_size_badge(self, painter, rect):
        text = f"{rect.width()} × {rect.height()}"
        font = QFont("Microsoft YaHei", 9, QFont.Weight.Bold)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(text)
        badge_w = text_w + 14
        badge_h = 22
        
        bx = rect.left()
        by = rect.top() - badge_h - 4
        if by < 4:
            by = rect.top() + 4
        if bx + badge_w > self.width() - 4:
            bx = self.width() - badge_w - 4
            
        badge_rect = QRect(bx, by, badge_w, badge_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(25, 25, 28, 210))
        painter.drawRoundedRect(badge_rect, 4, 4)
        
        painter.setPen(QColor(240, 240, 240))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, text)

    def draw_top_hint(self, painter):
        text = "🖱️ 鼠标框选 | 空格键: 复制图片 | Ctrl / 回车 / 双击: 识别文字 | ↗ 箭头标注 | Esc: 退出"
        font = QFont("Microsoft YaHei", 10)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(text)
        badge_w = text_w + 24
        badge_h = 30
        
        bx = (self.width() - badge_w) // 2
        by = 16
        
        badge_rect = QRect(bx, by, badge_w, badge_h)
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(QColor(25, 25, 28, 210))
        painter.drawRoundedRect(badge_rect, 6, 6)
        
        painter.setPen(QColor(230, 230, 230))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, text)

    def draw_annotation(self, painter, ann):
        a_type = ann.get('type')
        color = ann.get('color', self.draw_color)
        width = ann.get('width', self.line_width)
        
        if a_type == 'rect':
            pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRect(ann['start'], ann['end']).normalized())
            
        elif a_type == 'ellipse':
            pen = QPen(color, width, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRect(ann['start'], ann['end']).normalized())
            
        elif a_type == 'arrow':
            start_pt = ann['start']
            end_pt = ann['end']
            dx = end_pt.x() - start_pt.x()
            dy = end_pt.y() - start_pt.y()
            dist = math.hypot(dx, dy)
            if dist < 4:
                return
            
            angle = math.atan2(dy, dx)
            head_len = max(14.0, width * 3.5)
            head_angle = math.radians(25)
            
            tip = QPointF(end_pt.x(), end_pt.y())
            p1 = QPointF(
                tip.x() - head_len * math.cos(angle - head_angle),
                tip.y() - head_len * math.sin(angle - head_angle)
            )
            p2 = QPointF(
                tip.x() - head_len * math.cos(angle + head_angle),
                tip.y() - head_len * math.sin(angle + head_angle)
            )
            indent = QPointF(
                tip.x() - (head_len * 0.7) * math.cos(angle),
                tip.y() - (head_len * 0.7) * math.sin(angle)
            )
            
            pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(QPointF(start_pt.x(), start_pt.y()), indent)
            
            painter.setBrush(color)
            poly = QPolygonF([tip, p1, indent, p2])
            painter.drawPolygon(poly)
            
        elif a_type == 'pen':
            pts = ann.get('points', [])
            if len(pts) >= 2:
                pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                for i in range(len(pts) - 1):
                    painter.drawLine(pts[i], pts[i+1])
                    
        elif a_type == 'text':
            painter.setPen(color)
            font = QFont("Microsoft YaHei", ann.get('font_size', 15), QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(ann['pos'], ann['text'])

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.full_screen_pixmap:
            return
            
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.drawPixmap(0, 0, self.full_screen_pixmap)
        
        mask_color = QColor(0, 0, 0, 95)
        
        if self.state == 'HOVER':
            painter.fillRect(self.rect(), mask_color)
            if self.hover_rect and not self.hover_rect.isNull():
                painter.drawPixmap(self.hover_rect, self.full_screen_pixmap, self.hover_rect)
                pen = QPen(QColor(0, 160, 255), 2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self.hover_rect)
                self.draw_size_badge(painter, self.hover_rect)
            self.draw_top_hint(painter)
            
        elif self.state == 'DRAGGING':
            painter.fillRect(self.rect(), mask_color)
            if self.current_drag_rect and not self.current_drag_rect.isNull():
                painter.drawPixmap(self.current_drag_rect, self.full_screen_pixmap, self.current_drag_rect)
                pen = QPen(QColor(0, 160, 255), 2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self.current_drag_rect)
                self.draw_size_badge(painter, self.current_drag_rect)
                
        elif self.state == 'SELECTED':
            if self.selected_rect and not self.selected_rect.isNull():
                r = self.selected_rect
                w_full, h_full = self.width(), self.height()
                painter.fillRect(0, 0, w_full, r.top(), mask_color)
                painter.fillRect(0, r.bottom() + 1, w_full, h_full - r.bottom() - 1, mask_color)
                painter.fillRect(0, r.top(), r.left(), r.height(), mask_color)
                painter.fillRect(r.right() + 1, r.top(), w_full - r.right() - 1, r.height(), mask_color)
                
                painter.save()
                painter.setClipRect(self.selected_rect)
                for ann in self.annotations:
                    self.draw_annotation(painter, ann)
                if self.current_shape:
                    self.draw_annotation(painter, self.current_shape)
                painter.restore()
                
                border_pen = QPen(QColor(7, 193, 96), 2)
                painter.setPen(border_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self.selected_rect)
                
                handle_pen = QPen(QColor(7, 193, 96), 1.5)
                handle_brush = QBrush(QColor(255, 255, 255))
                painter.setPen(handle_pen)
                painter.setBrush(handle_brush)
                for h_rect in self.get_handles().values():
                    painter.drawRect(h_rect)
                    
                self.draw_size_badge(painter, self.selected_rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            if self.state == 'SELECTED':
                if self.text_editor.isVisible():
                    self.commit_text_editor()
                else:
                    self.selected_rect = None
                    self.annotations.clear()
                    self.active_tool = None
                    if self.toolbar:
                        self.toolbar.reset_tools()
                        self.toolbar.hide()
                    self.state = 'HOVER'
                    self.setCursor(Qt.CursorShape.ArrowCursor)
                    self.update()
            else:
                self.close_and_clear()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self.text_editor.isVisible() and not self.text_editor.geometry().contains(event.pos()):
                self.commit_text_editor()

            if self.state == 'HOVER':
                self.press_pos = event.pos()
                self.is_pressing = True
                self.drag_mode = None
                
            elif self.state == 'SELECTED':
                if self.selected_rect.contains(event.pos()):
                    if self.active_tool in ('rect', 'ellipse', 'arrow'):
                        self.current_shape = {
                            'type': self.active_tool,
                            'start': event.pos(),
                            'end': event.pos(),
                            'color': self.draw_color,
                            'width': self.line_width
                        }
                        self.is_pressing = True
                    elif self.active_tool == 'pen':
                        self.current_shape = {
                            'type': 'pen',
                            'points': [event.pos()],
                            'color': self.draw_color,
                            'width': self.line_width
                        }
                        self.is_pressing = True
                    elif self.active_tool == 'text':
                        self.start_text_editor(event.pos())
                    else:
                        handle = self.get_handle_at(event.pos())
                        if handle:
                            self.drag_mode = 'resize'
                            self.active_handle = handle
                        else:
                            self.drag_mode = 'move'
                            self.move_offset = event.pos() - self.selected_rect.topLeft()
                        self.is_pressing = True
                else:
                    handle = self.get_handle_at(event.pos())
                    if handle:
                        self.drag_mode = 'resize'
                        self.active_handle = handle
                        self.is_pressing = True
                    elif self.active_tool is None:
                        self.selected_rect = None
                        self.annotations.clear()
                        self.toolbar.hide()
                        self.state = 'DRAGGING'
                        self.press_pos = event.pos()
                        self.current_drag_rect = QRect(event.pos(), event.pos())
                        self.is_pressing = True
                        self.update()

    def mouseMoveEvent(self, event):
        pos = event.pos()
        
        if self.toolbar and self.toolbar.isVisible() and self.toolbar.geometry().contains(pos):
            return
            
        if self.text_editor and self.text_editor.isVisible() and self.text_editor.geometry().contains(pos):
            return

        if self.state == 'HOVER':
            if self.is_pressing:
                if (pos - self.press_pos).manhattanLength() > 5:
                    self.state = 'DRAGGING'
                    self.current_drag_rect = QRect(self.press_pos, pos).normalized()
                    self.update()
            else:
                target_screen_rect = self.get_current_screen_rect(pos)
                found_rect = BoxDetector.find_target(
                    self.system_windows,
                    self.visual_cards,
                    pos.x(), pos.y(),
                    target_screen_rect
                )
                if found_rect and not found_rect.isNull():
                    self.hover_rect = found_rect
                else:
                    self.hover_rect = target_screen_rect
                self.update()

        elif self.state == 'DRAGGING':
            self.current_drag_rect = QRect(self.press_pos, pos).normalized()
            self.update()

        elif self.state == 'SELECTED':
            if self.is_pressing:
                if self.active_tool in ('rect', 'ellipse', 'arrow') and self.current_shape:
                    self.current_shape['end'] = pos
                    self.update()
                elif self.active_tool == 'pen' and self.current_shape:
                    self.current_shape['points'].append(pos)
                    self.update()
                elif self.drag_mode == 'move':
                    self.move_selected_rect(pos)
                    self.update()
                elif self.drag_mode == 'resize':
                    self.resize_selected_rect(self.active_handle, pos)
                    self.update()
            else:
                if self.active_tool in ('rect', 'ellipse', 'arrow', 'pen'):
                    if self.selected_rect.contains(pos):
                        self.setCursor(Qt.CursorShape.CrossCursor)
                    else:
                        self.setCursor(Qt.CursorShape.ArrowCursor)
                elif self.active_tool == 'text':
                    if self.selected_rect.contains(pos):
                        self.setCursor(Qt.CursorShape.IBeamCursor)
                    else:
                        self.setCursor(Qt.CursorShape.ArrowCursor)
                else:
                    handle = self.get_handle_at(pos)
                    if handle:
                        self.setCursor(self.get_cursor_for_handle(handle))
                    elif self.selected_rect.contains(pos):
                        self.setCursor(Qt.CursorShape.SizeAllCursor)
                    else:
                        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.state == 'HOVER':
                if self.is_pressing:
                    if (event.pos() - self.press_pos).manhattanLength() <= 5:
                        if self.hover_rect and self.hover_rect.width() > 10 and self.hover_rect.height() > 10:
                            self.selected_rect = self.hover_rect
                            self.enter_selected_state()
                    self.is_pressing = False

            elif self.state == 'DRAGGING':
                self.is_pressing = False
                if self.current_drag_rect and self.current_drag_rect.width() > 10 and self.current_drag_rect.height() > 10:
                    self.selected_rect = self.current_drag_rect
                    self.enter_selected_state()
                else:
                    self.state = 'HOVER'
                    self.update()

            elif self.state == 'SELECTED':
                self.is_pressing = False
                self.drag_mode = None
                self.active_handle = None
                if self.current_shape:
                    if self.current_shape['type'] in ('rect', 'ellipse', 'arrow'):
                        dx = abs(self.current_shape['end'].x() - self.current_shape['start'].x())
                        dy = abs(self.current_shape['end'].y() - self.current_shape['start'].y())
                        if dx > 3 or dy > 3:
                            self.annotations.append(self.current_shape)
                    elif self.current_shape['type'] == 'pen':
                        if len(self.current_shape.get('points', [])) > 1:
                            self.annotations.append(self.current_shape)
                    self.current_shape = None
                    self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self.text_editor.isVisible():
                self.text_editor.clear()
                self.text_editor.hide()
                self.update()
            elif self.state == 'SELECTED':
                self.selected_rect = None
                self.annotations.clear()
                self.active_tool = None
                self.toolbar.reset_tools()
                self.toolbar.hide()
                self.state = 'HOVER'
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.update()
            else:
                self.close_and_clear()

        elif event.key() == Qt.Key.Key_Space:
            # 【用户快捷操作】：按空格直接复制图片并退出
            if self.state == 'SELECTED':
                self.copy_to_clipboard()
            elif self.state == 'HOVER' and self.hover_rect and not self.hover_rect.isNull():
                self.selected_rect = self.hover_rect
                self.copy_to_clipboard()

        elif event.key() == Qt.Key.Key_Control:
            self._ctrl_only_pressed = True

        elif event.key() == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._ctrl_only_pressed = False
            self.undo_annotation()

        elif event.key() == Qt.Key.Key_C and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._ctrl_only_pressed = False
            if self.state == 'SELECTED':
                self.copy_to_clipboard()
            elif self.state == 'HOVER' and self.hover_rect and not self.hover_rect.isNull():
                self.selected_rect = self.hover_rect
                self.copy_to_clipboard()

        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._ctrl_only_pressed = False
            if self.state == 'SELECTED' and not self.text_editor.hasFocus():
                self.commit_ocr()
            elif self.state == 'HOVER' and self.hover_rect and not self.hover_rect.isNull():
                self.selected_rect = self.hover_rect
                self.commit_ocr()

        else:
            self._ctrl_only_pressed = False
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Control and getattr(self, '_ctrl_only_pressed', False):
            self._ctrl_only_pressed = False
            # 【用户快捷操作】：单按 Ctrl 键直接触发识别文字并弹出面板
            if self.state == 'SELECTED':
                self.commit_ocr()
            elif self.state == 'HOVER' and self.hover_rect and not self.hover_rect.isNull():
                self.selected_rect = self.hover_rect
                self.commit_ocr()
        else:
            super().keyReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 【用户双击快捷操作】：双击选区直接进入识别文字
            if self.state == 'SELECTED' and self.selected_rect and self.selected_rect.contains(event.pos()):
                self.commit_ocr()
                event.accept()
                return
            elif self.state == 'HOVER' and self.hover_rect and self.hover_rect.contains(event.pos()):
                self.selected_rect = self.hover_rect
                self.commit_ocr()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def on_tool_changed(self, tool):
        if self.text_editor.isVisible():
            self.commit_text_editor()
        self.active_tool = tool
        self.update()

    def on_color_changed(self, color):
        self.draw_color = color

    def on_width_changed(self, width):
        self.line_width = width
        if width <= 2:
            self.font_size = 12
        elif width <= 4:
            self.font_size = 15
        else:
            self.font_size = 20

    def undo_annotation(self):
        if self.annotations:
            self.annotations.pop()
            self.update()

    def start_text_editor(self, pos):
        if self.text_editor.isVisible():
            self.commit_text_editor()
            
        self.text_editor_pos = pos
        ed_w = min(220, self.selected_rect.right() - pos.x() - 4)
        if ed_w < 100:
            ed_w = 120
            pos = QPoint(max(self.selected_rect.left() + 4, pos.x() - 100), pos.y())
            
        self.text_editor.setGeometry(pos.x(), pos.y(), ed_w, 32)
        font = QFont("Microsoft YaHei", self.font_size, QFont.Weight.Bold)
        self.text_editor.setFont(font)
        self.text_editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: rgba(20, 20, 22, 0.85);
                color: {self.draw_color.name()};
                border: 1.5px dashed {self.draw_color.name()};
                border-radius: 4px;
                padding: 2px 6px;
            }}
        """)
        self.text_editor.clear()
        self.text_editor.show()
        self.text_editor.setFocus()

    def commit_text_editor(self):
        if not self.text_editor.isVisible():
            return
        txt = self.text_editor.text().strip()
        if txt and self.text_editor_pos:
            text_pos = self.text_editor.pos() + QPoint(6, self.text_editor.height() - 8)
            self.annotations.append({
                'type': 'text',
                'pos': text_pos,
                'text': txt,
                'color': self.draw_color,
                'font_size': self.font_size
            })
        self.text_editor.clear()
        self.text_editor.hide()
        self.update()

    def render_final_pixmaps(self):
        crop_rect = self.selected_rect.intersected(QRect(0, 0, self.full_screen_pixmap.width(), self.full_screen_pixmap.height()))
        clean_crop = self.full_screen_pixmap.copy(crop_rect)
        annotated_crop = clean_crop.copy()
        
        if self.annotations:
            p = QPainter(annotated_crop)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            p.translate(-crop_rect.topLeft())
            for ann in self.annotations:
                self.draw_annotation(p, ann)
            p.end()
            
        return clean_crop, annotated_crop

    def commit_ocr(self):
        self.commit_text_editor()
        clean_crop, annotated_crop = self.render_final_pixmaps()
        self.close_and_clear()
        self.result_window.process_ocr(annotated_crop, clean_pixmap=clean_crop)

    def copy_to_clipboard(self):
        self.commit_text_editor()
        _, annotated_crop = self.render_final_pixmaps()
        QApplication.clipboard().setPixmap(annotated_crop)
        self.close_and_clear()
        if self.result_window and self.result_window.tray_icon.isVisible():
            self.result_window.tray_icon.showMessage("OCRCO", "截图已复制到剪贴板", QSystemTrayIcon.MessageIcon.Information, 800)

    def save_to_file(self):
        self.commit_text_editor()
        _, annotated_crop = self.render_final_pixmaps()
        self.hide()
        path, _ = QFileDialog.getSaveFileName(self, "保存截图", "OCR_Screenshot.png", "PNG (*.png);;JPG (*.jpg)")
        if path:
            annotated_crop.save(path)
            if self.result_window and self.result_window.tray_icon.isVisible():
                self.result_window.tray_icon.showMessage("OCRCO", f"截图已保存到: {os.path.basename(path)}", QSystemTrayIcon.MessageIcon.Information, 800)
        self.close_and_clear()

    def close_and_clear(self):
        self.clear_memory()
        self.repaint()
        QApplication.processEvents()
        self.hide()

if __name__ == "__main__":
    # 单实例运行锁：防止多次启动导致热键冲突或托盘重复
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_SingleInstance_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print(f"[INFO] {APP_NAME} 已经在运行中，请勿重复启动。")
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    
    signaler = OCRSignaler()
    
    # 【核心修复】安装原生事件过滤器
    # 将热键监听任务交给 App 处理，而不是主窗口
    hotkey_filter = GlobalHotkeyFilter(signaler)
    app.installNativeEventFilter(hotkey_filter)
    
    ocr_manager = None 
    
    try:
        if not os.path.exists(WECHAT_OCR_EXE):
            raise FileNotFoundError(f"找不到OCR引擎文件: {WECHAT_OCR_EXE}")
            
        ocr_manager = OcrManager(WECHAT_LIB_DIR)
        ocr_manager.SetExePath(WECHAT_OCR_EXE)
        ocr_manager.SetUsrLibDir(WECHAT_LIB_DIR)
        ocr_manager.StartWeChatOCR()
        
    except Exception as e:
        QMessageBox.critical(None, "致命错误", f"OCR引擎启动失败！\n\n原因: {str(e)}\n\n请检查 ocr_engine 文件夹是否完整。")
        sys.exit(1)

    if ocr_manager:
        res_win = ResultWindow(ocr_manager, signaler)
        selector = ScreenshotSelector(res_win)
        
        res_win.set_selector_reference(selector) 
        
        # 连接信号：当过滤器收到 Alt+A 时，触发截图
        signaler.hotkey_pressed.connect(selector.start_capture)
        
        if res_win.tray_icon.isVisible():
            res_win.tray_icon.showMessage("OCRCO", "服务已就绪 (按 Alt+A 截图)", QSystemTrayIcon.MessageIcon.Information, 1000)
            
        sys.exit(app.exec())
        
        try: ocr_manager.KillWeChatOCR() 
        except: pass