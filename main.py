import sys
import os
import threading
import requests
import json
import re
import gc 
import winreg 
import ctypes
from ctypes import wintypes
import math
import numpy as np
import cv2
from PyQt6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, 
                             QTextEdit, QPushButton, QMessageBox, QSystemTrayIcon, QMenu, 
                             QStyle, QSizePolicy, QFrame, QLineEdit, QFileDialog, QButtonGroup)
from PyQt6.QtCore import Qt, QRect, QPoint, QPointF, pyqtSignal, QObject, QSize, QTimer, QAbstractNativeEventFilter
from PyQt6.QtGui import (QPainter, QColor, QPen, QGuiApplication, QIcon, QAction, QFont, 
                        QPixmap, QFontMetrics, QImage, QPolygonF, QBrush, QCursor)
from wechat_ocr.ocr_manager import OcrManager

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

# --- 环境设置 ---
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
        self._pixmap = None

    def set_custom_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def clear_canvas(self):
        self._pixmap = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#888888"))
            font = painter.font()
            font.setPointSize(14)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "截图预览\n(Light Mode)")

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
    if gap > avg_h * 0.85:
        return False
    prev_txt = prev_it['text'].strip()
    cur_txt = cur_it['text'].strip()
    if not prev_txt or not cur_txt:
        return False
    if abs(cur_it['left'] - prev_it['left']) > avg_h * 1.5:
        return False
    if re.match(r'^((\d+[\.\、\)])|([一二三四五六七八九十]+[\.\、\)])|([•\-\*\+]))', cur_txt):
        return False
    if prev_txt[-1] in ('。', '！', '？', '；', ':', '：', ';', '{', '}'):
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

        # 1. 检测是否存在多栏/表格排版 (寻找全局贯通的纵向空隙 gutter)
        min_x = max(0.0, min(it['left'] for it in items))
        max_x = max(min_x + 10.0, max(it['right'] for it in items))
        
        w_span = min(20000, int(max_x) + 15)
        occupied = [0] * max(1, w_span)
        for it in items:
            start_x = max(0, min(w_span - 1, int(it['left'])))
            end_x = max(start_x, min(w_span, int(it['right'])))
            for x in range(start_x, end_x):
                occupied[x] += 1
                
        gutters = []
        in_gap = False
        gap_start = 0
        min_gap_width = max(8, int(avg_h * 0.5))
        for x in range(int(min_x), min(w_span, int(max_x))):
            if occupied[x] == 0:
                if not in_gap:
                    in_gap = True
                    gap_start = x
            else:
                if in_gap:
                    in_gap = False
                    if (x - gap_start) >= min_gap_width:
                        gutters.append((gap_start, x, (gap_start + x) / 2))

        is_multi_column = False
        split_x_list = []
        if gutters:
            valid_splits = []
            for g_start, g_end, split_x in gutters:
                left_count = sum(1 for it in items if it['right'] <= split_x)
                right_count = sum(1 for it in items if it['left'] >= split_x)
                if left_count >= 2 and right_count >= 2:
                    valid_splits.append(split_x)
            if valid_splits:
                is_multi_column = True
                split_x_list = valid_splits

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
            items.sort(key=lambda x: (round(x['top'] / (avg_h * 0.8)), x['left']))
            
            paragraphs = []
            current_para = []
            for it in items:
                if not current_para:
                    current_para.append(it)
                else:
                    prev = current_para[-1]
                    if should_merge_lines(prev, it, avg_h):
                        current_para.append(it)
                    else:
                        paragraphs.append(current_para)
                        current_para = [it]
                        
            if current_para:
                paragraphs.append(current_para)
                
            para_texts = []
            for p in paragraphs:
                merged = p[0]['text']
                for it in p[1:]:
                    merged = merge_text_lines(merged, it['text'])
                para_texts.append(merged)
                
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

        # 排版模式切换栏
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
        self.btn_mode_smart.setToolTip("自动识别多栏与表格，智能合并跨行文字 (默认)")
        self.btn_mode_smart.clicked.connect(lambda: self.switch_format_mode('smart'))

        self.btn_mode_para = QPushButton("📃 自然段落")
        self.btn_mode_para.setProperty("class", "modeBtn")
        self.btn_mode_para.setCheckable(True)
        self.btn_mode_para.setToolTip("合并换行硬回车，还原连贯自然段落")
        self.btn_mode_para.clicked.connect(lambda: self.switch_format_mode('paragraph'))

        self.btn_mode_raw = QPushButton("📄 逐行原始")
        self.btn_mode_raw.setProperty("class", "modeBtn")
        self.btn_mode_raw.setCheckable(True)
        self.btn_mode_raw.setToolTip("保留 OCR 检测的原始单行分行")
        self.btn_mode_raw.clicked.connect(lambda: self.switch_format_mode('raw'))

        self.btn_mode_md = QPushButton("📊 表格Markdown")
        self.btn_mode_md.setProperty("class", "modeBtn")
        self.btn_mode_md.setCheckable(True)
        self.btn_mode_md.setToolTip("将多栏表格转换为 Markdown 表格格式")
        self.btn_mode_md.clicked.connect(lambda: self.switch_format_mode('markdown'))

        self.mode_group.addButton(self.btn_mode_smart)
        self.mode_group.addButton(self.btn_mode_para)
        self.mode_group.addButton(self.btn_mode_raw)
        self.mode_group.addButton(self.btn_mode_md)

        mode_layout.addWidget(self.btn_mode_smart)
        mode_layout.addWidget(self.btn_mode_para)
        mode_layout.addWidget(self.btn_mode_raw)
        mode_layout.addWidget(self.btn_mode_md)
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
    def get_run_key(self):
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)

    def get_app_cmd(self):
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}"'
        else:
            main_script = os.path.abspath(__file__)
            py_exe = sys.executable
            pyw_exe = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            exe_to_use = pyw_exe if os.path.exists(pyw_exe) else py_exe
            return f'"{exe_to_use}" "{main_script}"'

    def check_autostart_status(self):
        try:
            key = self.get_run_key()
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def toggle_autostart(self):
        try:
            key = self.get_run_key()
            if self.autostart_action.isChecked():
                cmd = self.get_app_cmd()
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                self.tray_icon.showMessage("OCRCO", "已开启开机自启动", QSystemTrayIcon.MessageIcon.Information, 1000)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except Exception:
                    pass
                self.tray_icon.showMessage("OCRCO", "已关闭开机自启动", QSystemTrayIcon.MessageIcon.Information, 1000)
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[WARN] toggle_autostart error: {e}")
    def show_window(self):
        self.showNormal(); self.activateWindow(); self.setFocus()
    def quit_app(self): QApplication.quit()
    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger: self.show_window()

    def process_ocr(self, pixmap, clean_pixmap=None):
        self.current_pixmap = pixmap
        self.image_label.set_custom_pixmap(pixmap)
        
        self.trans_image_label.hide()
        self.text_edit.show()
        self.text_edit.setHtml("")
        self.text_edit.setPlaceholderText("正在识别中...")
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
        if text == "APPLY_FORMAT":
            self.apply_current_format()
            return
        elif text.startswith("PAINT_OVERLAY"):
            self.paint_translation_overlay(text.replace("PAINT_OVERLAY", ""))
            return
        self.update_text(text)

    def switch_format_mode(self, mode):
        self.current_format_mode = mode
        self.apply_current_format()

    def apply_current_format(self):
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
            self.tray_icon.showMessage("OCRCO", "文字排版已复制到剪贴板！", QSystemTrayIcon.MessageIcon.Information, 800)

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
        if not self.current_ocr_data:
            self.text_edit.append("\n没有可用于定位的文字信息，无法进行图片翻译。")
            return

        self.text_edit.setPlaceholderText("正在进行图片级翻译与渲染...")
        self.text_edit.clear()
        
        threading.Thread(target=self.do_translate_overlay_thread, daemon=True).start()

    def do_translate_overlay_thread(self):
        try:
            texts_to_trans = []
            valid_blocks = []
            
            for block in self.current_ocr_data:
                txt = block.get('text', '').strip()
                loc = block.get('location', {})
                if txt and loc and loc.get('left') is not None:
                    texts_to_trans.append(txt)
                    valid_blocks.append(block)
            
            if not texts_to_trans:
                self.signaler.result_ready.emit("未检测到有效坐标文本")
                return

            joint_text = " ||| ".join(texts_to_trans)
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": joint_text}
            
            resp = requests.get(url, params=params, timeout=10)
            translated_results = []
            
            if resp.status_code == 200:
                data = resp.json()
                full_trans = "".join([x[0] for x in data[0]])
                translated_results = full_trans.split("|||")
            else:
                self.signaler.result_ready.emit(f"翻译接口出错: {resp.status_code}")
                return

            self.translated_blocks_cache = []
            for i, block in enumerate(valid_blocks):
                if i < len(translated_results):
                    trans_text = translated_results[i].strip()
                    self.translated_blocks_cache.append({
                        'rect': block['location'],
                        'text': trans_text
                    })
            
            self.signaler.result_ready.emit("PAINT_OVERLAY")

        except Exception as e:
            self.signaler.result_ready.emit(f"翻译处理异常: {e}")

    def paint_translation_overlay(self, _):
        if not self.current_pixmap: return
        
        new_pixmap = self.current_pixmap.copy()
        painter = QPainter(new_pixmap)
        
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        
        src_image = self.current_pixmap.toImage()
        img_w, img_h = src_image.width(), src_image.height()
        
        base_font = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        
        for item in self.translated_blocks_cache:
            loc = item.get('rect') or {}
            text = item.get('text', '')
            
            x = int(safe_float(loc.get('left'), 0))
            y = int(safe_float(loc.get('top'), 0))
            r = int(safe_float(loc.get('right'), x))
            b = int(safe_float(loc.get('bottom'), y))
            w = max(1, r - x)
            h = max(1, b - y)
            
            pad = 2 
            bg_x = max(0, x - pad)
            bg_y = max(0, y - pad)
            bg_w = min(img_w - bg_x, w + pad * 2)
            bg_h = min(img_h - bg_y, h + pad * 2)
            
            bg_rect = QRect(bg_x, bg_y, bg_w, bg_h)
            
            sample_x = max(0, x - 5)
            sample_y = max(0, y + h // 2) 
            bg_color = src_image.pixelColor(sample_x, sample_y)
            
            painter.fillRect(bg_rect, bg_color)
            
            if bg_color.lightness() < 128:
                text_color = QColor(255, 255, 255) 
            else:
                text_color = QColor(0, 0, 0)       
            painter.setPen(text_color)
            
            target_pixel_size = max(8, min(24, int(h * 0.8)))
            base_font.setPixelSize(target_pixel_size)
            
            fm = QFontMetrics(base_font)
            text_width = fm.horizontalAdvance(text)
            
            while text_width > (w + 10) and target_pixel_size > 8:
                target_pixel_size -= 1
                base_font.setPixelSize(target_pixel_size)
                fm = QFontMetrics(base_font)
                text_width = fm.horizontalAdvance(text)
            
            painter.setFont(base_font)
            
            draw_rect = QRect(x, y, w, h)
            painter.drawText(draw_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
            
        painter.end()
        self.text_edit.hide()
        self.trans_image_label.set_custom_pixmap(new_pixmap)
        self.trans_image_label.show()

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
                padding: 4px 10px;
                border-radius: 4px;
            }
            QPushButton#ocrBtn:hover {
                background: #09D66A;
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
        r1_layout.setSpacing(3)
        
        self.tool_buttons = {}
        tool_defs = [
            ('rect', '🔲 矩形', '矩形框选标注'),
            ('ellipse', '⭕ 椭圆', '椭圆标注'),
            ('arrow', '↗ 箭头', '箭头标注'),
            ('pen', '✏️ 涂鸦', '画笔自由涂鸦'),
            ('text', '🔤 文字', '添加文字说明')
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
        
        self.ocr_btn = QPushButton('🔍 识别')
        self.ocr_btn.setObjectName('ocrBtn')
        self.ocr_btn.setToolTip('完成并进行 OCR 文字识别 (Enter)')
        self.ocr_btn.clicked.connect(self.ocr_clicked.emit)
        r1_layout.addWidget(self.ocr_btn)
        
        self.copy_btn = QPushButton('📋 复制')
        self.copy_btn.setProperty('class', 'actionBtn')
        self.copy_btn.setToolTip('复制截图到剪贴板 (Ctrl+C)')
        self.copy_btn.clicked.connect(self.copy_clicked.emit)
        r1_layout.addWidget(self.copy_btn)
        
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
        text = "🖱️ 移动鼠标自动框选 / 拖拽自定义选区 | 单击自动吸附 | 右键或 Esc 退出"
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
                
        elif event.key() == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo_annotation()
            
        elif event.key() == Qt.Key.Key_C and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            if self.state == 'SELECTED':
                self.copy_to_clipboard()
                
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.state == 'SELECTED' and not self.text_editor.hasFocus():
                self.commit_ocr()
                
        else:
            super().keyPressEvent(event)

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
    app = QApplication(sys.argv)
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