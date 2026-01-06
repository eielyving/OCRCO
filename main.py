import sys
import os
import threading
import requests
import json
import re
import gc 
import winreg 
from PyQt6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, 
                             QTextEdit, QPushButton, QMessageBox, QSystemTrayIcon, QMenu, 
                             QStyle, QSizePolicy)
from PyQt6.QtCore import Qt, QRect, QPoint, pyqtSignal, QObject, QSize
from PyQt6.QtGui import QPainter, QColor, QPen, QGuiApplication, QIcon, QAction, QFont, QPixmap
import keyboard
from wechat_ocr.ocr_manager import OcrManager

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

def get_app_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_PATH = get_app_path()
ICON_PATH = resource_path("icon-48.png")
MY_OCR_FOLDER = os.path.join(APP_PATH, "ocr_engine")
WECHAT_OCR_EXE = os.path.join(MY_OCR_FOLDER, "WeChatOCR.exe")
WECHAT_LIB_DIR = MY_OCR_FOLDER

class OCRSignaler(QObject):
    result_ready = pyqtSignal(str)
    hotkey_pressed = pyqtSignal()

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

class ResultWindow(QWidget):
    def __init__(self, ocr_manager, signaler):
        super().__init__()
        self.ocr_manager = ocr_manager
        self.signaler = signaler
        self.selector = None # 将在外部赋值
        
        self.setWindowTitle("OCRCO - 智能识别")
        self.resize(1100, 650)
        
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        
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

        self.setup_ui()
        self.current_pixmap = None
        self.signaler.result_ready.connect(self.update_text)
        
        if hasattr(self.ocr_manager, 'SetOcrResultCallback'):
            self.ocr_manager.SetOcrResultCallback(self.global_ocr_callback)

    def set_selector_reference(self, selector):
        self.selector = selector

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget { font-family: 'Microsoft YaHei', 'SimHei'; }
            ResultWindow { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2b1055, stop:1 #7597de); }
            QTextEdit {
                background-color: rgba(255, 255, 255, 0.95);
                border-radius: 15px; border: none; padding: 20px;
                color: #333; selection-background-color: #7597de;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 0.2);
                border: 1px solid rgba(255, 255, 255, 0.3);
                border-radius: 8px; color: white; font-size: 14px; padding: 8px; font-weight: bold;
            }
            QPushButton:hover { background-color: rgba(255, 255, 255, 0.4); }
            QPushButton:pressed { background-color: rgba(255, 255, 255, 0.1); }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        self.image_label = AutoScalableLabel()

        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("等待识别...")
        
        btn_layout = QHBoxLayout()
        self.copy_img_btn = QPushButton("📷 复制并隐藏")
        self.copy_img_btn.setFixedHeight(40)
        self.copy_img_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.trans_btn = QPushButton("🌐 翻译文本")
        self.trans_btn.setFixedHeight(40)
        self.trans_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.save_btn = QPushButton("💾")
        self.save_btn.setFixedSize(40, 40)
        self.save_btn.setToolTip("保存图片到本地")
        
        self.copy_img_btn.clicked.connect(self.copy_image_action)
        self.save_btn.clicked.connect(self.save_image_action)
        self.trans_btn.clicked.connect(self.translate_action)
        
        btn_layout.addWidget(self.copy_img_btn)
        btn_layout.addWidget(self.trans_btn)
        btn_layout.addWidget(self.save_btn)
        
        right_layout.addWidget(self.text_edit)
        right_layout.addLayout(btn_layout)
        
        main_layout.addWidget(self.image_label, 1)
        main_layout.addLayout(right_layout, 1)

    def enter_light_mode(self):
        """深度清理内存"""
        # 1. 清理主界面图片
        self.text_edit.clear()
        self.image_label.clear_canvas()
        self.current_pixmap = None
        
        # 2. 清理截图器的大图缓存 (关键！)
        if self.selector:
            self.selector.clear_memory()
            
        self.hide()
        
        # 3. 强制 GC
        gc.collect() 
        self.tray_icon.showMessage("OCRCO", "进入静默模式", QSystemTrayIcon.MessageIcon.Information, 500)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.enter_light_mode()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.enter_light_mode()
        event.ignore()

    def get_run_key(self):
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)

    def check_autostart_status(self):
        try:
            key = self.get_run_key()
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False

    def toggle_autostart(self):
        key = self.get_run_key()
        if self.autostart_action.isChecked():
            exe_path = sys.executable
            try:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
            except Exception as e:
                QMessageBox.warning(self, "错误", f"无法设置开机自启: {e}")
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)

    def show_window(self):
        self.showNormal()
        self.activateWindow()
        self.setFocus() 

    def quit_app(self):
        QApplication.quit()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def process_ocr(self, pixmap):
        self.current_pixmap = pixmap
        self.image_label.set_custom_pixmap(pixmap)
        
        self.text_edit.setHtml("")
        self.text_edit.setPlaceholderText("正在识别中...")
        self.show_window()
        
        temp_path = os.path.join(APP_PATH, "temp_ocr.png")
        pixmap.save(temp_path)

        try:
            self.ocr_manager.DoOCRTask(temp_path)
        except Exception as e:
            self.text_edit.setPlainText(f"引擎调用异常: {e}")

    def global_ocr_callback(self, img_path, results):
        try:
            final_text = ""
            text_blocks = []
            if isinstance(results, dict):
                if 'ocrResult' in results:
                    text_blocks = results['ocrResult']
                elif 'ocr_response' in results:
                    text_blocks = results['ocr_response']
            
            if text_blocks:
                lines = [str(block.get('text', '')) for block in text_blocks if 'text' in block]
                final_text = "\n".join(lines)
            
            clean_text = final_text.strip()
            if len(clean_text) < 3 and not re.search(r'[\u4e00-\u9fa5a-zA-Z0-9]', clean_text):
                 self.signaler.result_ready.emit("") 
            elif clean_text:
                self.signaler.result_ready.emit(final_text)
            else:
                self.signaler.result_ready.emit("未检测到有效文字") 
                
        except Exception as e:
            self.signaler.result_ready.emit(f"解析错误: {e}")

    def update_text(self, text):
        if not text:
            self.text_edit.setHtml("")
            self.text_edit.setPlaceholderText("未识别到文字 (已过滤杂讯)")
            return

        html_content = f"""
        <html>
        <head>
        <style>
            body {{
                font-family: 'Microsoft YaHei', 'SimHei';
                font-size: 20px;
                line-height: 1.4; 
                color: #333;
            }}
        </style>
        </head>
        <body>
        {text.replace(chr(10), '<br>')}
        </body>
        </html>
        """
        self.text_edit.setHtml(html_content)

        if "未检测到" not in text and "正在识别" not in text:
            QApplication.clipboard().setText(text)

    def copy_image_action(self):
        if self.current_pixmap:
            QApplication.clipboard().setPixmap(self.current_pixmap)
            self.enter_light_mode()

    def save_image_action(self):
        if self.current_pixmap:
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getSaveFileName(self, "保存图片", "OCR.png", "PNG (*.png)")
            if path: self.current_pixmap.save(path)

    def translate_action(self):
        text = self.text_edit.toPlainText()
        if not text or "未检测到" in text: return
        self.text_edit.append("\n\n--- 正在翻译... ---")
        threading.Thread(target=self.do_translate, args=(text,), daemon=True).start()

    def do_translate(self, text):
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                trans_res = "".join([x[0] for x in data[0]])
                self.signaler.result_ready.emit(f"{text}\n\n=== 翻译结果 ===\n{trans_res}")
            else:
                self.signaler.result_ready.emit(f"{text}\n\n[翻译失败] {resp.status_code}")
        except Exception as e:
            self.signaler.result_ready.emit(f"{text}\n\n[网络错误]: {e}")

