#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "generate_daily_quote.py"
PRODUCTION_ENTRYPOINT = ROOT / "scripts" / "run_mikihouse_daily_production.py"
RUNTIME_CONFIG = ROOT / "config" / "wechat_favorite_runtime.json"
LATEST = ROOT / "outputs" / "daily_quote" / "最新"
PRODUCTION_CONFIRMATION = "CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE"


class DailyQuoteApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MIKI HOUSE 每日报价")
        self.geometry("900x620")
        runtime_config = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
        self.production_enabled = runtime_config.get("production_save_enabled") is True
        initial = (
            "就绪；正式保存已启用，执行前仍需精确确认"
            if self.production_enabled
            else "就绪；微信收藏正式保存默认关闭"
        )
        self.status = tk.StringVar(value=initial)
        top = ttk.Frame(self, padding=12)
        top.pack(fill=tk.X)
        self.generate_button = ttk.Button(top, text="生成今日报价", command=self.generate)
        self.generate_button.pack(side=tk.LEFT)
        ttk.Button(top, text="预览PDF收藏", command=lambda: self.preview("wechat_pdf_favorite_preview.txt")).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="预览文字收藏", command=lambda: self.preview("wechat_text_favorite_preview.txt")).pack(side=tk.LEFT)
        self.production_button = ttk.Button(
            top,
            text="生成并保存两个微信收藏",
            command=self.generate_and_save,
            state=tk.NORMAL if self.production_enabled else tk.DISABLED,
        )
        self.production_button.pack(side=tk.LEFT, padx=8)
        ttk.Label(self, textvariable=self.status, padding=(12, 0)).pack(anchor=tk.W)
        self.output = scrolledtext.ScrolledText(self, wrap=tk.WORD)
        self.output.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

    def _set_output(self, text: str) -> None:
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, text)

    def generate(self) -> None:
        self.generate_button.configure(state=tk.DISABLED)
        self.status.set("正在完整抓取官网并生成报价……")

        def run() -> None:
            result = subprocess.run(
                [sys.executable, str(ENTRYPOINT)], cwd=ROOT, text=True, capture_output=True
            )
            self.after(0, self._finished, result.returncode, result.stdout, result.stderr)

        threading.Thread(target=run, daemon=True).start()

    def _finished(self, code: int, stdout: str, stderr: str) -> None:
        self.generate_button.configure(state=tk.NORMAL)
        self._set_output(stdout + ("\n" + stderr if stderr else ""))
        if code == 0:
            self.status.set("生成完成；微信收藏仅预览，未执行真实写入")
            subprocess.run(["open", str(LATEST)], check=False)
        else:
            self.status.set("生成失败并已安全停止；上次成功结果未覆盖")
            messagebox.showerror("生成失败", "请查看窗口中的错误详情。")

    def generate_and_save(self) -> None:
        if not self.production_enabled:
            messagebox.showwarning(
                "正式保存已关闭",
                "默认安全门禁仍关闭；请在新的明确授权任务中启用。",
            )
            return
        confirmation = simpledialog.askstring(
            "确认创建两条微信收藏",
            "请输入完整确认语句：\n" + PRODUCTION_CONFIRMATION,
        )
        if confirmation != PRODUCTION_CONFIRMATION:
            self.status.set("确认语句不匹配；零微信写入")
            return
        self.generate_button.configure(state=tk.DISABLED)
        self.production_button.configure(state=tk.DISABLED)
        self.status.set("正在重新抓取官网、生成报价并保存恰好两条微信收藏……")

        def run() -> None:
            result = subprocess.run(
                [
                    sys.executable,
                    str(PRODUCTION_ENTRYPOINT),
                    "--production-save",
                    "--confirm",
                    confirmation,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.after(
                0,
                self._production_finished,
                result.returncode,
                result.stdout,
                result.stderr,
            )

        threading.Thread(target=run, daemon=True).start()

    def _production_finished(self, code: int, stdout: str, stderr: str) -> None:
        self.generate_button.configure(state=tk.NORMAL)
        self.production_button.configure(
            state=tk.NORMAL if self.production_enabled else tk.DISABLED
        )
        self._set_output(stdout + ("\n" + stderr if stderr else ""))
        if code == 0:
            self.status.set("完成：两条收藏均已保存、重开并强回读验证")
            subprocess.run(["open", str(LATEST)], check=False)
        else:
            self.status.set("流程已安全停止；微信 mutation 不会自动重试")
            messagebox.showerror("正式流程停止", "请查看错误和 checkpoint；不要盲目重跑。")

    def preview(self, filename: str) -> None:
        path = LATEST / filename
        if not path.exists():
            messagebox.showwarning("尚无预览", "请先生成今日报价。")
            return
        self._set_output(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    DailyQuoteApp().mainloop()
