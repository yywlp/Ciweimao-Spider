# 刺猬猫小说导出工具（EPUB / TXT + 本地离线 OCR）

一个基于 Playwright（复用本机 Edge 登录态）的刺猬猫小说导出工具，支持将**已购/免费章节**按目录顺序导出为 **EPUB 3** 或 **纯文本 TXT**。针对刺猬猫付费图片章节，内置基于 **RapidOCR（PP-OCRv4 ONNX）** 的本地离线 OCR，自动去除背景斜向水印与行尾浅灰水印并智能还原自然段。

> **免责声明**：本工具仅用于将本人账号已拥有阅读权限的章节导出至本地备份与个人阅读。工具不会绕过登录、验证码或付费墙，且内置购买/订阅接口拦截以防误扣费。请勿将导出内容用于二次分发或商业用途。

## 功能特性

- **图形界面 + 命令行双模式**：提供基于 CustomTkinter 的桌面界面（`gui.py` / `启动界面.cmd`），支持选择导出模式、输入起止章节范围、实时查看日志及一键打开输出目录。
- **文字 + 图片章节混合支持**：
  - 文字章节直接提取实时渲染 DOM，自动过滤 `opacity` 隐藏/淡化水印、吐槽计数与评论区节点。
  - 图片章节优先拦截浏览器原始图片响应（失败时使用 canvas 缓存兜底），保留完整原始 PNG，并在导出 EPUB 时自动切片适配阅读器。
- **本地离线 OCR 转 TXT**：
  - 基于 `rapidocr-onnxruntime`，完全本机运行，无需联网 API 或 GPU。
  - 自动根据背景亮度去除斜向水印与行尾水印（兼容浅色/深色主题）。
  - 按空白行切分长图，避免截断文字，并根据首行缩进与行距自动合并自然段。
- **断点续传与缓存校验**：
  - 章节正文、图片及 OCR 结果均独立缓存并做 SHA-256 校验，中断后重跑自动复用已完成章节。
  - 缺章不会静默丢弃，会在文件名标记 `-未完整` 并在正文内保留占位提示。
- **安全与隐私**：
  - 使用独立 Edge 用户目录保存本机登录态，不读取日常浏览器配置，不导出或打印 Cookie，不自动触发购买/订阅/充值请求。

## 环境要求

- Windows 10 / 11 + Microsoft Edge
- Python 3.10+

## 安装依赖

```bash
python -m pip install -r requirements.txt
```

> 本工具直接调用系统已安装的 Microsoft Edge（`channel='msedge'`），无需额外下载 Playwright Chromium。

## 使用方法

### 1. 图形界面（推荐）

双击 **`启动界面.cmd`**，或在终端运行：

```bash
python gui.py
```

在界面中：
1. 输入书籍链接（如 `https://www.ciweimao.com/book/书籍ID`）或直接输入纯数字书籍 ID。
2. 选择导出模式：
   - **下载并导出 TXT（含图片自动 OCR）**
   - **下载并导出 EPUB + TXT（含图片自动 OCR）**
   - **仅下载并导出 EPUB**
   - **仅离线 OCR 转 TXT**（不打开浏览器，直接转换已下载到本地的书籍）
3. 选择 **整本全部章节** 或 **指定起止章节范围**（按目录顺序从 1 起算，结束留空表示到末尾）。
4. 首次使用时取消勾选“已有登录态，跳过登录确认”，在自动打开的 Edge 中完成登录，然后点击界面上的 **“已完成登录，继续”** 即可。

### 2. 命令行用法

```bash
# 首次运行（会打开 Edge 等待手动登录，登录后按回车开始）
python main.py https://www.ciweimao.com/book/书籍ID

# 已有本机登录态时，下载整本并导出 TXT（推荐单标签页更稳定）
python main.py https://www.ciweimao.com/book/书籍ID --no-prompt --workers 1 --txt-only

# 同时导出 EPUB 和 TXT，只试跑前 3 章
python main.py https://www.ciweimao.com/book/书籍ID --no-prompt --workers 1 --limit 3 --txt

# 指定从目录第 10 项起导出 5 章，后台无头运行
python main.py https://www.ciweimao.com/book/书籍ID --start 10 --limit 5 --no-prompt --headless --txt

# 对已下载到本地的书籍离线 OCR 转 TXT（也可双击 导出TXT.cmd）
python ocr.py 书籍ID
```

## 输出目录结构

导出文件默认保存在项目下的 `output/<书籍ID>/`：

```text
output/<书籍ID>/
├── catalog.json                  # 完整目录（保留分卷与顺序）
├── failures.json                 # 失败章节及原因（不含凭据或临时链接）
├── chapters/
│   ├── <章节ID>.json             # 文字正文或图片元数据（含 SHA-256）
│   ├── <章节ID>.png              # 图片章节原始完整 PNG
│   └── <章节ID>.ocr.json         # 本地 OCR 识别结果缓存
├── <书名>-<书籍ID>.epub          # EPUB 3 电子书
├── <书名>-<书籍ID>.txt           # 纯文本 TXT
└── range-<起始>-<结束>/          # 指定范围导出时的独立输出目录（共用 chapters 缓存）
```

## 项目结构

| 文件 | 说明 |
| --- | --- |
| `gui.py` | CustomTkinter 桌面图形界面 |
| `main.py` | 命令行主入口、并发调度与断点续传 |
| `browser.py` | Playwright 目录解析、购买拦截、文字/图片章节提取 |
| `exporter.py` | 本地缓存校验、EPUB 3 打包与 TXT 导出 |
| `ocr.py` | 本地离线去水印预处理、空白行切图、RapidOCR 识别与段落合并 |
| `tests/test_export.py` | 单元测试（涵盖目录、文字/图片抓取、缓存、EPUB/TXT 导出、OCR 与 GUI） |
| `启动界面.cmd` / `启动导出.cmd` / `导出TXT.cmd` | Windows 快捷启动脚本 |

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 注意事项

- 登录目录默认位于 `%LOCALAPPDATA%\CiweimaoImageSave\EdgeProfile`，请勿将此目录分享给他人；运行时请关闭占用同一登录目录的其他 Edge 窗口。
- 若遇到网站风控或加载超时，建议将并发数设为 `--workers 1` 并适当调大 `--delay`。
- 离线 OCR 针对小字号复杂形近字仍可能存在极少量误字，导出整本 TXT 后可按需对常见人名做一次批量校对。