class ScreenshotSelector(QWidget):
    def __init__(self, result_window):
        super().__init__()
        self.result_window = result_window
        # 移除 Tool 属性，有时它会导致 DWM 缓存刷新不及时
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        
        self.start_point = None
        self.end_point = None
        self.is_selecting = False
        self.full_screen_pixmap = None

    def clear_memory(self):
        self.full_screen_pixmap = None

    def start_capture(self):
        # 1. 状态重置
        self.start_point = None
        self.end_point = None
        self.is_selecting = False
        self.full_screen_pixmap = None
        
        # 2. 获取新截图
        screen = QGuiApplication.primaryScreen()
        # 这一步是关键：先拿到数据，再显示窗口
        self.full_screen_pixmap = screen.grabWindow(0)
        self.setGeometry(screen.geometry())
        
        # 3. 显示窗口
        # 理论上因为 paintEvent 此时会画新图，所以直接显示即可
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.CrossCursor)
        
        # 4. 强制刷新，确保第一帧就是新图
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        
        # 核心逻辑：如果没有图片，就什么都不画（即完全透明），
        # 这样就算发生闪烁，用户也看不见（因为是透明的）
        if not self.full_screen_pixmap: 
            return
            
        painter.drawPixmap(0, 0, self.full_screen_pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        
        # 严格判断：只有正在选择且坐标有效时才画框
        if self.is_selecting and self.start_point and self.end_point:
            rect = QRect(self.start_point, self.end_point).normalized()
            painter.drawPixmap(rect, self.full_screen_pixmap, rect)
            painter.setPen(QPen(QColor(0, 255, 255), 2))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.close_and_clear()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.start_point = event.pos()
            self.end_point = event.pos()
            self.is_selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self.is_selecting:
            self.end_point = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_selecting:
            rect = QRect(self.start_point, self.end_point).normalized()
            
            if rect.width() > 5 and rect.height() > 5:
                captured = self.full_screen_pixmap.copy(rect)
                self.close_and_clear() 
                self.result_window.process_ocr(captured)
            else:
                # 选区太小，视为误触，重置选区但不退出
                self.start_point = None
                self.end_point = None
                self.is_selecting = False
                self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape: 
            self.close_and_clear()

    def close_and_clear(self):
        # --- 终极防闪烁逻辑 ---
        # 1. 既然要退出了，先把图片引用干掉
        self.full_screen_pixmap = None
        # 2. 强制重绘。
        # 因为上面 pixmap 设为 None 了，paintEvent 会画一个“空”的透明层。
        self.repaint() 
        
        # 3. 【关键】告诉 Qt：“立刻、马上处理这个重绘事件，不要等！”
        # 这确保了在窗口隐藏之前，它已经在屏幕上变成透明的了。
        QApplication.processEvents()
        
        # 4. 现在 Windows 记住的是一个透明窗口，下次出来就不会闪旧图了
        self.hide()
        self.clear_memory()

def hotkey_thread_func(signaler):
    try:
        keyboard.add_hotkey('alt+a', signaler.hotkey_pressed.emit)
        keyboard.wait()
    except Exception as e:
        print(f"Hotkey Error: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    
    signaler = OCRSignaler()
    try:
        ocr_manager = OcrManager(WECHAT_LIB_DIR)
        ocr_manager.SetExePath(WECHAT_OCR_EXE)
        ocr_manager.SetUsrLibDir(WECHAT_LIB_DIR)
        ocr_manager.StartWeChatOCR()
    except Exception as e:
        pass

    res_win = ResultWindow(ocr_manager, signaler)
    selector = ScreenshotSelector(res_win)
    
    # 关键：建立引用，让主窗口能控制截图器的内存清理
    res_win.set_selector_reference(selector) 
    
    signaler.hotkey_pressed.connect(selector.start_capture)
    
    t = threading.Thread(target=hotkey_thread_func, args=(signaler,), daemon=True)
    t.start()
    
    if res_win.tray_icon.isVisible():
        res_win.tray_icon.showMessage("OCRCO", "服务已就绪 (按 Alt+A 截图)", QSystemTrayIcon.MessageIcon.Information, 1000)
        
    sys.exit(app.exec())
    try: ocr_manager.KillWeChatOCR() 
    except: pass
