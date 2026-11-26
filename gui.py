"""Desktop GUI for Ciweimao EPUB/TXT exporter and offline OCR."""

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import customtkinter as ctk

from exporter import ContentError, book_id


ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "output"

MODES = [
    ("txt_only", "下载并导出 TXT（含图片自动 OCR）"),
    ("both", "下载并导出 EPUB + TXT（含图片自动 OCR）"),
    ("epub_only", "仅下载并导出 EPUB"),
    ("offline_ocr", "仅离线 OCR 转 TXT（不打开浏览器，转已下载书籍）"),
]


def normalize_target(raw_text, offline_ocr=False):
    text = (raw_text or "").strip()
    if not text:
        raise ContentError("请输入书籍链接或书籍 ID。")
    if offline_ocr:
        if text.isdigit():
            return text
        if text.startswith("http"):
            return book_id(text)
        return text
    if text.isdigit():
        return f"https://www.ciweimao.com/book/{text}"
    identifier = book_id(text)
    return f"https://www.ciweimao.com/book/{identifier}"


def parse_chapter_range(all_chapters, start_text, end_text):
    if all_chapters:
        return 1, None
    start_raw = (start_text or "").strip()
    end_raw = (end_text or "").strip()
    if not start_raw:
        start = 1
    else:
        if not start_raw.isdigit() or int(start_raw) < 1:
            raise ContentError("起始章节必须为正整数（从 1 开始）。")
        start = int(start_raw)
    if not end_raw:
        return start, None
    if not end_raw.isdigit() or int(end_raw) < 1:
        raise ContentError("结束章节必须为正整数。")
    end = int(end_raw)
    if end < start:
        raise ContentError("结束章节不能小于起始章节。")
    return start, end - start + 1


def build_command(
    python_exe,
    target,
    mode,
    all_chapters=True,
    start_text="",
    end_text="",
    workers=1,
    delay=2.0,
    no_prompt=True,
    headless=False,
):
    offline = mode == "offline_ocr"
    normalized = normalize_target(target, offline_ocr=offline)
    start, limit = parse_chapter_range(all_chapters, start_text, end_text)
    if offline:
        cmd = [str(python_exe), str(ROOT / "ocr.py"), normalized]
        if start > 1:
            cmd += ["--start", str(start)]
        if limit is not None:
            cmd += ["--limit", str(limit)]
        return cmd

    try:
        workers_int = int(workers)
        delay_val = float(delay)
    except (TypeError, ValueError):
        raise ContentError("并行标签页数和间隔秒数必须为有效数字。") from None
    if workers_int not in (1, 2, 3, 4):
        raise ContentError("并行标签页数必须在 1 到 4 之间。")
    if delay_val < 1:
        raise ContentError("抓取间隔至少为 1 秒。")
    if headless and not no_prompt:
        raise ContentError("后台无头运行需要先勾选“已有登录状态，跳过手动登录确认”。")

    cmd = [
        str(python_exe),
        str(ROOT / "main.py"),
        normalized,
        "--workers",
        str(workers_int),
        "--delay",
        str(delay_val),
    ]
    if start > 1:
        cmd += ["--start", str(start)]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    if no_prompt:
        cmd.append("--no-prompt")
    if headless:
        cmd.append("--headless")
    if mode == "both":
        cmd.append("--txt")
    elif mode == "txt_only":
        cmd.append("--txt-only")
    return cmd


class CiweimaoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("刺猬猫小说导出工具（EPUB / TXT + 本地 OCR）")
        self.geometry("820x720")
        self.minsize(740, 640)

        self.process = None
        self.log_queue = queue.Queue()

        self.mode_var = ctk.StringVar(value="txt_only")
        self.range_var = ctk.StringVar(value="all")
        self.workers_var = ctk.StringVar(value="1")
        self.delay_var = ctk.StringVar(value="2")
        self.no_prompt_var = ctk.BooleanVar(value=True)
        self.headless_var = ctk.BooleanVar(value=False)

        self._build_ui()
        self._update_controls()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # 1. Book input
        book_frame = ctk.CTkFrame(self)
        book_frame.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        book_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(book_frame, text="书籍链接 / ID：", font=("Microsoft YaHei UI", 13, "bold")).grid(
            row=0, column=0, padx=(12, 8), pady=12, sticky="w"
        )
        self.url_entry = ctk.CTkEntry(
            book_frame,
            placeholder_text="粘贴 https://www.ciweimao.com/book/书籍ID 或直接输入纯数字书籍 ID",
            font=("Microsoft YaHei UI", 13),
        )
        self.url_entry.grid(row=0, column=1, padx=(0, 8), pady=12, sticky="ew")

        ctk.CTkButton(
            book_frame,
            text="打开输出目录",
            width=110,
            font=("Microsoft YaHei UI", 12),
            command=self._open_output,
        ).grid(row=0, column=2, padx=(0, 12), pady=12)

        # 2. Mode selection
        mode_frame = ctk.CTkFrame(self)
        mode_frame.grid(row=1, column=0, padx=16, pady=6, sticky="ew")
        mode_frame.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(mode_frame, text="导出模式", font=("Microsoft YaHei UI", 13, "bold")).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="w"
        )
        for idx, (value, label) in enumerate(MODES):
            r, c = divmod(idx, 2)
            ctk.CTkRadioButton(
                mode_frame,
                text=label,
                variable=self.mode_var,
                value=value,
                font=("Microsoft YaHei UI", 12),
                command=self._update_controls,
            ).grid(row=r + 1, column=c, padx=14, pady=6, sticky="w")

        # 3. Chapter range + crawl options
        opt_frame = ctk.CTkFrame(self)
        opt_frame.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        opt_frame.grid_columnconfigure(0, weight=1)

        range_row = ctk.CTkFrame(opt_frame, fg_color="transparent")
        range_row.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")

        ctk.CTkLabel(range_row, text="章节范围：", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left", padx=(2, 8))
        ctk.CTkRadioButton(
            range_row, text="整本全部章节", variable=self.range_var, value="all",
            font=("Microsoft YaHei UI", 12), command=self._update_controls
        ).pack(side="left", padx=(0, 14))
        ctk.CTkRadioButton(
            range_row, text="指定范围：第", variable=self.range_var, value="custom",
            font=("Microsoft YaHei UI", 12), command=self._update_controls
        ).pack(side="left")

        self.start_entry = ctk.CTkEntry(range_row, width=70, placeholder_text="1", font=("Microsoft YaHei UI", 12))
        self.start_entry.pack(side="left", padx=4)
        self.start_entry.insert(0, "1")
        ctk.CTkLabel(range_row, text="章 ～ 第", font=("Microsoft YaHei UI", 12)).pack(side="left")
        self.end_entry = ctk.CTkEntry(range_row, width=70, placeholder_text="留空=末尾", font=("Microsoft YaHei UI", 12))
        self.end_entry.pack(side="left", padx=4)
        ctk.CTkLabel(range_row, text="章（按目录序号）", font=("Microsoft YaHei UI", 12)).pack(side="left")

        crawl_row = ctk.CTkFrame(opt_frame, fg_color="transparent")
        crawl_row.grid(row=1, column=0, padx=10, pady=(2, 10), sticky="ew")

        ctk.CTkLabel(crawl_row, text="抓取设置：", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left", padx=(2, 8))
        ctk.CTkLabel(crawl_row, text="并发标签页", font=("Microsoft YaHei UI", 12)).pack(side="left")
        self.workers_menu = ctk.CTkOptionMenu(
            crawl_row, values=["1", "2", "3", "4"], variable=self.workers_var, width=64, font=("Microsoft YaHei UI", 12)
        )
        self.workers_menu.pack(side="left", padx=(4, 12))

        ctk.CTkLabel(crawl_row, text="间隔(秒)", font=("Microsoft YaHei UI", 12)).pack(side="left")
        self.delay_entry = ctk.CTkEntry(crawl_row, width=56, textvariable=self.delay_var, font=("Microsoft YaHei UI", 12))
        self.delay_entry.pack(side="left", padx=(4, 14))

        self.no_prompt_chk = ctk.CTkCheckBox(
            crawl_row, text="已有登录态，跳过登录确认", variable=self.no_prompt_var,
            font=("Microsoft YaHei UI", 12), command=self._update_controls
        )
        self.no_prompt_chk.pack(side="left", padx=(0, 12))

        self.headless_chk = ctk.CTkCheckBox(
            crawl_row, text="后台无头运行", variable=self.headless_var, font=("Microsoft YaHei UI", 12)
        )
        self.headless_chk.pack(side="left")

        # 4. Action buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=16, pady=6, sticky="ew")

        self.start_btn = ctk.CTkButton(
            btn_frame, text="开始运行", width=130, height=36, font=("Microsoft YaHei UI", 13, "bold"),
            command=self._start
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.continue_btn = ctk.CTkButton(
            btn_frame, text="已完成登录，继续", width=150, height=36, font=("Microsoft YaHei UI", 12),
            fg_color="#2e7d32", hover_color="#1b5e20", state="disabled", command=self._send_enter
        )
        self.continue_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="停止任务", width=100, height=36, font=("Microsoft YaHei UI", 12),
            fg_color="#c62828", hover_color="#8e0000", state="disabled", command=self._stop
        )
        self.stop_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_frame, text="清空日志", width=90, height=36, font=("Microsoft YaHei UI", 12),
            fg_color="gray40", hover_color="gray30", command=self._clear_log
        ).pack(side="left")

        self.status_label = ctk.CTkLabel(btn_frame, text="状态：就绪", font=("Microsoft YaHei UI", 12))
        self.status_label.pack(side="right", padx=6)

        # 5. Live log textbox
        self.log_box = ctk.CTkTextbox(self, font=("Consolas", 12), wrap="word")
        self.log_box.grid(row=4, column=0, padx=16, pady=(4, 14), sticky="nsew")
        self.log_box.configure(state="disabled")

    def _update_controls(self):
        running = self.process is not None and self.process.poll() is None
        offline = self.mode_var.get() == "offline_ocr"
        custom_range = self.range_var.get() == "custom"

        range_state = "normal" if (custom_range and not running) else "disabled"
        self.start_entry.configure(state=range_state)
        self.end_entry.configure(state=range_state)

        crawl_state = "normal" if (not offline and not running) else "disabled"
        self.workers_menu.configure(state=crawl_state)
        self.delay_entry.configure(state=crawl_state)
        self.no_prompt_chk.configure(state=crawl_state)
        if not self.no_prompt_var.get():
            self.headless_var.set(False)
        headless_state = "normal" if (not offline and not running and self.no_prompt_var.get()) else "disabled"
        self.headless_chk.configure(state=headless_state)

        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        can_continue = running and not offline and not self.no_prompt_var.get()
        self.continue_btn.configure(state="normal" if can_continue else "disabled")

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _open_output(self):
        target_dir = OUTPUT_ROOT
        raw = self.url_entry.get().strip()
        try:
            if raw.isdigit() and (OUTPUT_ROOT / raw).exists():
                target_dir = OUTPUT_ROOT / raw
            elif raw.startswith("http"):
                bid = book_id(raw)
                if (OUTPUT_ROOT / bid).exists():
                    target_dir = OUTPUT_ROOT / bid
        except ContentError:
            pass
        target_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target_dir))

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        try:
            cmd = build_command(
                python_exe=sys.executable,
                target=self.url_entry.get(),
                mode=self.mode_var.get(),
                all_chapters=(self.range_var.get() == "all"),
                start_text=self.start_entry.get(),
                end_text=self.end_entry.get(),
                workers=self.workers_var.get(),
                delay=self.delay_var.get(),
                no_prompt=self.no_prompt_var.get(),
                headless=self.headless_var.get(),
            )
        except ContentError as error:
            self._append_log(f"[参数错误] {error}\n")
            self.status_label.configure(text="状态：参数有误")
            return

        self._append_log(f"=== 开始执行 ===\n命令：{' '.join(cmd)}\n")
        self.status_label.configure(text="状态：运行中...")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=flags,
            )
        except OSError as error:
            self._append_log(f"[启动失败] {error}\n")
            self.status_label.configure(text="状态：启动失败")
            return

        self._update_controls()
        threading.Thread(target=self._reader_thread, args=(self.process,), daemon=True).start()

    def _reader_thread(self, proc):
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log_queue.put(("log", line))
            code = proc.wait()
            self.log_queue.put(("done", code))
        except Exception as error:
            self.log_queue.put(("log", f"\n[日志读取异常] {error}\n"))
            self.log_queue.put(("done", 1))

    def _send_enter(self):
        if self.process is not None and self.process.poll() is None and self.process.stdin:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
                self._append_log("[已发送登录确认，开始抓取...]\n")
                self.continue_btn.configure(state="disabled")
            except OSError:
                pass

    def _stop(self):
        if self.process is not None and self.process.poll() is None:
            self._append_log("\n[正在停止任务，已保存章节与 OCR 缓存会保留...]\n")
            self.process.terminate()
            self.status_label.configure(text="状态：已请求停止")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self.process = None
                    if payload == 0:
                        self.status_label.configure(text="状态：全部完成")
                        self._append_log("=== 任务完成（全部成功）===\n\n")
                    elif payload == 2:
                        self.status_label.configure(text="状态：完成（有缺章，可续传）")
                        self._append_log("=== 任务结束（存在缺章，可再次点击开始运行续传）===\n\n")
                    else:
                        self.status_label.configure(text=f"状态：已结束（退出码 {payload}）")
                        self._append_log(f"=== 任务结束（退出码 {payload}）===\n\n")
                    self._update_controls()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.destroy()


def main():
    app = CiweimaoApp()
    app.mainloop()


if __name__ == "__main__":
    main()
