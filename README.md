# 🔍 OCRCO - 智能截图文字识别工具

OCRCO 是一款基于 **Python + PyQt6** 开发的轻量级 Windows 桌面 OCR 工具。它集成了 **WeChatOCR** 本地引擎，能够快速、精准地识别屏幕截图中的文字，并提供实时翻译功能。

> 🚀 **核心优势**：本地离线识别（保护隐私）、内存自动释放（轻量运行）、极简交互。

---

## ✨ 功能特性 (Features)

* **🎯 极速截图**：全局快捷键 `Alt + A` 一键唤起，左键框选，右键取消。
* **🔒 本地离线引擎**：内置 WeChatOCR 引擎，无需联网即可识别，识别率极高，尤其适合票据、文档提取。
* **🧠 智能内存优化**：独创 "Light Mode" 机制。识别完成后按 `ESC` 或关闭窗口，程序自动释放显存与内存，后台静默占用极低。
* **🌐 辅助翻译**：集成 Google Translate 接口，一键将识别结果翻译为中文。
* **📋 自动工作流**：识别成功后自动复制文字到剪贴板，支持开机自启。
* **👁️ 视觉体验**：高清截图预览，UI 自动缩放，支持高 DPI 屏幕。


`![软件截图](screenshot.png)`
---

## 🛠️ 安装与运行 (Installation)

### 方式一：下载直接运行 (推荐)
前往本项目的 [Releases](../../releases) 页面下载最新的 `OCRCO.exe` 压缩包。
1. 解压压缩包（确保 `ocr_engine` 文件夹与 exe 在同一目录）。
2. 双击 `OCRCO.exe` 即可使用。

### 方式二：从源码运行

如果你是开发者，想自己修改代码：


```bash
# 1. 克隆仓库
git clone [https://github.com/eielyving/OCRCO.git](https://github.com/eielyving/OCRCO.git)
cd OCRCO

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行
python main.py
```
  注意：本项目依赖 ```ocr_engine``` 文件夹中的二进制文件，请确保该文件夹完整存在。使用翻译功能需自行配置网络环境
---

## 🎮 使用指南 (Usage)
### 操作说明
 | 操作 | 说明 |
 | ------ | ------ | 
 | Alt + A | 唤起截图 (全局热键) | 
 | 鼠标左键拖拽 | 框选需要识别的区域 | 
 | 鼠标右键 | 取消截图/退出截图模式 | 
 | ESC | 隐藏窗口并进入*静默模式* (清理内存) | 
 | 复制并隐藏 | 将图片存入剪贴板，并后台运行 | 

---

## 📦 编译构建 (Build)
### 如果你修改了代码并想重新打包成 EXE：

1. 确保安装了 PyInstaller：
```pip install pyinstaller```
2. 使用项目自带的配置文件进行打包：
```pyinstaller OCRCO.spec```
3. 打包完成后，在 ```dist``` 文件夹中找到 ```OCRCO.exe```。

4. 重要：将根目录下的 ```ocr_engine``` 文件夹手动复制到 ```dist``` 文件夹中，与 exe 同级，否则无法识别

---

## 🧩 技术栈 (Tech Stack)
- Python 3.10+

- PyQt6 - 图形界面框架

- WeChatOCR - 提取自微信的强力 OCR 引擎

- Keyboard - 全局热键管理

---

## 📄 License
此项目供学习交流使用。WeChatOCR 引擎版权归腾讯所有。

---
