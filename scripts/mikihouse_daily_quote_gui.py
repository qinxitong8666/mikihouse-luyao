#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "generate_daily_quote.py"
LATEST = ROOT / "outputs" / "daily_quote" / "最新"


class DailyQuoteApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MIKI HOUSE 每日报价（Preview-only）")
        self.geometry("900x620")
        self.status = tk.StringVar(value="就绪；微信收藏真实写入已禁用")
        top = ttk.Frame(self, padding=12)
        top.pack(fill=tk.X)
        self.generate_button = ttk.Button(top, text="生成今日报价", command=self.generate)
        self.generate_button.pack(side=tk.LEFT)
        ttk.Button(top, text="预览PDF收藏", command=lambda: self.preview("wechat_pdf_favorite_preview.txt")).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="预览文字收藏", command=lambda: self.preview("wechat_text_favorite_preview.txt")).pack(side=tk.LEFT)
        disabled = ttk.Button(top, text="保存两个微信收藏（尚未验收）", state=tk.DISABLED)
        disabled.pack(side=tk.LEFT, padx=8)
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

    def preview(self, filename: str) -> None:
        path = LATEST / filename
        if not path.exists():
            messagebox.showwarning("尚无预览", "请先生成今日报价。")
            return
        self._set_output(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    DailyQuoteApp().mainloop()
